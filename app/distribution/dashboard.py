"""Monitoring dashboard aggregates for the distribution subsystem (Phase 3, Slice 4).

One function, :func:`collect_dashboard_stats`, gathers every metric the operator
monitoring view needs with a small, fixed number of aggregate queries (never one
query per batch/delivery). It reuses the existing DistributionBatch and
PayslipDelivery data — no new tables, no duplicated business logic.

This is a read-only, cross-tenant operational view (the operator watches every
tenant's distributions), mirroring how the worker itself spans tenants.
"""
from datetime import datetime, timezone

from flask import current_app
from sqlalchemy import func
from sqlalchemy.orm import joinedload

from app import db
from app.models import (
    BATCH_CANCELLED,
    BATCH_COMPLETED,
    BATCH_FAILED,
    BATCH_QUEUED,
    BATCH_RUNNING,
    DELIVERY_FAILED,
    DELIVERY_PENDING,
    DELIVERY_SENT,
    DistributionBatch,
    PayslipDelivery,
)


def _as_aware(dt):
    """Treat a stored naive datetime as UTC (SQLite drops tzinfo on write)."""
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _status_counts(model):
    """{status: count} for a model with a `status` column, in one grouped query."""
    rows = db.session.query(model.status, func.count()).group_by(model.status).all()
    return {status: count for status, count in rows}


def _pct(part, whole):
    return round(100 * part / whole, 1) if whole else 0.0


def _running_batch_progress(batch):
    """Best-effort live progress for a running batch. Because distribute_run
    commits a batch's deliveries as a unit, a running batch's per-item progress
    isn't visible mid-run; we surface elapsed time and the expected total so the
    operator still sees it working, plus an ETA once any progress is measurable."""
    started = _as_aware(batch.started_at)
    now = datetime.now(timezone.utc)
    elapsed = (now - started).total_seconds() if started else 0
    done = (batch.sent_count or 0) + (batch.failed_count or 0)
    total = batch.total or 0
    pct = _pct(done, total)
    eta = None
    if done and total and elapsed > 0 and done < total:
        rate = done / elapsed  # deliveries/sec
        if rate > 0:
            eta = int((total - done) / rate)
    return {
        "batch": batch,
        "elapsed_seconds": int(elapsed),
        "done": done,
        "total": total,
        "percent": pct,
        "eta_seconds": eta,
    }


def _throughput(completed_batches):
    """Average processing speed across recently completed batches:
    (payslips per minute, mean batch duration seconds)."""
    total_items = 0
    total_seconds = 0.0
    durations = []
    for batch in completed_batches:
        started = _as_aware(batch.started_at)
        finished = _as_aware(batch.finished_at)
        if not started or not finished:
            continue
        duration = (finished - started).total_seconds()
        if duration < 0:
            continue
        durations.append(duration)
        total_seconds += duration
        total_items += batch.total or 0
    per_minute = round(total_items / (total_seconds / 60), 1) if total_seconds else None
    avg_duration = round(sum(durations) / len(durations), 1) if durations else None
    return {"payslips_per_minute": per_minute, "avg_batch_seconds": avg_duration}


def _worker_health(batch_counts, backlog, last_processed_at, worker_last_poll):
    """Best-effort health signal. The inline worker publishes a heartbeat
    (worker_last_poll); an external worker process does not, so we fall back to
    the most recent processing timestamp. A backlog with stale activity is the
    stall signal."""
    now = datetime.now(timezone.utc)
    heartbeat = _as_aware(worker_last_poll) or _as_aware(last_processed_at)
    age = (now - heartbeat).total_seconds() if heartbeat else None
    has_backlog = (
        backlog["queued_batches"] > 0
        or batch_counts.get(BATCH_RUNNING, 0) > 0
        or backlog["due_retries"] > 0
    )
    # Stalled: work is waiting but nothing has been processed recently.
    if has_backlog and (age is None or age > 120):
        status = "stalled"
    elif has_backlog:
        status = "active"
    else:
        status = "idle"
    return {"status": status, "heartbeat_age_seconds": int(age) if age is not None else None}


def collect_dashboard_stats(recent_limit=10):
    """Every metric the monitoring dashboard needs, in one call."""
    from app.distribution.queue import worker_last_poll  # avoid import cycle

    batch_counts = _status_counts(DistributionBatch)
    delivery_counts = _status_counts(PayslipDelivery)

    sent = delivery_counts.get(DELIVERY_SENT, 0)
    failed = delivery_counts.get(DELIVERY_FAILED, 0)
    pending = delivery_counts.get(DELIVERY_PENDING, 0)
    attempted = sent + failed

    # Active retries vs final failures (one extra count each).
    active_retries = PayslipDelivery.query.filter(
        PayslipDelivery.status == DELIVERY_FAILED,
        PayslipDelivery.next_retry_at.isnot(None),
    ).count()
    final_failures = failed - active_retries

    queued_batches = batch_counts.get(BATCH_QUEUED, 0)
    backlog_payslips = (
        db.session.query(func.coalesce(func.sum(DistributionBatch.total), 0))
        .filter(DistributionBatch.status == BATCH_QUEUED)
        .scalar()
        or 0
    )
    backlog = {
        "queued_batches": queued_batches,
        "queued_payslips": int(backlog_payslips),
        "due_retries": active_retries,
    }

    recent_batches = (
        DistributionBatch.query.options(
            joinedload(DistributionBatch.payroll_run),
            joinedload(DistributionBatch.client_company),
            joinedload(DistributionBatch.initiated_by),
        )
        .order_by(DistributionBatch.created_at.desc())
        .limit(recent_limit)
        .all()
    )
    running = (
        DistributionBatch.query.options(joinedload(DistributionBatch.payroll_run))
        .filter(DistributionBatch.status == BATCH_RUNNING)
        .order_by(DistributionBatch.started_at.asc())
        .all()
    )
    completed_recent = (
        DistributionBatch.query.filter(DistributionBatch.status == BATCH_COMPLETED)
        .order_by(DistributionBatch.finished_at.desc())
        .limit(50)
        .all()
    )

    last_processed_at = (
        db.session.query(func.max(DistributionBatch.finished_at)).scalar()
    )

    return {
        "batches": {
            "queued": batch_counts.get(BATCH_QUEUED, 0),
            "running": batch_counts.get(BATCH_RUNNING, 0),
            "completed": batch_counts.get(BATCH_COMPLETED, 0),
            "failed": batch_counts.get(BATCH_FAILED, 0),
            "cancelled": batch_counts.get(BATCH_CANCELLED, 0),
            "total": sum(batch_counts.values()),
        },
        "deliveries": {
            "sent": sent,
            "failed": failed,
            "pending": pending,
            "attempted": attempted,
            "active_retries": active_retries,
            "final_failures": max(0, final_failures),
            "success_rate": _pct(sent, attempted),
            "failure_rate": _pct(failed, attempted),
        },
        "backlog": backlog,
        "recent_batches": recent_batches,
        "running_batches": [_running_batch_progress(b) for b in running],
        "throughput": _throughput(completed_recent),
        "last_processed_at": last_processed_at,
        "worker": _worker_health(
            batch_counts, backlog, last_processed_at, worker_last_poll()
        ),
        "worker_inline": bool(current_app.config.get("DISTRIBUTION_WORKER_INLINE")),
    }
