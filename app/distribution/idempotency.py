"""Request-level idempotency for the "Send payslips" action.

A retried or double-clicked send carrying the same nonce replays the stored result instead
of distributing again. The unique key column makes a concurrent claim fail fast.
"""
import json

from sqlalchemy.exc import IntegrityError

from app import db
from app.models import IdempotencyKey


def replay_or_run(key, run_fn):
    """Run `run_fn()` (-> dict summary) at most once per key.

    Returns (summary, replayed). With no key, just runs (no guarantee).
    """
    if not key:
        return run_fn(), False

    record = IdempotencyKey(key=key)
    db.session.add(record)
    try:
        db.session.commit()  # claim
    except IntegrityError:
        db.session.rollback()
        existing = IdempotencyKey.query.filter_by(key=key).first()
        if existing is not None and existing.response_json:
            return json.loads(existing.response_json), True
        # Claimed but not finished (rare race) — treat as replay-in-progress.
        return {"total": 0, "sent": 0, "failed": 0, "skipped": 0, "in_progress": True}, True

    record_id = record.id
    try:
        summary = run_fn()
    except Exception:
        db.session.rollback()
        claim = db.session.get(IdempotencyKey, record_id)
        if claim is not None:
            db.session.delete(claim)
            db.session.commit()
        raise

    claim = db.session.get(IdempotencyKey, record_id)
    claim.response_json = json.dumps(summary)
    db.session.commit()
    return summary, False
