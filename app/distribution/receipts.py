"""Delivery-receipt handling (Phase 4, Slice 4).

Providers accept a send synchronously (our `sent` status) and then post an async
callback with the real handset outcome (delivered / read / undelivered / failed).
This maps those callbacks onto the PayslipDelivery: a success confirmation records
provider_status + delivered_at (status stays `sent`); a hard failure flips the
delivery back to `failed` through the normal retry machinery, so an undelivered
message is re-attempted like any other failure.

Payload parsers are best-effort and provider-shaped (Meta WhatsApp, Hubtel),
returning a normalised list of {message_id, status, reason} so the webhook routes
stay thin. Nothing here trusts the caller's identity — the routes verify the
provider secret/token first.
"""
from datetime import datetime, timezone

from app import db
from app.models import PayslipDelivery

from .service import _mark_failed

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
        # Provider says it never reached the handset — treat as a failed attempt so
        # the retry system can re-send (bounded by the retry limit).
        _mark_failed(delivery, f"provider reported {raw_status}"
                     + (f": {reason}" if reason else ""))
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
