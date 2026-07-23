"""The distribution queue: enqueue a send, claim it, run it.

A request never calls distribute_run() directly anymore — it enqueues a
DistributionBatch (status=queued) and returns immediately. A worker (an
in-process thread for single-dyno deployments, or a separate
`flask distribution-worker` process once a real worker dyno exists) claims
the oldest queued batch and runs the existing distribute_run() against it.

Claiming locks the row on Postgres (SELECT ... FOR UPDATE SKIP LOCKED), so
running the worker in more than one process at once is safe: a second worker
just skips a batch the first has already claimed rather than double-sending.
SQLite (used in tests) has no row locking, which is fine there since tests
never run more than one worker concurrently.
"""
import os
import socket
import time
from datetime import datetime, timedelta, timezone

from flask import current_app
from sqlalchemy import func

from app import db
from app.audit import record_audit
from app.models import (
    BATCH_CANCELLED,
    BATCH_COMPLETED,
    BATCH_FAILED,
    BATCH_PENDING_STATUSES,
    BATCH_QUEUED,
    BATCH_RUNNING,
    BATCH_SCHEDULED,
    DELIVERY_CANCELLED,
    DELIVERY_FAILED,
    WORKER_STATUS_RUNNING,
    WORKER_STATUS_STOPPED,
    DistributionBatch,
    DistributionWorkerHeartbeat,
    PayrollRun,
    PayslipDelivery,
    User,
)

from .service import as_aware, distribute_run, retry_delivery


def default_worker_name():
    """A stable name for the worker process, so its heartbeat row is upserted
    (not duplicated) across restarts. Overridable per process via env."""
    return os.getenv("DISTRIBUTION_WORKER_NAME") or f"{socket.gethostname()}"


def record_heartbeat(worker_name, status=WORKER_STATUS_RUNNING):
    """Upsert this worker's liveness row. Commits on its own (called at the top of
    a poll, before any batch work is staged)."""
    now = datetime.now(timezone.utc)
    hb = DistributionWorkerHeartbeat.query.filter_by(worker_name=worker_name).first()
    if hb is None:
        hb = DistributionWorkerHeartbeat(worker_name=worker_name, started_at=now)
        db.session.add(hb)
    hb.status = status
    hb.host = socket.gethostname()
    hb.pid = os.getpid()
    hb.last_poll_at = now
    db.session.commit()


def worker_last_poll():
    """The most recent poll time across all worker processes (inline or external),
    or None if no worker has ever run. Read by the monitoring dashboard."""
    try:
        return db.session.query(func.max(DistributionWorkerHeartbeat.last_poll_at)).scalar()
    except Exception:  # noqa: BLE001 - dashboard must never raise on a missing table
        db.session.rollback()
        return None


def worker_statuses():
    """Every known worker's heartbeat row, freshest first — for the dashboard."""
    try:
        return (
            DistributionWorkerHeartbeat.query.order_by(
                DistributionWorkerHeartbeat.last_poll_at.desc()
            ).all()
        )
    except Exception:  # noqa: BLE001
        db.session.rollback()
        return []


def _in_flight_batch(run_id):
    """The run's current unfinished batch (scheduled, queued, or running), if any."""
    return DistributionBatch.query.filter(
        DistributionBatch.payroll_run_id == run_id,
        DistributionBatch.status.in_(tuple(BATCH_PENDING_STATUSES)),
    ).first()


def enqueue_distribution(run, channel, only_failed, actor, scheduled_for=None):
    """Queue (or schedule) a distribution action for `run`. Returns a
    JSON-serialisable summary.

    A run only ever has one unfinished batch at a time — if one is already
    scheduled/queued/running, that batch is returned as-is rather than piling up
    a second one. A ``scheduled_for`` in the future creates a `scheduled` batch
    the worker activates when due; otherwise the batch is queued immediately."""
    existing = _in_flight_batch(run.id)
    if existing is not None:
        return {
            "batch_id": existing.id,
            "status": existing.status,
            "total": existing.total,
            "channel": existing.channel,
            "only_failed": existing.only_failed,
            "scheduled_for": existing.scheduled_for,
            "already_in_progress": True,
        }

    now = datetime.now(timezone.utc)
    is_scheduled = scheduled_for is not None and as_aware(scheduled_for) > now
    batch = DistributionBatch(
        payroll_run_id=run.id,
        client_company_id=run.client_company_id,
        channel=channel,
        only_failed=only_failed,
        status=BATCH_SCHEDULED if is_scheduled else BATCH_QUEUED,
        scheduled_for=scheduled_for if is_scheduled else None,
        initiated_by_user_id=getattr(actor, "id", None),
        initiated_by_role=getattr(actor, "role", None),
        total=len(run.items),
    )
    db.session.add(batch)
    if is_scheduled:
        record_audit(
            "Distribution scheduled",
            run,
            f"channel={channel} scheduled_for={as_aware(scheduled_for).isoformat()}.",
        )
    db.session.commit()
    return {
        "batch_id": batch.id,
        "status": batch.status,
        "total": batch.total,
        "channel": channel,
        "only_failed": only_failed,
        "scheduled_for": batch.scheduled_for,
        "already_in_progress": False,
    }


def reschedule_distribution(run, new_time, actor):
    """Change a scheduled batch's time (only while still `scheduled`). Returns a
    summary; ``ok`` is False when there is nothing rescheduleable."""
    batch = DistributionBatch.query.filter_by(
        payroll_run_id=run.id, status=BATCH_SCHEDULED
    ).first()
    if batch is None:
        return {"ok": False, "reason": "no_scheduled_batch"}
    now = datetime.now(timezone.utc)
    if new_time is None or as_aware(new_time) <= now:
        return {"ok": False, "reason": "not_future"}
    batch.scheduled_for = new_time
    record_audit(
        "Distribution rescheduled",
        run,
        f"scheduled_for={as_aware(new_time).isoformat()}.",
    )
    db.session.commit()
    return {"ok": True, "batch_id": batch.id, "scheduled_for": batch.scheduled_for}


def activate_due_scheduled():
    """Flip every scheduled batch whose time has arrived to `queued` so the normal
    claim path runs it. Returns the activated batches. Guards against duplicate
    execution: the scheduled->queued transition happens once (a subsequent
    activation no longer sees it as scheduled)."""
    candidates = DistributionBatch.query.filter(
        DistributionBatch.status == BATCH_SCHEDULED,
        DistributionBatch.scheduled_for.isnot(None),
    ).all()
    from .notify import notify_scheduled_started

    now = datetime.now(timezone.utc)
    due = [b for b in candidates if as_aware(b.scheduled_for) <= now]
    # Fetch the due batches' runs in one query rather than one get() per batch.
    runs_by_id = {}
    if due:
        run_ids = {b.payroll_run_id for b in due}
        runs_by_id = {
            r.id: r for r in PayrollRun.query.filter(PayrollRun.id.in_(run_ids)).all()
        }
    for batch in due:
        batch.status = BATCH_QUEUED
        run = runs_by_id.get(batch.payroll_run_id)
        record_audit(
            "Scheduled distribution activated",
            run,
            f"channel={batch.channel} was scheduled for "
            f"{as_aware(batch.scheduled_for).isoformat()}.",
        )
        notify_scheduled_started(batch, run)
    if due:
        db.session.commit()
    return due


def cancel_distribution(run, actor):
    """Cancel a run's not-yet-sent distribution work: a queued batch (before the
    worker claims it) and any pending automatic retries. Returns a summary.

    Guarantees:
      * An actively *running* batch is never cancelled — the operation is refused
        (blocked=True) so a send in progress is never interrupted mid-flight.
      * Already-`sent` deliveries are never touched.
      * Cancelled deliveries leave the retry pool (status flips to cancelled), so
        the worker's retry sweep skips them.

    Stages audit + a domain event; commits once."""
    running = DistributionBatch.query.filter_by(
        payroll_run_id=run.id, status=BATCH_RUNNING
    ).first()
    if running is not None:
        return {"cancelled_batch": False, "cancelled_retries": 0, "blocked": True}

    now = datetime.now(timezone.utc)
    # A scheduled OR queued batch is cancellable before it starts running.
    pending = DistributionBatch.query.filter(
        DistributionBatch.payroll_run_id == run.id,
        DistributionBatch.status.in_((BATCH_SCHEDULED, BATCH_QUEUED)),
    ).first()
    cancelled_batch = pending is not None
    if cancelled_batch:
        pending.status = BATCH_CANCELLED
        pending.finished_at = now

    pending_retries = PayslipDelivery.query.filter(
        PayslipDelivery.payroll_run_id == run.id,
        PayslipDelivery.status == DELIVERY_FAILED,
        PayslipDelivery.next_retry_at.isnot(None),
    ).all()
    for delivery in pending_retries:
        delivery.status = DELIVERY_CANCELLED
        delivery.next_retry_at = None

    if cancelled_batch or pending_retries:
        record_audit(
            "Distribution cancelled",
            run,
            f"queued send cancelled={cancelled_batch}, "
            f"pending retries stopped={len(pending_retries)}.",
        )
        from app.events import record_event

        record_event(
            "distribution.cancelled",
            summary=(
                f"{run.month} {run.year}: distribution cancelled "
                f"({'queued send stopped, ' if cancelled_batch else ''}"
                f"{len(pending_retries)} pending retr"
                f"{'y' if len(pending_retries) == 1 else 'ies'} stopped)."
            ),
            subject=run,
            client_company_id=run.client_company_id,
            level="warning",
        )
    db.session.commit()
    return {
        "cancelled_batch": cancelled_batch,
        "cancelled_retries": len(pending_retries),
        "blocked": False,
    }


def cancel_flash_message(result):
    """(message, category) for a cancel_distribution() result — shared by the
    operator and client cancel routes so the wording never drifts."""
    if result["blocked"]:
        return (
            "This distribution is already sending and can't be cancelled — "
            "wait for it to finish.",
            "warning",
        )
    if not result["cancelled_batch"] and not result["cancelled_retries"]:
        return ("Nothing to cancel — no queued send or pending retries for this run.", "info")
    parts = []
    if result["cancelled_batch"]:
        parts.append("queued send cancelled")
    if result["cancelled_retries"]:
        n = result["cancelled_retries"]
        parts.append(f"{n} pending retr{'y' if n == 1 else 'ies'} stopped")
    return (
        "Distribution cancelled: " + ", ".join(parts)
        + ". Already-sent payslips are unaffected.",
        "success",
    )


def claim_next_batch(worker_name=None):
    """Claim the oldest queued batch, marking it running. None if the queue is empty.

    Stamps the claiming worker's name so a stuck `running` batch can be attributed
    (and recovered — see reclaim_stale_batches) if that worker later dies."""
    query = DistributionBatch.query.filter_by(status=BATCH_QUEUED).order_by(
        DistributionBatch.created_at.asc()
    )
    if current_app.config.get("DATABASE_TYPE_LABEL") == "PostgreSQL":
        query = query.with_for_update(skip_locked=True)
    batch = query.first()
    if batch is None:
        return None
    batch.status = BATCH_RUNNING
    batch.started_at = datetime.now(timezone.utc)
    batch.claimed_by_worker = worker_name or default_worker_name()
    db.session.commit()
    return batch


def reclaim_stale_batches():
    """Recover batches stuck in `running` because the worker that claimed them died
    mid-send (crash, OOM, deploy kill) before committing a terminal status.

    A running batch whose `started_at` is older than DISTRIBUTION_BATCH_STALE_SECONDS
    is requeued so a worker retries it — A1's per-item delivery durability makes the
    retry idempotent (already-sent payslips are skipped, never re-sent). A batch
    reclaimed more than DISTRIBUTION_BATCH_MAX_RECLAIMS times is failed instead of
    looping forever (a poison batch). Returns the batches acted on.

    Keys on batch age rather than the worker heartbeat because a live worker does
    not heartbeat while inside a long send; the stale window is configured well
    above the longest plausible batch runtime so a busy worker is never reclaimed."""
    stale_seconds = current_app.config.get("DISTRIBUTION_BATCH_STALE_SECONDS", 900)
    max_reclaims = current_app.config.get("DISTRIBUTION_BATCH_MAX_RECLAIMS", 3)
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=stale_seconds)
    running = DistributionBatch.query.filter_by(status=BATCH_RUNNING).all()
    acted = []
    for batch in running:
        started = as_aware(batch.started_at)
        if started is None or started > cutoff:
            continue  # still within its expected runtime — leave it alone
        run = db.session.get(PayrollRun, batch.payroll_run_id)
        worker = batch.claimed_by_worker or "?"
        if (batch.reclaim_count or 0) >= max_reclaims:
            from .notify import notify_batch_failed

            batch.status = BATCH_FAILED
            batch.error = (
                f"Abandoned by worker '{worker}' and exceeded "
                f"{max_reclaims} reclaim attempts."
            )[:512]
            batch.finished_at = datetime.now(timezone.utc)
            record_audit(
                "Distribution batch abandoned",
                run,
                f"Batch {batch.id} failed after {max_reclaims} stale reclaims.",
            )
            notify_batch_failed(batch, run, batch.error)
        else:
            batch.reclaim_count = (batch.reclaim_count or 0) + 1
            batch.status = BATCH_QUEUED
            batch.started_at = None
            record_audit(
                "Distribution batch reclaimed",
                run,
                f"Batch {batch.id} was stuck running (worker '{worker}' presumed "
                f"dead); requeued (reclaim {batch.reclaim_count}).",
            )
        acted.append(batch)
    if acted:
        db.session.commit()
    return acted


def _notify_platform_of_client_distribution(batch, run, summary):
    """Tenant-initiated distributions notify Chrisnat oversight (unchanged from the
    previous synchronous behaviour, just deferred until the batch actually runs)."""
    initiator = (
        db.session.get(User, batch.initiated_by_user_id)
        if batch.initiated_by_user_id
        else None
    )
    if initiator is None or initiator.client_company_id is None:
        return
    from app.events import platform_admins, record_event

    record_event(
        "payslips.distributed",
        summary=(
            f"{run.month} {run.year}: {summary['sent']} sent, {summary['failed']} failed "
            f"(of {summary['total']}) via {batch.channel}."
        ),
        subject=run,
        client_company_id=run.client_company_id,
        level="info",
        payload={k: summary.get(k) for k in ("sent", "failed", "skipped", "total")},
        recipients=platform_admins(),
    )


def process_batch(batch):
    """Run `batch` (already claimed/running) to completion via distribute_run()."""
    from .notify import notify_batch_failed, notify_completion

    run = db.session.get(PayrollRun, batch.payroll_run_id)
    try:
        summary = distribute_run(
            run, channel=batch.channel, only_failed=batch.only_failed, batch_id=batch.id
        )
    except Exception as exc:  # noqa: BLE001 - one bad batch must not kill the worker loop
        db.session.rollback()
        batch.status = BATCH_FAILED
        batch.error = str(exc)[:500]
        batch.finished_at = datetime.now(timezone.utc)
        notify_batch_failed(batch, run, batch.error)
        db.session.commit()
        return batch

    batch.status = BATCH_COMPLETED
    batch.sent_count = summary["sent"]
    batch.failed_count = summary["failed"]
    batch.skipped_count = summary["skipped"]
    batch.finished_at = datetime.now(timezone.utc)
    _notify_platform_of_client_distribution(batch, run, summary)
    notify_completion(batch, run, summary)
    db.session.commit()
    return batch


def process_all_queued(worker_name=None):
    """Claim and process every currently queued batch, then return. No polling loop —
    used by tests and by the worker loop's inner step."""
    processed = []
    while True:
        batch = claim_next_batch(worker_name)
        if batch is None:
            return processed
        processed.append(process_batch(batch))


def process_due_retries():
    """Automatically re-attempt every failed delivery whose backoff has elapsed
    and whose retry limit is not yet spent (next_retry_at is set == retries
    remain). Returns the deliveries attempted. Groups by run so one audit entry
    is written per run, then commits once.

    Runs in the worker (no request context) and so intentionally spans tenants,
    exactly like the batch processor — tenant isolation gates *user* queries, not
    the platform worker."""
    candidates = PayslipDelivery.query.filter(
        PayslipDelivery.status == DELIVERY_FAILED,
        PayslipDelivery.next_retry_at.isnot(None),
    ).all()
    now = datetime.now(timezone.utc)
    due = [d for d in candidates if as_aware(d.next_retry_at) <= now]
    if not due:
        return []

    from .notify import notify_retry_exhausted

    by_run = {}
    for delivery in due:
        by_run.setdefault(delivery.payroll_run_id, []).append(delivery)

    processed = []
    for run_id, deliveries in by_run.items():
        run = db.session.get(PayrollRun, run_id)
        recovered = 0
        for delivery in deliveries:
            if retry_delivery(delivery):
                recovered += 1
            # Persist each retry outcome before the next attempt — same
            # crash-safety reason as the batch send loop: a mid-sweep crash must
            # not lose a delivery we just re-sent, or the next sweep resends it
            # (a duplicate).
            db.session.commit()
            processed.append(delivery)
        record_audit(
            "Payslip delivery auto-retry",
            run,
            f"retried {len(deliveries)}, recovered {recovered}.",
        )
        # Deliveries that just spent their last retry are a final failure —
        # surface them so an operator can intervene manually.
        exhausted = [
            d for d in deliveries
            if d.status == DELIVERY_FAILED and d.next_retry_at is None
        ]
        if exhausted:
            batch = (
                db.session.get(DistributionBatch, exhausted[0].distribution_batch_id)
                if exhausted[0].distribution_batch_id
                else None
            )
            notify_retry_exhausted(run, batch, len(exhausted))
        db.session.commit()  # persist the audit row + exhausted-notification state
    return processed


def drain_once(worker_name=None):
    """Run one full pass — activate due schedules, recover stuck batches, process
    the queue, run due retries — then return. For a cron-style deployment
    (`distribution-worker --once`) or tests. Returns True if anything was processed.

    Reclaim runs before the queue pass so a batch requeued this tick is picked up
    in the same drain."""
    activate_due_scheduled()
    reclaim_stale_batches()
    did = bool(process_all_queued(worker_name))
    did = bool(process_due_retries()) or did
    return did


def run_worker_loop(poll_interval=3, stop_event=None, worker_name=None):
    """Poll the queue forever, processing queued batches and due retries as they
    appear.

    `stop_event` (a threading.Event) lets a caller ask the loop to exit between
    polls (a graceful shutdown on SIGTERM); without one the loop runs until the
    process is killed. Each poll upserts this worker's heartbeat so the dashboard
    can see it — inline or a separate process.
    """
    from .sla import maybe_check_sla

    worker_name = worker_name or default_worker_name()
    while stop_event is None or not stop_event.is_set():
        record_heartbeat(worker_name, WORKER_STATUS_RUNNING)
        did_work = drain_once(worker_name)
        maybe_check_sla()  # throttled internally; alerts on new breaches
        if not did_work:
            if stop_event is not None:
                stop_event.wait(poll_interval)
            else:
                time.sleep(poll_interval)


def run_worker(poll_interval=3, stop_event=None, worker_name=None):
    """The worker entrypoint used in production: run_worker_loop wrapped so an
    unexpected crash alerts platform admins (Phase 3, Slice 8) before it
    propagates. A clean stop (stop_event / KeyboardInterrupt) is not an error and
    marks the worker's heartbeat stopped so the dashboard reflects the shutdown."""
    worker_name = worker_name or default_worker_name()
    try:
        run_worker_loop(poll_interval=poll_interval, stop_event=stop_event, worker_name=worker_name)
    except KeyboardInterrupt:
        _mark_worker_stopped(worker_name)
        raise
    except Exception as exc:  # noqa: BLE001 - notify, log, then re-raise
        from .notify import notify_worker_stopped

        try:
            db.session.rollback()
            notify_worker_stopped(str(exc))
            db.session.commit()
        except Exception:  # noqa: BLE001 - notification must never mask the crash
            db.session.rollback()
        current_app.logger.exception("Distribution worker stopped unexpectedly.")
        raise
    else:
        _mark_worker_stopped(worker_name)


def _mark_worker_stopped(worker_name):
    try:
        record_heartbeat(worker_name, WORKER_STATUS_STOPPED)
    except Exception:  # noqa: BLE001 - a shutdown must not fail on bookkeeping
        db.session.rollback()
