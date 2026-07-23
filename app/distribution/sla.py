"""SLA monitoring + alert thresholds (Phase 4, Slice 6).

Evaluates delivery service levels against configured thresholds and reports
breaches: batches that took too long to run, a recent-window failure rate over
budget, and (opt-in) sent messages with no delivery receipt after too long. The
worker re-checks on a throttled cadence and alerts platform admins (once per
breach type per cooldown) via the existing notification system; the dashboard
shows the current SLA status. Read-only evaluation reusing existing data.
"""
from datetime import datetime, timedelta, timezone

from flask import current_app

from app import db
from app.models import (
    BATCH_QUEUED,
    BATCH_RUNNING,
    DELIVERY_FAILED,
    DELIVERY_SENT,
    DistributionBatch,
    PayslipDelivery,
)

from .service import as_aware

# Throttling state for the worker's periodic check (module-level: the worker is
# one process). Reset across a process restart is harmless (an ongoing breach may
# re-alert once).
_last_check_at = None
_last_alert_at = {}  # breach type -> datetime of last alert


def reset():
    """Clear check/alert throttling state (used by tests)."""
    global _last_check_at
    _last_check_at = None
    _last_alert_at.clear()


def _thresholds():
    cfg = current_app.config
    return {
        "batch_minutes": int(cfg.get("SLA_BATCH_MINUTES", 30) or 0),
        "failure_rate": float(cfg.get("SLA_FAILURE_RATE", 0.2) or 0),
        "min_volume": int(cfg.get("SLA_MIN_VOLUME", 20) or 0),
        "window_hours": int(cfg.get("SLA_WINDOW_HOURS", 24) or 0),
        "confirm_hours": int(cfg.get("SLA_DELIVERY_CONFIRM_HOURS", 0) or 0),
    }


def _batch_reference_time(batch):
    """When a batch's SLA clock started: for a running batch, when it started; for
    a queued batch, its scheduled time if it was scheduled, else its creation."""
    if batch.status == BATCH_RUNNING and batch.started_at:
        return as_aware(batch.started_at)
    return as_aware(batch.scheduled_for) or as_aware(batch.created_at)


def evaluate_sla():
    """Return {ok, breaches, thresholds}. Each breach is {type, detail, count}."""
    th = _thresholds()
    now = datetime.now(timezone.utc)
    breaches = []

    # 1. Overdue batches — queued/running past the completion budget.
    if th["batch_minutes"]:
        cutoff = timedelta(minutes=th["batch_minutes"])
        active = DistributionBatch.query.filter(
            DistributionBatch.status.in_((BATCH_QUEUED, BATCH_RUNNING))
        ).all()
        overdue = [
            b for b in active
            if (ref := _batch_reference_time(b)) is not None and (now - ref) > cutoff
        ]
        if overdue:
            breaches.append({
                "type": "batch_overdue",
                "count": len(overdue),
                "detail": (
                    f"{len(overdue)} distribution batch(es) have not finished within "
                    f"{th['batch_minutes']} min"
                ),
            })

    # 2. Recent-window failure rate over budget (with a minimum volume).
    if th["failure_rate"] and th["window_hours"]:
        since = now - timedelta(hours=th["window_hours"])
        recent = PayslipDelivery.query.filter(
            PayslipDelivery.status.in_((DELIVERY_SENT, DELIVERY_FAILED))
        ).all()
        recent = [d for d in recent if (as_aware(d.updated_at) or now) >= since]
        attempted = len(recent)
        failed = sum(1 for d in recent if d.status == DELIVERY_FAILED)
        if attempted >= th["min_volume"]:
            rate = failed / attempted
            if rate >= th["failure_rate"]:
                breaches.append({
                    "type": "failure_rate",
                    "count": failed,
                    "detail": (
                        f"failure rate {round(rate * 100, 1)}% over the last "
                        f"{th['window_hours']}h ({failed}/{attempted}) exceeds "
                        f"{round(th['failure_rate'] * 100, 1)}%"
                    ),
                })

    # 3. Sent-but-unconfirmed for too long (opt-in; needs delivery receipts).
    if th["confirm_hours"]:
        cutoff_time = now - timedelta(hours=th["confirm_hours"])
        candidates = PayslipDelivery.query.filter(
            PayslipDelivery.status == DELIVERY_SENT,
            PayslipDelivery.provider_message_id.isnot(None),
            PayslipDelivery.provider_status.is_(None),
        ).all()
        unconfirmed = [
            d for d in candidates
            if (as_aware(d.sent_at) or now) < cutoff_time
        ]
        if unconfirmed:
            breaches.append({
                "type": "unconfirmed",
                "count": len(unconfirmed),
                "detail": (
                    f"{len(unconfirmed)} sent message(s) unconfirmed after "
                    f"{th['confirm_hours']}h"
                ),
            })

    return {"ok": not breaches, "breaches": breaches, "thresholds": th}


def maybe_check_sla():
    """Throttled SLA check for the worker loop: evaluate at most once per
    SLA_CHECK_INTERVAL_SECONDS, and alert platform admins about breaches not
    alerted within SLA_ALERT_COOLDOWN_SECONDS. Returns the evaluation (or None if
    skipped this poll)."""
    global _last_check_at
    now = datetime.now(timezone.utc)
    interval = int(current_app.config.get("SLA_CHECK_INTERVAL_SECONDS", 300) or 0)
    if _last_check_at is not None and (now - _last_check_at).total_seconds() < interval:
        return None
    _last_check_at = now

    result = evaluate_sla()
    if not result["breaches"]:
        return result

    cooldown = int(current_app.config.get("SLA_ALERT_COOLDOWN_SECONDS", 3600) or 0)
    fresh = []
    for breach in result["breaches"]:
        last = _last_alert_at.get(breach["type"])
        if last is None or (now - last).total_seconds() >= cooldown:
            fresh.append(breach)
            _last_alert_at[breach["type"]] = now
    if fresh:
        from .notify import notify_sla_breach

        notify_sla_breach(fresh)
        db.session.commit()
    return result
