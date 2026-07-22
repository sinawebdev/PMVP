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
import time
from datetime import datetime, timezone

from flask import current_app

from app import db
from app.audit import record_audit
from app.models import (
    BATCH_COMPLETED,
    BATCH_FAILED,
    BATCH_QUEUED,
    BATCH_RUNNING,
    DELIVERY_FAILED,
    DistributionBatch,
    PayrollRun,
    PayslipDelivery,
    User,
)

from .service import distribute_run, retry_delivery


def _in_flight_batch(run_id):
    return DistributionBatch.query.filter(
        DistributionBatch.payroll_run_id == run_id,
        DistributionBatch.status.in_((BATCH_QUEUED, BATCH_RUNNING)),
    ).first()


def enqueue_distribution(run, channel, only_failed, actor):
    """Queue a distribution action for `run`. Returns a JSON-serialisable summary.

    A run only ever has one unfinished batch at a time — if one is already
    queued/running, that batch is returned as-is rather than piling up a second
    one (the worker processes one batch per run at a time anyway; queuing a
    second just confuses "latest batch" in the UI for no benefit)."""
    existing = _in_flight_batch(run.id)
    if existing is not None:
        return {
            "batch_id": existing.id,
            "status": existing.status,
            "total": existing.total,
            "channel": existing.channel,
            "only_failed": existing.only_failed,
            "already_in_progress": True,
        }

    batch = DistributionBatch(
        payroll_run_id=run.id,
        client_company_id=run.client_company_id,
        channel=channel,
        only_failed=only_failed,
        status=BATCH_QUEUED,
        initiated_by_user_id=getattr(actor, "id", None),
        initiated_by_role=getattr(actor, "role", None),
        total=len(run.items),
    )
    db.session.add(batch)
    db.session.commit()
    return {
        "batch_id": batch.id,
        "status": batch.status,
        "total": batch.total,
        "channel": channel,
        "only_failed": only_failed,
        "already_in_progress": False,
    }


def claim_next_batch():
    """Claim the oldest queued batch, marking it running. None if the queue is empty."""
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
    db.session.commit()
    return batch


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
    run = db.session.get(PayrollRun, batch.payroll_run_id)
    try:
        summary = distribute_run(run, channel=batch.channel, only_failed=batch.only_failed)
    except Exception as exc:  # noqa: BLE001 - one bad batch must not kill the worker loop
        db.session.rollback()
        batch.status = BATCH_FAILED
        batch.error = str(exc)[:500]
        batch.finished_at = datetime.now(timezone.utc)
        db.session.commit()
        return batch

    batch.status = BATCH_COMPLETED
    batch.sent_count = summary["sent"]
    batch.failed_count = summary["failed"]
    batch.skipped_count = summary["skipped"]
    batch.finished_at = datetime.now(timezone.utc)
    _notify_platform_of_client_distribution(batch, run, summary)
    db.session.commit()
    return batch


def process_all_queued():
    """Claim and process every currently queued batch, then return. No polling loop —
    used by tests and by the worker loop's inner step."""
    processed = []
    while True:
        batch = claim_next_batch()
        if batch is None:
            return processed
        processed.append(process_batch(batch))


def _as_aware(dt):
    """Treat a stored naive datetime as UTC (SQLite drops tzinfo on write)."""
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


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
    due = [d for d in candidates if _as_aware(d.next_retry_at) <= now]
    if not due:
        return []

    by_run = {}
    for delivery in due:
        by_run.setdefault(delivery.payroll_run_id, []).append(delivery)

    processed = []
    for run_id, deliveries in by_run.items():
        run = db.session.get(PayrollRun, run_id)
        recovered = sum(1 for d in deliveries if retry_delivery(d))
        processed.extend(deliveries)
        record_audit(
            "Payslip delivery auto-retry",
            run,
            f"retried {len(deliveries)}, recovered {recovered}.",
        )
    db.session.commit()
    return processed


def run_worker_loop(poll_interval=3, stop_event=None):
    """Poll the queue forever, processing queued batches and due retries as they
    appear.

    `stop_event` (a threading.Event) lets a caller ask the loop to exit between
    polls; without one the loop runs until the process is killed.
    """
    while stop_event is None or not stop_event.is_set():
        did_work = bool(process_all_queued())
        did_work = bool(process_due_retries()) or did_work
        if not did_work:
            if stop_event is not None:
                stop_event.wait(poll_interval)
            else:
                time.sleep(poll_interval)
