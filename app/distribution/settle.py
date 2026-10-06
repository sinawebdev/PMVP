"""An operator settles a send that may have gone out (SMS Phase 3, Q2).

A delivery is `unknown` when SasuSync may have accepted it (a timeout, or a
send that stopped mid-way). Nothing settles it automatically except a delivery
report, so an operator checks the SasuSync portal and says which it was:

* ``sent``: the row becomes `sent`. ``provider_status`` stays empty, because
  this is the operator's word, not a delivery report.
* ``not_sent``: the row becomes a failure that "Resend failed" picks up. It is
  never retried automatically; the resend mints a fresh link and revokes the
  old one like any other retry, since the operator confirmed it never arrived.

Same shape as the send claim: a conditional UPDATE on the status the row has in
the database right now, so two operators (or a delivery report) racing to
settle one row cannot both change it.
"""
from datetime import datetime, timezone

from sqlalchemy import update

from app import db
from app.models import DELIVERY_FAILED, DELIVERY_SENT, DELIVERY_UNKNOWN, PayslipDelivery

SETTLE_SENT = "sent"
SETTLE_NOT_SENT = "not_sent"
SETTLE_OUTCOMES = (SETTLE_SENT, SETTLE_NOT_SENT)
NOT_SENT_ERROR = "Operator confirmed not sent (SasuSync portal)"


def settle_unknown(delivery, outcome, *, now=None):
    """Settle ``delivery`` as ``outcome`` if it is `unknown` right now. True if
    it was; False (and nothing changed) if it was not. The caller commits."""
    if outcome == SETTLE_SENT:
        values = {
            "status": DELIVERY_SENT, "error": None, "next_retry_at": None,
            # When the send was attempted: the best record of when it went out.
            "sent_at": delivery.claimed_at or now or datetime.now(timezone.utc),
        }
    elif outcome == SETTLE_NOT_SENT:
        values = {"status": DELIVERY_FAILED, "error": NOT_SENT_ERROR, "next_retry_at": None}
    else:
        raise ValueError(f"unknown settle outcome: {outcome!r}")
    result = db.session.execute(
        update(PayslipDelivery)
        .where(PayslipDelivery.id == delivery.id, PayslipDelivery.status == DELIVERY_UNKNOWN)
        .values(**values)
        .execution_options(synchronize_session=False)
    )
    return result.rowcount == 1
