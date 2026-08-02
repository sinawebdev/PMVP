"""One answer to "what is the delivery state of this run?" — Phase 5, Task 5.1.

Both portals ask that question. Until this module existed they each answered it
separately: ``app/distribution/__init__.py::_run_status_context`` and
``app/client/__init__.py::_distribute_context`` were the same twenty lines
written twice, on top of byte-identical private copies of ``_latest_delivery``
and ``_latest_batch``.

Two implementations of one question drift, and these had:

* The tenant copy never computed ``scheduled``. Its template's status chain ran
  queued/running/completed/failed only, so a tenant looking at a *scheduled*
  batch saw a "Queue status" card with no status line in it at all.
* The tenant polled on ``in_flight`` where the operator polled on ``live``. The
  operator drew that distinction deliberately — a batch scheduled for next
  Friday changes nothing second to second, so it must not poll — and the tenant
  page, lacking it, re-fetched every three seconds until the send happened.

The context returned here is the superset, and it is the same dict on both
sides. What differs between the portals is which parts the *template* renders,
which is a shell concern and lives in ``macros/distribution.html``.
"""

from datetime import datetime, timezone

from app.models import DELIVERY_CHANNELS, DistributionBatch, PayslipDelivery
from app.payroll_status import SENDABLE_STATUSES

from .service import resolve_channel

# Batch states that mean work is actively moving through a worker right now.
ACTIVE_BATCH_STATUSES = ("queued", "running")


def latest_batch(run_id):
    """The most recent distribution batch for a run, or None."""
    return (
        DistributionBatch.query.filter_by(payroll_run_id=run_id)
        .order_by(DistributionBatch.created_at.desc())
        .first()
    )


def latest_deliveries_by_item(item_ids):
    """``{payroll_item_id: newest PayslipDelivery}`` in ONE query.

    Both portals previously ran a per-item ``SELECT ... LIMIT 1`` inside the row
    loop, so opening the status page for a 400-worker run issued 400 queries
    before rendering anything."""
    ids = list(item_ids)
    if not ids:
        return {}
    rows = (
        PayslipDelivery.query.filter(PayslipDelivery.payroll_item_id.in_(ids))
        .order_by(
            PayslipDelivery.payroll_item_id.asc(),
            PayslipDelivery.created_at.desc(),
        )
        .all()
    )
    latest = {}
    for delivery in rows:
        # Ordered newest-first within each item, so the first one wins.
        latest.setdefault(delivery.payroll_item_id, delivery)
    return latest


def delivery_status_context(run, now=None):
    """Everything either portal needs to render a run's delivery state.

    ``now`` is injectable so the scheduled-countdown is testable without
    freezing the clock globally."""
    now = now or datetime.now(timezone.utc)

    items = list(run.items)
    deliveries = latest_deliveries_by_item(item.id for item in items)
    rows = [
        {
            "item": item,
            "delivery": deliveries.get(item.id),
            "suggested": resolve_channel(item),
        }
        for item in items
    ]

    sent = sum(1 for r in rows if r["delivery"] and r["delivery"].status == "sent")
    failed = sum(1 for r in rows if r["delivery"] and r["delivery"].status == "failed")

    batch = latest_batch(run.id)
    # A pending automatic retry (a failed delivery still scheduled) keeps the
    # page live even after the batch itself reached a terminal state, so the
    # viewer watches recovery happen.
    pending_retry = any(
        r["delivery"]
        and r["delivery"].status == "failed"
        and r["delivery"].next_retry_at
        for r in rows
    )
    batch_active = batch is not None and batch.status in ACTIVE_BATCH_STATUSES
    scheduled = batch is not None and batch.status == "scheduled"

    seconds_until = None
    if scheduled and batch.scheduled_for is not None:
        target = batch.scheduled_for
        if target.tzinfo is None:
            target = target.replace(tzinfo=timezone.utc)
        seconds_until = int((target - now).total_seconds())

    return {
        "run": run,
        "rows": rows,
        "channels": DELIVERY_CHANNELS,
        "sendable": run.status in SENDABLE_STATUSES,
        "sent_count": sent,
        "failed_count": failed,
        "batch": batch,
        "in_flight": batch_active or pending_retry or scheduled,
        # Drives live polling — a far-future scheduled batch changes nothing
        # second-to-second, so it does not poll (only active/retrying does).
        "live": batch_active or pending_retry,
        "scheduled": scheduled,
        "seconds_until_scheduled": seconds_until,
        # Cancellable == there is not-yet-sent work to stop and no send is
        # actively running (a running batch is never cancelled mid-flight).
        "cancellable": scheduled
        or (batch is not None and batch.status == "queued")
        or pending_retry,
    }
