"""Distribution lifecycle notifications (Phase 3, Slice 8).

Thin helpers over the existing app.events.record_event — the one notification
subsystem. Each function decides *what* happened and *who* hears about it (the
initiating operator, and/or platform admins), then stages a DomainEvent +
per-user Notifications. Callers own the transaction (the worker commits once),
matching how record_event/record_audit already work.

No new inbox, model, or delivery channel: these land in the same per-user
in-app Notification inbox everything else uses, and — being DomainEvents about a
run — they also show up in that run's activity timeline for free.
"""
from flask import current_app

from app import db
from app.events import platform_admins, record_event
from app.models import User


def _initiator(batch):
    if batch is None or batch.initiated_by_user_id is None:
        return None
    return db.session.get(User, batch.initiated_by_user_id)


def _recipients(*users):
    """Flatten Users/lists into a de-duplicated recipient list (Nones dropped)."""
    out = []
    for u in users:
        if u is None:
            continue
        out.extend(u if isinstance(u, (list, tuple)) else [u])
    return out


def notify_completion(batch, run, summary):
    """A batch finished: tell the initiator whether it fully/partially/entirely
    failed, and alert platform admins when the failure rate is high."""
    total = summary.get("total", 0) or 0
    failed = summary.get("failed", 0) or 0
    sent = summary.get("sent", 0) or 0
    initiator = _initiator(batch)

    if failed == 0:
        event_type, level, headline = "distribution.completed", "success", "completed"
    elif total and failed >= total:
        event_type, level, headline = "distribution.failed", "warning", "failed"
    else:
        event_type, level, headline = "distribution.partial", "warning", "partially completed"

    summary_text = (
        f"{run.month} {run.year}: distribution {headline} — "
        f"{sent} sent, {failed} failed of {total}."
    )
    payload = {k: summary.get(k) for k in ("sent", "failed", "skipped", "total")}

    recipients = _recipients(initiator)
    # High failure rate escalates to platform oversight.
    threshold = current_app.config.get("DISTRIBUTION_FAILURE_ALERT_RATE", 0.5)
    if total and (failed / total) >= threshold and failed > 0:
        recipients = _recipients(initiator, platform_admins())
        level = "warning"

    record_event(
        event_type,
        summary=summary_text,
        subject=run,
        client_company_id=run.client_company_id,
        level=level,
        payload=payload,
        recipients=recipients,
    )


def notify_batch_failed(batch, run, error):
    """The batch itself errored out (not per-delivery failures) — tell the
    initiator and platform admins."""
    record_event(
        "distribution.batch_failed",
        summary=f"{run.month} {run.year}: distribution batch failed — {error}",
        subject=run,
        client_company_id=run.client_company_id,
        level="warning",
        recipients=_recipients(_initiator(batch), platform_admins()),
    )


def notify_retry_exhausted(run, batch, exhausted_count):
    """One or more deliveries hit the retry limit and will not be retried again."""
    if not exhausted_count:
        return
    record_event(
        "distribution.retry_exhausted",
        summary=(
            f"{run.month} {run.year}: {exhausted_count} payslip"
            f"{'' if exhausted_count == 1 else 's'} exhausted all retries and "
            "need manual attention."
        ),
        subject=run,
        client_company_id=run.client_company_id,
        level="warning",
        recipients=_recipients(_initiator(batch), platform_admins()),
    )


def notify_scheduled_started(batch, run):
    """A scheduled distribution just activated — tell the operator who set it."""
    record_event(
        "distribution.scheduled_started",
        summary=f"{run.month} {run.year}: scheduled distribution has started.",
        subject=run,
        client_company_id=run.client_company_id,
        level="info",
        recipients=_recipients(_initiator(batch)),
    )


def notify_worker_stopped(error):
    """The worker loop exited unexpectedly — alert platform admins. Stages a
    DomainEvent + notifications; the caller commits."""
    record_event(
        "distribution.worker_stopped",
        summary=f"The payslip distribution worker stopped unexpectedly: {error}",
        subject=None,
        level="warning",
        recipients=platform_admins(),
    )
