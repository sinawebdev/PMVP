"""Distribute a payroll run's payslips over a channel, recording every attempt.

Mirrors the standalone distribution system's send_period(): one bad recipient becomes a
recorded `failed` PayslipDelivery, never an exception that aborts the run. Already-`sent`
items are skipped so re-running is safe; only_failed re-attempts just the failures.
"""
from datetime import datetime, timedelta, timezone

from flask import current_app

from app import db
from app.audit import record_audit
from app.models import (
    CHANNEL_AUTO,
    CHANNEL_EMAIL,
    DELIVERY_CHANNELS,
    DELIVERY_FAILED,
    DELIVERY_SENT,
    Employee,
    PayrollItem,
    PayrollRun,
    PayslipDelivery,
)
from app.raw_import import normalise_emp_id

from .channels import OutboundMessage, get_sender
from .render import render_payslip_email, render_payslip_text
from .tokens import public_payslip_url


def _retry_config():
    """(max_attempts, backoff_base_seconds) — falls back to sane defaults outside
    an app context (defensive; the worker and routes always have one)."""
    try:
        return (
            int(current_app.config.get("DISTRIBUTION_MAX_ATTEMPTS", 3)),
            int(current_app.config.get("DISTRIBUTION_RETRY_BACKOFF_SECONDS", 60)),
        )
    except RuntimeError:  # no app context
        return 3, 60


def _mark_sent(delivery, provider):
    delivery.status = DELIVERY_SENT
    delivery.provider = provider
    delivery.error = None
    delivery.sent_at = datetime.now(timezone.utc)
    delivery.next_retry_at = None


def _mark_failed(delivery, error, *, provider=None, max_attempts=None, backoff_base=None):
    """Record a failed attempt and schedule the next automatic retry — unless the
    retry limit is reached, in which case next_retry_at is left NULL (final
    failure). `delivery.attempts` must already be incremented by the caller."""
    if max_attempts is None or backoff_base is None:
        max_attempts, backoff_base = _retry_config()
    delivery.status = DELIVERY_FAILED
    delivery.error = error
    delivery.provider = provider
    attempts = delivery.attempts or 1
    if attempts < max_attempts:
        delay = backoff_base * (2 ** (attempts - 1))
        delivery.next_retry_at = datetime.now(timezone.utc) + timedelta(seconds=delay)
    else:
        delivery.next_retry_at = None


def retry_state(delivery):
    """UI-facing view of a delivery's retry position: how many attempts so far,
    how many automatic retries remain, and whether it is a final failure."""
    max_attempts, _ = _retry_config()
    attempts = delivery.attempts or 0
    is_failed = delivery.status == DELIVERY_FAILED
    return {
        "attempts": attempts,
        "max_attempts": max_attempts,
        "remaining": max(0, max_attempts - attempts) if is_failed else 0,
        # No retry scheduled on a failed delivery == the limit is spent.
        "final": is_failed and delivery.next_retry_at is None,
        "will_retry": is_failed and delivery.next_retry_at is not None,
        "next_retry_at": delivery.next_retry_at,
    }


def get_distribution_contact(client_company_id, employee_id_str):
    """Return ``{'email', 'phone', 'name'}`` for an employee.

    Always reads from the client's active Employee roster — never from payroll upload
    data. Inactive or unregistered employees yield no contact, so distribution skips
    them rather than falling back to whatever was in the spreadsheet.
    """
    norm_id = normalise_emp_id(employee_id_str)
    emp = Employee.query.filter_by(
        client_company_id=client_company_id, staff_id=norm_id, status="Active"
    ).first()
    if not emp:
        return {"email": None, "phone": None, "name": employee_id_str}
    return {"email": emp.email, "phone": emp.phone, "name": emp.full_name}


def _roster_employee(item):
    """The roster Employee behind this payroll item.

    The item's own employee relationship (set at import time) is preferred —
    and returned regardless of roster status, because a worker deactivated
    after payday still needs the payslip for work already done. Only items
    that never got linked fall back to an active-roster lookup by normalised
    staff_id."""
    employee = getattr(item, "employee", None)
    if employee is not None:
        return employee
    run = getattr(item, "payroll_run", None)
    client_id = run.client_company_id if run else None
    if not client_id or not item.staff_id:
        return None
    return Employee.query.filter_by(
        client_company_id=client_id,
        staff_id=normalise_emp_id(item.staff_id),
        status="Active",
    ).first()


def _contact_for(channel, item):
    """The address an item is reachable at on a channel.

    The active roster record is authoritative when it has a contact, but the
    payroll item's own momo/email is a real fallback, not noise: reps can edit
    momo_number directly on the payroll row, and a worker deactivated (or not
    yet registered) on the roster after payday still has to be able to receive
    the payslip for work already done."""
    employee = _roster_employee(item)
    if channel == CHANNEL_EMAIL:
        roster_contact = employee.email if employee else None
        return roster_contact or item.email
    # sms / whatsapp -> a phone number (roster phone preferred, then momo,
    # then the momo captured on the payroll row itself)
    roster_contact = (employee.phone or employee.momo_number) if employee else None
    return roster_contact or item.momo_number


def resolve_channel(item, default_pref=None):
    """Pick the channel for an item: the roster employee's preference first, then the
    remaining channels in order, choosing the first with a usable contact."""
    employee = _roster_employee(item)
    pref = (employee.preferred_channel if employee else None) or default_pref
    order = ([pref] if pref else []) + [c for c in DELIVERY_CHANNELS if c != pref]
    for channel in order:
        if _contact_for(channel, item):
            return channel
    return pref or DELIVERY_CHANNELS[0]


def _latest_delivery(item, channel):
    return (
        PayslipDelivery.query.filter_by(payroll_item_id=item.id, channel=channel)
        .order_by(PayslipDelivery.created_at.desc())
        .first()
    )


def _build_message(channel, item, run, client, recipient):
    link = public_payslip_url(item.id)
    if channel == CHANNEL_EMAIL:
        subject, text, html = render_payslip_email(item, run, client, link=link)
        return OutboundMessage(channel, recipient, subject, text, html)
    text = render_payslip_text(item, run, client, link=link)
    return OutboundMessage(channel, recipient, f"Payslip {run.month} {run.year}".strip(), text)


def _attempt_send(delivery, item, run, client, ch, sender, max_attempts, backoff_base):
    """Send one payslip for `delivery` and record the outcome (attempt count,
    status, error, retry schedule). The single place a delivery attempt is made,
    reused by both the batch send loop and the automatic-retry path. Returns True
    on success. Does NOT commit."""
    recipient = _contact_for(ch, item)
    delivery.channel = ch
    delivery.recipient = recipient
    delivery.attempts = (delivery.attempts or 0) + 1

    if not recipient:
        _mark_failed(
            delivery, f"no contact on roster for {ch}",
            provider=None, max_attempts=max_attempts, backoff_base=backoff_base,
        )
        return False

    result = sender.send(_build_message(ch, item, run, client, recipient))
    if result.ok:
        _mark_sent(delivery, result.provider)
        return True
    _mark_failed(
        delivery, result.error,
        provider=result.provider, max_attempts=max_attempts, backoff_base=backoff_base,
    )
    return False


def distribute_run(run, channel=CHANNEL_AUTO, only_failed=False, batch_id=None):
    """Render + send every payslip in `run`. Returns a summary dict. Commits once.

    `batch_id` (the DistributionBatch driving this send) is stamped onto every
    delivery touched, so history can attribute a delivery to the initiating
    operator and filter by batch."""
    client = run.client_company
    auto = channel == CHANNEL_AUTO
    max_attempts, backoff_base = _retry_config()
    senders = {}

    def sender_for(ch):
        if ch not in senders:
            senders[ch] = get_sender(ch)
        return senders[ch]

    summary = {"total": 0, "sent": 0, "failed": 0, "skipped": 0, "failed_workers": []}

    for item in run.items:
        summary["total"] += 1
        ch = resolve_channel(item) if auto else channel
        existing = _latest_delivery(item, ch)

        if only_failed:
            if existing is None or existing.status != DELIVERY_FAILED:
                summary["skipped"] += 1
                continue
            delivery = existing
        else:
            if existing is not None and existing.status == DELIVERY_SENT:
                summary["skipped"] += 1
                continue
            delivery = existing or PayslipDelivery(
                payroll_item_id=item.id, payroll_run_id=run.id, channel=ch
            )
            if existing is None:
                db.session.add(delivery)

        if batch_id is not None:
            delivery.distribution_batch_id = batch_id
        if _attempt_send(delivery, item, run, client, ch, sender_for(ch),
                         max_attempts, backoff_base):
            summary["sent"] += 1
        else:
            summary["failed"] += 1
            summary["failed_workers"].append(item.staff_id or str(item.id))

    record_audit(
        "Payslips distributed" if not only_failed else "Failed payslips resent",
        run,
        f"channel={channel} sent={summary['sent']} failed={summary['failed']} "
        f"skipped={summary['skipped']} of {summary['total']}.",
    )
    db.session.commit()
    return summary


def retry_delivery(delivery):
    """Re-attempt a single failed delivery in place (no new row, same channel).
    Reuses the roster contact fresh, so a fixed roster is picked up. Does NOT
    commit — the caller (the retry sweep) owns the transaction."""
    if delivery.status == DELIVERY_SENT:
        return False  # never resend a success
    item = db.session.get(PayrollItem, delivery.payroll_item_id)
    run = db.session.get(PayrollRun, delivery.payroll_run_id)
    if item is None or run is None:
        return False
    max_attempts, backoff_base = _retry_config()
    return _attempt_send(
        delivery, item, run, run.client_company, delivery.channel,
        get_sender(delivery.channel), max_attempts, backoff_base,
    )
