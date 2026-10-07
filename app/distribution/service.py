"""Distribute a payroll run's payslips over a channel, recording every attempt.

Mirrors the standalone distribution system's send_period(): one bad recipient becomes a
recorded `failed` PayslipDelivery, never an exception that aborts the run. Sent, in-flight
and unconfirmed items are skipped so re-running is safe; only_failed re-attempts just the
failures.

Every attempt is claimed before the provider is called (:func:`claim_delivery`): a
conditional UPDATE moves the row to `sending`, and only the caller whose UPDATE changed
the row goes on to send. That is what stops a batch, a "Resend failed" and the retry
sweep from sending one payslip twice. The outcome is then written as `sent`, `failed`
(retried automatically only when the failure is retryable) or `unknown` (the provider
may have accepted it, so it is never re-sent automatically).
"""
from datetime import datetime, timedelta, timezone

from flask import current_app
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError

from app import db
from app.audit import record_audit
from app.models import (
    CHANNEL_AUTO,
    CHANNEL_EMAIL,
    CHANNEL_SMS,
    DELIVERY_CANCELLED,
    DELIVERY_FAILED,
    DELIVERY_PENDING,
    DELIVERY_SENDING,
    DELIVERY_SENT,
    DELIVERY_UNKNOWN,
    PayrollItem,
    PayrollRun,
    PayslipDelivery,
)

from .channels import OutboundMessage, get_sender, sendable_channels
from .contacts import SOURCE_INVALID, roster_employee, sms_contact
from .links import short_payslip_url
from .render import (
    SMS_TEMPLATE_VERSION,
    render_payslip_email,
    render_payslip_sms,
    render_payslip_text,
)
from .tokens import public_payslip_url

# A new send may claim a row that has never gone out (pending, failed, or
# cancelled before it went). A retry, manual or automatic, claims failures only.
# `sent`, `sending` and `unknown` are never claimable by anything here.
CLAIMABLE_FOR_SEND = (DELIVERY_PENDING, DELIVERY_FAILED, DELIVERY_CANCELLED)
CLAIMABLE_FOR_RETRY = (DELIVERY_FAILED,)
SKIPPED_BY_SEND = (DELIVERY_SENT, DELIVERY_SENDING, DELIVERY_UNKNOWN)


def as_aware(dt):
    """Treat a stored naive datetime as UTC (SQLite drops tzinfo on write), so
    comparisons against an aware ``datetime.now(timezone.utc)`` are correct on
    both SQLite and PostgreSQL. The one place this normalisation lives."""
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


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


def _mark_sent(delivery, provider, message_id=None, units=None):
    delivery.status = DELIVERY_SENT
    delivery.provider = provider
    delivery.error = None
    delivery.sent_at = datetime.now(timezone.utc)
    delivery.next_retry_at = None
    delivery.units = units
    if message_id:
        delivery.provider_message_id = message_id
        # A fresh send supersedes any prior receipt state.
        delivery.provider_status = None
        delivery.delivered_at = None


def _mark_failed(delivery, error, *, provider=None, max_attempts=None, backoff_base=None,
                 retry=True):
    """Record a failed attempt and, when ``retry`` is True, schedule the next
    automatic retry — unless the retry limit is reached, in which case
    next_retry_at is left NULL (final failure). `delivery.attempts` must already
    count this attempt. ``retry=False`` is a permanent failure: retrying the same
    thing cannot work (no contact, a refusal), or must not happen without a
    person deciding (a failure reported after the send)."""
    if max_attempts is None or backoff_base is None:
        max_attempts, backoff_base = _retry_config()
    delivery.status = DELIVERY_FAILED
    delivery.error = error
    delivery.provider = provider
    attempts = delivery.attempts or 1
    if retry and attempts < max_attempts:
        delay = backoff_base * (2 ** (attempts - 1))
        delivery.next_retry_at = datetime.now(timezone.utc) + timedelta(seconds=delay)
    else:
        delivery.next_retry_at = None


def _mark_unknown(delivery, error, *, provider=None):
    """The provider may have accepted it. Never retried automatically and never
    picked up by "Resend failed"; a delivery report or an operator settles it."""
    delivery.status = DELIVERY_UNKNOWN
    delivery.error = error
    delivery.provider = provider
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


def _contact_for(channel, item):
    """The address an item is reachable at on a channel.

    The active roster record is authoritative when it has a contact, but the
    payroll item's own momo/email is a real fallback, not noise: reps can edit
    momo_number directly on the payroll row, and a worker deactivated (or not
    yet registered) on the roster after payday still has to be able to receive
    the payslip for work already done."""
    employee = roster_employee(item)
    if channel == CHANNEL_EMAIL:
        roster_contact = employee.email if employee else None
        return roster_contact or item.email
    if channel == CHANNEL_SMS:
        # Only a number normalise_gh_mobile accepts, already normalised.
        return sms_contact(item, employee)[0]
    # whatsapp (on hold, unchanged) -> roster phone, then momo, then the momo
    # captured on the payroll row itself
    roster_contact = (employee.phone or employee.momo_number) if employee else None
    return roster_contact or item.momo_number


def _recipient_for(channel, item):
    """``(recipient, None)``, or ``(None, why there is none)``. Both reasons are
    permanent: a retry cannot invent a contact, so neither is retried
    automatically."""
    if channel == CHANNEL_SMS:
        number, source = sms_contact(item)
        if number:
            return number, None
        if source == SOURCE_INVALID:
            return None, "invalid number on roster for sms"
        return None, "no contact on roster for sms"
    recipient = _contact_for(channel, item)
    return (recipient, None) if recipient else (None, f"no contact on roster for {channel}")


def resolve_channel(item, default_pref=None):
    """Pick the channel for an item: the roster employee's preference first, then the
    remaining channels in order, choosing the first with a usable contact. Only
    channels this deployment may send on are considered (no SMS on desktop)."""
    employee = roster_employee(item)
    channels = sendable_channels()
    pref = (employee.preferred_channel if employee else None) or default_pref
    if pref not in channels:
        pref = None
    order = ([pref] if pref else []) + [c for c in channels if c != pref]
    for channel in order:
        if _contact_for(channel, item):
            return channel
    return pref or channels[0]


def _latest_delivery(item, channel):
    return (
        PayslipDelivery.query.filter_by(payroll_item_id=item.id, channel=channel)
        .order_by(PayslipDelivery.created_at.desc())
        .first()
    )


def _payslip_pdf_attachment(item):
    """Build a validated PDF attachment for an email payslip, or None. Never
    raises: a generation/validation failure logs and falls back to link-only, so
    a bad attachment never blocks the email (Phase 3, Slice 9)."""
    from .channels import Attachment

    try:
        from app.pdf_service import generate_payslip_pdf, payslip_filename

        path = generate_payslip_pdf(item, current_app.config["EXPORT_FOLDER"])
        with open(path, "rb") as fh:
            content = fh.read()
        max_bytes = current_app.config.get("EMAIL_MAX_ATTACHMENT_BYTES", 5 * 1024 * 1024)
        if not content or len(content) > max_bytes:
            current_app.logger.warning(
                "[email] payslip PDF for item %s failed validation (%d bytes) — sending link only",
                item.id, len(content),
            )
            return None
        try:
            filename = payslip_filename(item)
        except Exception:  # noqa: BLE001 - filename helper is best-effort
            filename = f"payslip-{item.id}.pdf"
        return Attachment(filename=filename, content=content, mimetype="application/pdf")
    except Exception as exc:  # noqa: BLE001 - never let attachment build abort a send
        current_app.logger.warning(
            "[email] could not attach payslip PDF for item %s: %s — sending link only",
            item.id, exc,
        )
        return None


def _build_message(channel, item, run, client, recipient, delivery=None):
    item_id = getattr(item, "id", None)
    delivery_id = getattr(delivery, "id", None)
    subject = f"Payslip {run.month} {run.year}".strip()
    if channel == CHANNEL_SMS:
        # Link only, one GSM-7 part, carrying a short /s/ code minted for this
        # attempt. The wording's version is stored; the body never is.
        text = render_payslip_sms(run, client, short_payslip_url(item, delivery))
        if delivery is not None:
            delivery.template_version = SMS_TEMPLATE_VERSION
        return OutboundMessage(channel, recipient, subject, text,
                               item_id=item_id, delivery_id=delivery_id)
    # The item, not item.id: the token embeds the item's revocation counter, and
    # passing the loaded row lets tokens.py read it without a second query.
    link = public_payslip_url(item)
    if channel == CHANNEL_EMAIL:
        subject, text, html = render_payslip_email(item, run, client, link=link)
        attachments = []
        if current_app.config.get("EMAIL_ATTACH_PAYSLIP_PDF"):
            attachment = _payslip_pdf_attachment(item)
            if attachment is not None:
                attachments.append(attachment)
        # Tenant branding pack: per-message From name / Reply-To (fall back to config).
        return OutboundMessage(
            channel, recipient, subject, text, html, attachments=attachments,
            from_name=getattr(client, "email_from_name", None) if client else None,
            reply_to=getattr(client, "email_reply_to", None) if client else None,
            item_id=item_id, delivery_id=delivery_id,
        )
    text = render_payslip_text(item, run, client, link=link)
    return OutboundMessage(channel, recipient, subject, text,
                           item_id=item_id, delivery_id=delivery_id)


def claim_delivery(delivery_id, claimable, *, batch_id=None, require_scheduled_retry=False):
    """Move one delivery to `sending` if it is still in a ``claimable`` status,
    count the attempt, and commit. True for exactly one caller.

    The UPDATE is conditional on the status the row has *in the database*, not
    the one this process last read, so two workers that both saw a row as
    `failed` cannot both change it: the second UPDATE matches nothing. Same on
    SQLite and PostgreSQL. ``require_scheduled_retry`` is the automatic sweep's
    extra condition, so it never re-sends a failure that has become permanent
    since it was selected."""
    conditions = [PayslipDelivery.id == delivery_id, PayslipDelivery.status.in_(claimable)]
    if require_scheduled_retry:
        conditions.append(PayslipDelivery.next_retry_at.isnot(None))
    values = {
        "status": DELIVERY_SENDING,
        "claimed_at": datetime.now(timezone.utc),
        "attempts": PayslipDelivery.attempts + 1,
    }
    if batch_id is not None:
        values["distribution_batch_id"] = batch_id
    result = db.session.execute(
        update(PayslipDelivery).where(*conditions).values(**values)
        .execution_options(synchronize_session=False)
    )
    db.session.commit()
    return result.rowcount == 1


def _insert_pending(item, run, channel):
    """A new `pending` row for (item, channel), committed so it can be claimed.
    If another worker inserted the same row first, the unique (item, channel)
    constraint refuses this one; theirs is returned and the claim decides who
    sends."""
    delivery = PayslipDelivery(
        payroll_item_id=item.id, payroll_run_id=run.id, channel=channel,
        status=DELIVERY_PENDING, attempts=0,
    )
    db.session.add(delivery)
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        return _latest_delivery(item, channel)
    return delivery


def _record_outcome(delivery, result, max_attempts, backoff_base):
    if result.ok:
        _mark_sent(delivery, result.provider, message_id=result.message_id, units=result.units)
    elif result.ambiguous:
        _mark_unknown(delivery, result.error, provider=result.provider)
    else:
        # retryable=None is a sender that does not classify its failures yet
        # (Hubtel, WhatsApp, SMTP): it keeps the old rule of retrying them all.
        _mark_failed(
            delivery, result.error, provider=result.provider, retry=result.retryable is not False,
            max_attempts=max_attempts, backoff_base=backoff_base,
        )


def _attempt_send(delivery, item, run, client, ch, sender, max_attempts, backoff_base, *,
                  claimable=CLAIMABLE_FOR_SEND, batch_id=None, require_scheduled_retry=False):
    """Claim `delivery`, send its payslip once, and record the outcome. The single
    place a delivery attempt is made, reused by the batch send loop, "Resend
    failed" and the automatic-retry path. Returns the delivery's status
    afterwards, or None when the claim was lost and nothing was sent. Commits."""
    if not claim_delivery(delivery.id, claimable, batch_id=batch_id,
                          require_scheduled_retry=require_scheduled_retry):
        return None
    recipient, problem = _recipient_for(ch, item)
    delivery.channel = ch
    delivery.recipient = recipient
    if problem:
        _mark_failed(delivery, problem, retry=False)
        db.session.commit()
        return delivery.status

    # Pace sends to the channel/provider's configured rate before the real call.
    from .throttle import throttle

    throttle(ch)
    try:
        message = _build_message(ch, item, run, client, recipient, delivery)
        # Commit before sending: the short link minted for an SMS has to exist
        # before the SMS carrying it does.
        db.session.commit()
    except Exception:  # noqa: BLE001 - nothing was sent; record that and carry on
        db.session.rollback()
        current_app.logger.exception(
            "[distribution] could not prepare the %s message for item %s", ch, item.id
        )
        delivery.channel, delivery.recipient = ch, recipient
        _mark_failed(delivery, "could not prepare the message; nothing was sent", retry=False)
        db.session.commit()
        return delivery.status
    result = sender.send(message)
    _record_outcome(delivery, result, max_attempts, backoff_base)
    db.session.commit()
    return delivery.status


def distribute_run(run, channel=CHANNEL_AUTO, only_failed=False, batch_id=None):
    """Render + send every payslip in `run`. Returns a summary dict. Each delivery's
    claim and outcome are committed as they happen, then the audit row.

    `batch_id` (the DistributionBatch driving this send) is stamped onto every
    delivery attempted, so history can attribute a delivery to the initiating
    operator and filter by batch."""
    client = run.client_company
    auto = channel == CHANNEL_AUTO
    max_attempts, backoff_base = _retry_config()
    senders = {}

    def sender_for(ch):
        if ch not in senders:
            senders[ch] = get_sender(ch)
        return senders[ch]

    summary = {"total": 0, "sent": 0, "failed": 0, "unknown": 0, "skipped": 0,
               "failed_workers": [], "unknown_workers": []}

    # Materialise the roster up front: we commit inside the loop (expiring the
    # session), so a live iterator over the lazy `run.items` collection would be
    # invalidated mid-flight.
    for item in list(run.items):
        summary["total"] += 1
        ch = resolve_channel(item) if auto else channel
        existing = _latest_delivery(item, ch)

        if only_failed:
            if existing is None or existing.status != DELIVERY_FAILED:
                summary["skipped"] += 1
                continue
            delivery, claimable = existing, CLAIMABLE_FOR_RETRY
        else:
            if existing is not None and existing.status in SKIPPED_BY_SEND:
                summary["skipped"] += 1
                continue
            delivery = existing or _insert_pending(item, run, ch)
            claimable = CLAIMABLE_FOR_SEND
        if delivery is None:
            summary["skipped"] += 1
            continue

        staff_ref = item.staff_id or str(item.id)
        # Durable per item: the claim and the outcome each commit, so a crash
        # mid-run leaves every payslip either untouched, `sending` (which the
        # stale-claim recovery turns `unknown`), or settled. None is re-sent
        # blind by a re-run or a batch reclaim.
        status = _attempt_send(delivery, item, run, client, ch, sender_for(ch),
                               max_attempts, backoff_base, claimable=claimable,
                               batch_id=batch_id)
        # Counted by where the delivery ended up: a timeout is neither a
        # success nor a failure that anyone should resend.
        if status is None:
            summary["skipped"] += 1
        elif status == DELIVERY_SENT:
            summary["sent"] += 1
        elif status == DELIVERY_UNKNOWN:
            summary["unknown"] += 1
            summary["unknown_workers"].append(staff_ref)
        else:
            summary["failed"] += 1
            summary["failed_workers"].append(staff_ref)

    record_audit(
        "Payslips distributed" if not only_failed else "Failed payslips resent",
        run,
        f"channel={channel} sent={summary['sent']} failed={summary['failed']} "
        f"unknown={summary['unknown']} skipped={summary['skipped']} of {summary['total']}.",
    )
    db.session.commit()
    return summary


def retry_delivery(delivery):
    """The automatic sweep's re-attempt of one failed delivery whose retry is due,
    in place (no new row, same channel). Reuses the roster contact fresh, so a
    fixed roster is picked up. Returns True if it was sent. Commits."""
    if delivery.status != DELIVERY_FAILED:
        return False  # never resend a success, an in-flight or an unconfirmed send
    item = db.session.get(PayrollItem, delivery.payroll_item_id)
    run = db.session.get(PayrollRun, delivery.payroll_run_id)
    if item is None or run is None:
        return False
    max_attempts, backoff_base = _retry_config()
    status = _attempt_send(
        delivery, item, run, run.client_company, delivery.channel,
        get_sender(delivery.channel), max_attempts, backoff_base,
        claimable=CLAIMABLE_FOR_RETRY, require_scheduled_retry=True,
    )
    return status == DELIVERY_SENT
