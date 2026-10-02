"""Deliveries a worker stopped part-way through.

A delivery is `sending` only between its claim and its outcome, and one
provider call is capped at 30 seconds, so a live send is never minutes old. A
`sending` row older than :data:`STALE_CLAIM_MINUTES` belongs to a worker that
died (a crash, an OOM kill, a deploy) after claiming it. Whether the provider
got the message first cannot be known, so the row becomes `unknown`: never
re-sent automatically, and visible until someone settles it.
"""
from datetime import datetime, timedelta, timezone

from sqlalchemy import update

from app import db
from app.models import DELIVERY_SENDING, DELIVERY_UNKNOWN, PayslipDelivery

from .service import as_aware

STALE_CLAIM_MINUTES = 10
STALE_CLAIM_ERROR = "Sending stopped mid-way; this message may have been sent."


def recover_stale_claims(now=None):
    """Turn every `sending` row claimed more than STALE_CLAIM_MINUTES ago into
    `unknown`. Returns how many changed. Commits when any did.

    Each UPDATE is conditional on the row still being `sending` with the same
    `claimed_at`, so a send that finishes, or a fresh claim of the same row, in
    the moment between the read and the write is left alone."""
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(minutes=STALE_CLAIM_MINUTES)
    stale = [
        delivery
        for delivery in PayslipDelivery.query.filter_by(status=DELIVERY_SENDING).all()
        if (as_aware(delivery.claimed_at) or as_aware(delivery.updated_at) or now) < cutoff
    ]
    changed = 0
    for delivery in stale:
        same_claim = (
            PayslipDelivery.claimed_at.is_(None)
            if delivery.claimed_at is None
            else PayslipDelivery.claimed_at == delivery.claimed_at
        )
        result = db.session.execute(
            update(PayslipDelivery)
            .where(
                PayslipDelivery.id == delivery.id,
                PayslipDelivery.status == DELIVERY_SENDING,
                same_claim,
            )
            .values(status=DELIVERY_UNKNOWN, error=STALE_CLAIM_ERROR, next_retry_at=None)
            .execution_options(synchronize_session=False)
        )
        changed += result.rowcount
    if changed:
        db.session.commit()
    return changed


def unconfirmed_count():
    """Deliveries in `unknown` right now, whenever they were sent. A standing
    state, like the retry counts, so the dashboard does not window it."""
    return PayslipDelivery.query.filter_by(status=DELIVERY_UNKNOWN).count()
