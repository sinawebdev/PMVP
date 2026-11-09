"""Delivery-receipt handling (Phase 4, Slice 4).

Providers accept a send synchronously (our `sent` status) and then post an async
callback with the real handset outcome (delivered / read / undelivered / failed).
This maps those callbacks onto the PayslipDelivery: a success confirmation records
provider_status + delivered_at (status stays `sent`); a hard failure flips the
delivery back to `failed`, with no automatic retry. A report can be wrong, and a
resend on the strength of one could reach the worker twice, so whether to resend
is left to an operator ("Resend failed").

Payload parsers are best-effort and provider-shaped (Meta WhatsApp, Hubtel),
returning a normalised list of {message_id, status, reason} so the webhook routes
stay thin. Nothing here trusts the caller's identity — the routes verify the
provider secret/token first.
"""
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, or_

from app import db
from app.models import (
    CHANNEL_SMS,
    DELIVERY_FAILED,
    DELIVERY_SENDING,
    DELIVERY_SENT,
    DELIVERY_UNKNOWN,
    PayslipDelivery,
)

from .channels import recipient_fingerprint
from .phones import normalise_gh_mobile
from .sasusync import scrub_numbers
from .service import _mark_failed

logger = logging.getLogger(__name__)

# Normalised receipt outcomes.
RECEIPT_DELIVERED = "delivered"
RECEIPT_READ = "read"
RECEIPT_FAILED = "failed"

# Raw provider statuses we treat as a confirmed success vs a hard failure.
_SUCCESS = {"delivered", "read", "sent", "success", "delivrd", "0", "deliveredtohandset"}
_FAILURE = {"failed", "undelivered", "undeliverable", "rejected", "expired", "error"}


def _classify(raw_status):
    s = (raw_status or "").strip().lower()
    if s in _SUCCESS:
        return RECEIPT_READ if s == "read" else RECEIPT_DELIVERED
    if s in _FAILURE:
        return RECEIPT_FAILED
    return None  # unknown -> ignored (do not corrupt state on an unrecognised status)


def apply_receipt(message_id, raw_status, reason=None):
    """Apply one provider receipt to the matching delivery. Returns the delivery
    if found and updated, else None. Does NOT commit — the caller owns the txn."""
    if not message_id:
        return None
    delivery = PayslipDelivery.query.filter_by(provider_message_id=message_id).first()
    if delivery is None:
        return None
    outcome = _classify(raw_status)
    if outcome is None:
        return None
    delivery.provider_status = outcome
    if outcome in (RECEIPT_DELIVERED, RECEIPT_READ):
        delivery.delivered_at = datetime.now(timezone.utc)
    elif outcome == RECEIPT_FAILED:
        # Provider says it never reached the handset. Recorded as a failure, but
        # never retried automatically: see the module docstring.
        _mark_failed(delivery, f"provider reported {raw_status}"
                     + (f": {reason}" if reason else ""), retry=False)
    return delivery


def apply_receipts(receipts):
    """Apply a batch of normalised receipts; commit once. Returns the count applied."""
    applied = 0
    for r in receipts:
        if apply_receipt(r.get("message_id"), r.get("status"), r.get("reason")) is not None:
            applied += 1
    db.session.commit()
    return applied


# --- Provider payload parsers ----------------------------------------------
def parse_whatsapp_statuses(payload):
    """Meta WhatsApp Cloud webhook -> [{message_id, status, reason}].

    Shape: entry[].changes[].value.statuses[] with id, status, and errors[] on
    failure."""
    out = []
    for entry in (payload or {}).get("entry", []) or []:
        for change in entry.get("changes", []) or []:
            value = change.get("value", {}) or {}
            for st in value.get("statuses", []) or []:
                reason = None
                errors = st.get("errors")
                if isinstance(errors, list) and errors:
                    reason = errors[0].get("title") or errors[0].get("message")
                out.append({
                    "message_id": st.get("id"),
                    "status": st.get("status"),
                    "reason": reason,
                })
    return out


def parse_hubtel_status(payload):
    """Hubtel delivery callback -> [{message_id, status, reason}]. Field names vary
    across Hubtel products, so match case-insensitively."""
    if not isinstance(payload, dict):
        return []
    lower = {str(k).lower(): v for k, v in payload.items()}
    message_id = lower.get("messageid") or lower.get("message_id") or lower.get("id")
    status = lower.get("status") or lower.get("deliverystatus") or lower.get("delivery_status")
    reason = lower.get("reason") or lower.get("networkcode") or lower.get("statusdescription")
    if message_id is None and status is None:
        return []
    return [{"message_id": str(message_id) if message_id is not None else None,
             "status": str(status) if status is not None else None,
             "reason": str(reason) if reason is not None else None}]


# --- SasuSync delivery reports ---------------------------------------------
# Not routed through apply_receipt: SasuSync's `sent` only means "handed to the
# network", so it must not count as delivered, and its reports carry their own
# timestamps, where the later timestamp wins rather than the later request.
_SASUSYNC_FAILURES = {"failed", "expired"}
_SASUSYNC_EVENTS = {"sent", "delivered"} | _SASUSYNC_FAILURES
_REPORTABLE_STATUSES = (DELIVERY_SENT, DELIVERY_UNKNOWN, DELIVERY_FAILED, DELIVERY_SENDING)
# A report matched on the phone number alone only counts for a send this recent.
_RECIPIENT_MATCH_WINDOW = timedelta(days=3)


def _utc_now_naive():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _event_time(raw):
    """The report's timestamp as naive UTC, the way the column stores it. A missing
    or unreadable one counts as now."""
    try:
        parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return _utc_now_naive()
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def _by_delivery_id(payload, data):
    """The delivery id we sent as metadata. The API docs say metadata is echoed on
    webhooks but don't show where, so both places are checked."""
    for source in (payload, data):
        metadata = source.get("metadata")
        raw = metadata.get("delivery_id") if isinstance(metadata, dict) else None
        try:
            delivery = db.session.get(PayslipDelivery, int(raw))
        except (TypeError, ValueError):
            continue
        if delivery is not None and delivery.channel == CHANNEL_SMS:
            return delivery
    return None


def _by_provider_id(data):
    """We store the send response's task_id, which the docs say webhooks refer to.
    Their example shows only a per-message message_id, so that is tried as well."""
    for key in ("task_id", "job_id", "message_id"):
        value = data.get(key)
        if not value:
            continue
        delivery = PayslipDelivery.query.filter_by(
            provider_message_id=str(value), channel=CHANNEL_SMS
        ).first()
        if delivery is not None:
            return delivery
    return None


def _by_recipient(data):
    """The last resort: the one recent send to this number still awaiting a report.
    Two candidates are ambiguous, and a report is ignored rather than guessed."""
    number = normalise_gh_mobile(data.get("recipient"))
    if number is None:
        return None
    awaiting = or_(
        and_(
            PayslipDelivery.status.in_((DELIVERY_SENT, DELIVERY_UNKNOWN)),
            PayslipDelivery.provider_status.is_(None),
        ),
        and_(
            PayslipDelivery.status == DELIVERY_FAILED,
            PayslipDelivery.provider_status.in_(_SASUSYNC_FAILURES),
        ),
    )
    candidates = PayslipDelivery.query.filter(
        PayslipDelivery.channel == CHANNEL_SMS,
        PayslipDelivery.recipient == number,
        PayslipDelivery.claimed_at >= _utc_now_naive() - _RECIPIENT_MATCH_WINDOW,
        awaiting,
    ).limit(2).all()
    return candidates[0] if len(candidates) == 1 else None


def _find_sasusync_delivery(payload, data):
    """``(delivery, how it was matched)``, or ``(None, None)``."""
    for how, delivery in (
        ("delivery_id", _by_delivery_id(payload, data)),
        ("provider_id", _by_provider_id(data)),
        ("recipient", _by_recipient(data)),
    ):
        if delivery is not None:
            return delivery, how
    return None, None


def _record_as_sent(delivery, at):
    delivery.status = DELIVERY_SENT
    delivery.error = None
    delivery.next_retry_at = None
    if delivery.sent_at is None:
        delivery.sent_at = at


def apply_sasusync_event(payload):
    """Apply one verified SasuSync report and return the delivery it changed, or
    None if it changed nothing. An older or repeated report is a no-op. Does NOT
    commit: the route owns the transaction. Makes no outgoing calls, because
    SasuSync wants an answer within 10 seconds.

    * ``sent``: proves the network took it, so it settles an `unknown` row and
      otherwise changes nothing. provider_status stays empty, which is what keeps
      the row in the "sent but unconfirmed" alert until a real report arrives.
    * ``delivered``: reached the handset. Also corrects an `unknown` or `failed` row.
    * ``failed`` / ``expired``: never reached it. Recorded as failed with no
      automatic retry, since a resend is a person's decision (see the docstring).
    """
    if not isinstance(payload, dict):
        return None
    event = str(payload.get("event") or "").strip().lower()
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    if event not in _SASUSYNC_EVENTS:
        # low_balance and topup_succeeded are about the account, not a message.
        if event == "low_balance":
            logger.warning("[sasusync] low balance: sms_credits=%s", data.get("sms_credits"))
        return None

    delivery, how = _find_sasusync_delivery(payload, data)
    if delivery is None or delivery.status not in _REPORTABLE_STATUSES:
        logger.info(
            "[sasusync] report not applied: event=%s to=%s found=%s", event,
            recipient_fingerprint(str(data.get("recipient") or "")), delivery is not None,
        )
        return None

    at = _event_time(payload.get("timestamp"))
    if delivery.provider_status_at is not None and at <= delivery.provider_status_at:
        return None

    if event == "sent":
        if delivery.status != DELIVERY_UNKNOWN:
            return None
        _record_as_sent(delivery, at)
    elif event == "delivered":
        delivery.provider_status = event
        delivery.delivered_at = at
        if delivery.status != DELIVERY_SENDING:
            _record_as_sent(delivery, at)
    else:
        delivery.provider_status = event
        delivery.delivered_at = None
        if delivery.status != DELIVERY_SENDING:
            reason = data.get("reason") or data.get("error") or data.get("detail")
            detail = f"Mobile network reported: {event}"
            if reason:
                detail = f"{detail} ({scrub_numbers(str(reason))})"
            _mark_failed(delivery, detail, provider=delivery.provider, retry=False)

    delivery.provider_status_at = at
    logger.info(
        "[sasusync] report applied: event=%s delivery=%s matched_by=%s", event, delivery.id, how
    )
    return delivery
