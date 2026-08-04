"""Monitoring dashboard aggregates for the distribution subsystem (Phase 3, Slice 4).

One function, :func:`collect_dashboard_stats`, gathers every metric the operator
monitoring view needs with a small, fixed number of aggregate queries (never one
query per batch/delivery). It reuses the existing DistributionBatch and
PayslipDelivery data — no new tables, no duplicated business logic.

This is a read-only, cross-tenant operational view (the operator watches every
tenant's distributions), mirroring how the worker itself spans tenants.

**Every metric here is one of exactly two kinds, and the difference is the whole
reason this module was reshaped (DDEP Phase 2, trust engineering).**

  *Outcome* metrics count things that HAPPENED, and are meaningless without a
  window: a success rate accumulated since the first deploy never moves again
  after the first few thousand sends, so it can be 83% while today was perfect
  and it will read 83% tomorrow whatever anyone does. These are scoped to
  ``window_days`` and carry the same window's previous period for comparison.

  *State* metrics count what is TRUE RIGHT NOW — queued, running, retrying,
  out of retries. Windowing those would be nonsense (a batch queued 40 days ago
  is still queued), so they are deliberately unscoped, and the template labels
  them as standing state rather than period activity.

Mixing the two is what produced the original page's contradictions, so the
distinction is kept in the data rather than left to the template to remember.
"""
from datetime import datetime, timedelta, timezone

from flask import current_app
from sqlalchemy import func
from sqlalchemy.orm import joinedload

from app import db
from app.distribution.service import as_aware
from app.models import (
    BATCH_CANCELLED,
    BATCH_COMPLETED,
    BATCH_FAILED,
    BATCH_QUEUED,
    BATCH_RUNNING,
    BATCH_SCHEDULED,
    DELIVERY_FAILED,
    DELIVERY_PENDING,
    DELIVERY_SENT,
    WORKER_STATUS_RUNNING,
    DistributionBatch,
    PayslipDelivery,
)

# A heartbeat older than this belongs to a process that is no longer running.
# Generous relative to the poll interval so a worker inside a long send is never
# counted dead.
WORKER_LIVE_WINDOW_SECONDS = 300

# The reporting windows the monitor offers, newest-relevant first. 0 == all time,
# kept because "has this EVER happened" is a real investigative question — it is
# just the wrong default for a page whose job is "how are we doing".
WINDOW_OPTIONS = (
    (7, "7 days"),
    (30, "30 days"),
    (90, "90 days"),
    (0, "All time"),
)
DEFAULT_WINDOW_DAYS = 30
_WINDOW_DAYS = {days for days, _ in WINDOW_OPTIONS}


def resolve_window(value, default=DEFAULT_WINDOW_DAYS):
    """The window a query string asked for, or the default.

    A window arrives from a URL a user can edit, so an unknown value is the
    default rather than a 500 or an unbounded scan."""
    try:
        days = int(value)
    except (TypeError, ValueError):
        return default
    return days if days in _WINDOW_DAYS else default


def window_label(days):
    for option_days, label in WINDOW_OPTIONS:
        if option_days == days:
            return label
    return f"{days} days"


def _status_counts(model, since=None, until=None):
    """{status: count} for a model with a `status` column, in one grouped query.

    ``since``/``until`` bound it to a window on ``created_at``. Aware UTC
    datetimes, matching how history.py filters the same columns."""
    query = db.session.query(model.status, func.count())
    if since is not None:
        query = query.filter(model.created_at >= since)
    if until is not None:
        query = query.filter(model.created_at < until)
    return {status: count for status, count in query.group_by(model.status).all()}


def _pct(part, whole):
    return round(100 * part / whole, 1) if whole else 0.0


def _change(current, previous):
    """Fractional change for macros/charts.html::delta, or None when there is no
    baseline to compare against.

    None is not zero. A window with nothing before it has an UNKNOWN movement,
    and rendering that as "0.0%" asserts stability that was never measured —
    the same rule app.analytics.pct_change already follows."""
    if not previous:
        return None
    return (current - previous) / previous


def _running_batch_progress(batch):
    """Best-effort live progress for a running batch. Because distribute_run
    commits a batch's deliveries as a unit, a running batch's per-item progress
    isn't visible mid-run; we surface elapsed time and the expected total so the
    operator still sees it working, plus an ETA once any progress is measurable."""
    started = as_aware(batch.started_at)
    now = datetime.now(timezone.utc)
    elapsed = (now - started).total_seconds() if started else 0
    done = (batch.sent_count or 0) + (batch.failed_count or 0)
    total = batch.total or 0
    pct = _pct(done, total)
    eta = None
    if done and total and elapsed > 0 and done < total:
        rate = done / elapsed  # deliveries/sec
        if rate > 0:
            eta = int((total - done) / rate)
    return {
        "batch": batch,
        "elapsed_seconds": int(elapsed),
        "done": done,
        "total": total,
        "percent": pct,
        "eta_seconds": eta,
    }


def _in_flight(now=None):
    """Every batch that has not finished yet — running, queued and scheduled — as
    ONE ordered list.

    The page used to answer "what is happening right now" three times over: a
    six-row status legend, two KPI tiles, and a panel that only ever showed
    running batches. An operator asking that question wants one list in the order
    the work will clear, which is what this is. `waiting_seconds` is how long the
    batch has been waiting to start (negative for a schedule still in the
    future), so the template can say "queued 4m" or "starts in 2h" without
    doing arithmetic of its own.
    """
    now = now or datetime.now(timezone.utc)
    batches = (
        DistributionBatch.query.options(
            joinedload(DistributionBatch.payroll_run),
            joinedload(DistributionBatch.client_company),
        )
        .filter(
            DistributionBatch.status.in_((BATCH_RUNNING, BATCH_QUEUED, BATCH_SCHEDULED))
        )
        .order_by(DistributionBatch.created_at.asc())
        .all()
    )
    # Running first (it is the one actually consuming the worker), then queued in
    # claim order, then schedules by when they fire.
    rank = {BATCH_RUNNING: 0, BATCH_QUEUED: 1, BATCH_SCHEDULED: 2}
    rows = []
    for batch in sorted(batches, key=lambda b: (rank.get(b.status, 3), b.id)):
        row = _running_batch_progress(batch)
        due = as_aware(batch.scheduled_for) or as_aware(batch.created_at)
        row["waiting_seconds"] = int((now - due).total_seconds()) if due else None
        row["due_at"] = due
        rows.append(row)
    return rows


def _attention(sla, worker, final_failures, failed_batches, backlog):
    """What on this page needs a human, as signal cards for macros/dashboard.html.

    Every item is derived from a number already on the screen — nothing new is
    measured here — so the panel can never disagree with the tiles above it. An
    all-clear still renders a card: a panel that draws nothing when all is well
    reads as broken, not as safe. Same rule, and the same component, as the
    operator dashboard's Risk & action rail.
    """
    signals = []
    for breach in sla.get("breaches", []):
        signals.append(
            {
                "tone": "danger",
                "title": "Service level breached",
                # sla.evaluate_sla already writes the threshold into the detail,
                # which is what makes the breach checkable rather than asserted.
                "detail": breach["detail"][:1].upper() + breach["detail"][1:],
                "action": "See history",
                "endpoint": "distribution.history",
                "args": {},
            }
        )
    if worker["status"] == "stalled":
        signals.append(
            {
                "tone": "danger",
                "title": "Worker is not processing",
                "detail": (
                    "Work is queued but nothing has completed recently. Check the "
                    "distribution worker process is running."
                ),
            }
        )
    if final_failures:
        signals.append(
            {
                "tone": "warn",
                "title": f"{final_failures} payslip{'' if final_failures == 1 else 's'} out of retries",
                "detail": (
                    "Automatic retries are exhausted. These need a corrected "
                    "contact detail and a resend — nothing will send them on its own."
                ),
                "action": "Open failures",
                "endpoint": "distribution.history",
                "args": {"status": DELIVERY_FAILED},
            }
        )
    if failed_batches:
        signals.append(
            {
                "tone": "warn",
                "title": f"{failed_batches} batch{'' if failed_batches == 1 else 'es'} failed",
                "detail": "A send stopped part-way. Resend the failed payslips from the run.",
                "action": "See history",
                "endpoint": "distribution.history",
                "args": {},
            }
        )
    if backlog["queued_payslips"] and worker["status"] != "stalled":
        signals.append(
            {
                "tone": "info",
                "title": f"{backlog['queued_payslips']} payslips waiting to send",
                "detail": "Queued and moving. No action needed unless the queue stops draining.",
            }
        )
    if not signals:
        signals.append(
            {
                "tone": "ok",
                "title": "Nothing needs attention",
                "detail": (
                    "No service-level breaches, no exhausted retries, and the "
                    "worker is keeping up."
                ),
            }
        )
    return signals


def _throughput(completed_batches):
    """Average processing speed across recently completed batches:
    (payslips per minute, mean batch duration seconds)."""
    total_items = 0
    total_seconds = 0.0
    durations = []
    for batch in completed_batches:
        started = as_aware(batch.started_at)
        finished = as_aware(batch.finished_at)
        if not started or not finished:
            continue
        duration = (finished - started).total_seconds()
        if duration < 0:
            continue
        durations.append(duration)
        total_seconds += duration
        total_items += batch.total or 0
    per_minute = round(total_items / (total_seconds / 60), 1) if total_seconds else None
    avg_duration = round(sum(durations) / len(durations), 1) if durations else None
    return {"payslips_per_minute": per_minute, "avg_batch_seconds": avg_duration}


def _worker_health(batch_counts, backlog, last_processed_at, worker_last_poll):
    """Best-effort health signal. The inline worker publishes a heartbeat
    (worker_last_poll); an external worker process does not, so we fall back to
    the most recent processing timestamp. A backlog with stale activity is the
    stall signal."""
    now = datetime.now(timezone.utc)
    heartbeat = as_aware(worker_last_poll) or as_aware(last_processed_at)
    age = (now - heartbeat).total_seconds() if heartbeat else None
    has_backlog = (
        backlog["queued_batches"] > 0
        or batch_counts.get(BATCH_RUNNING, 0) > 0
        or backlog["due_retries"] > 0
    )
    # Stalled: work is waiting but nothing has been processed recently.
    if has_backlog and (age is None or age > 120):
        status = "stalled"
    elif has_backlog:
        status = "active"
    else:
        status = "idle"
    return {"status": status, "heartbeat_age_seconds": int(age) if age is not None else None}


def _worker_fleet(heartbeats):
    """Plain-language liveness summary for the operator's Worker panel.

    The raw heartbeat rows are engineering detail. Because each deploy gives the
    process a new hostname, they accumulate one row per past container and read
    as a wall of pod names and PIDs to the business user this screen is for. The
    panel states whether a worker is running; the rows stay reachable behind an
    explicit disclosure for whoever actually needs them.
    """
    now = datetime.now(timezone.utc)
    live = 0
    for hb in heartbeats:
        polled = as_aware(hb.last_poll_at)
        if (
            hb.status == WORKER_STATUS_RUNNING
            and polled is not None
            and (now - polled).total_seconds() <= WORKER_LIVE_WINDOW_SECONDS
        ):
            live += 1
    return {"live": live, "known": len(heartbeats), "retired": len(heartbeats) - live}


def collect_dashboard_stats(recent_limit=10, window_days=DEFAULT_WINDOW_DAYS):
    """Every metric the monitoring dashboard needs, in one call.

    ``window_days`` scopes the OUTCOME metrics (see the module docstring); 0
    means all time. State metrics ignore it by design."""
    from app.distribution.queue import worker_last_poll, worker_statuses  # avoid import cycle

    now = datetime.now(timezone.utc)
    since = now - timedelta(days=window_days) if window_days else None
    # The equal-length period immediately before this one, so "vs previous" on a
    # tile compares like with like instead of against everything that came before.
    prior_since = since - timedelta(days=window_days) if since else None

    heartbeats = worker_statuses()
    # Batch STATE — every unfinished batch, whatever window is selected. A batch
    # queued six weeks ago is still queued, and hiding it behind a 30-day filter
    # would hide the exact failure the queue exists to make visible.
    live_batch_counts = _status_counts(DistributionBatch)
    # Batch OUTCOMES — what finished inside the window.
    window_batch_counts = _status_counts(DistributionBatch, since=since)
    delivery_counts = _status_counts(PayslipDelivery, since=since)
    prior_counts = _status_counts(PayslipDelivery, since=prior_since, until=since)

    sent = delivery_counts.get(DELIVERY_SENT, 0)
    failed = delivery_counts.get(DELIVERY_FAILED, 0)
    pending = delivery_counts.get(DELIVERY_PENDING, 0)
    attempted = sent + failed
    prior_sent = prior_counts.get(DELIVERY_SENT, 0)
    prior_failed = prior_counts.get(DELIVERY_FAILED, 0)
    prior_attempted = prior_sent + prior_failed

    # Provider-confirmed receipts — the only figure on this page that means a
    # payslip actually reached a handset. `sent` means the channel ACCEPTED it,
    # which with the console backend means "written to the log". Counted
    # separately, and never conflated with `sent`, so the tile can say which it
    # is showing (see macros/delivery.html::simulated_notice for the same point).
    confirmed_query = PayslipDelivery.query.filter(
        PayslipDelivery.status == DELIVERY_SENT,
        PayslipDelivery.delivered_at.isnot(None),
    )
    if since is not None:
        confirmed_query = confirmed_query.filter(PayslipDelivery.created_at >= since)
    confirmed = confirmed_query.count()

    # Retry STATE, both unwindowed on purpose: a payslip that is still retrying,
    # or has run out of retries, is outstanding work today no matter when it was
    # first attempted.
    active_retries = PayslipDelivery.query.filter(
        PayslipDelivery.status == DELIVERY_FAILED,
        PayslipDelivery.next_retry_at.isnot(None),
    ).count()
    # Counted directly rather than as `failed - active_retries`. That subtraction
    # mixed a windowed total with an unwindowed one (so it could go negative, and
    # was clamped at zero to hide it), and it silently assumed every failure had
    # ever been scheduled for retry. "No retry is coming" is a property of the
    # row; ask the row.
    final_failures = PayslipDelivery.query.filter(
        PayslipDelivery.status == DELIVERY_FAILED,
        PayslipDelivery.next_retry_at.is_(None),
    ).count()

    queued_batches = live_batch_counts.get(BATCH_QUEUED, 0)
    backlog_payslips = (
        db.session.query(func.coalesce(func.sum(DistributionBatch.total), 0))
        .filter(DistributionBatch.status == BATCH_QUEUED)
        .scalar()
        or 0
    )
    backlog = {
        "queued_batches": queued_batches,
        "queued_payslips": int(backlog_payslips),
        "due_retries": active_retries,
    }

    recent_batches = (
        DistributionBatch.query.options(
            joinedload(DistributionBatch.payroll_run),
            joinedload(DistributionBatch.client_company),
            joinedload(DistributionBatch.initiated_by),
        )
        .order_by(DistributionBatch.created_at.desc())
        .limit(recent_limit)
        .all()
    )
    running = (
        DistributionBatch.query.options(joinedload(DistributionBatch.payroll_run))
        .filter(DistributionBatch.status == BATCH_RUNNING)
        .order_by(DistributionBatch.started_at.asc())
        .all()
    )
    completed_recent = (
        DistributionBatch.query.filter(DistributionBatch.status == BATCH_COMPLETED)
        .order_by(DistributionBatch.finished_at.desc())
        .limit(50)
        .all()
    )

    last_processed_at = (
        db.session.query(func.max(DistributionBatch.finished_at)).scalar()
    )

    sla = _sla_snapshot()
    worker = _worker_health(
        live_batch_counts, backlog, last_processed_at, worker_last_poll()
    )
    failed_batches_in_window = window_batch_counts.get(BATCH_FAILED, 0)

    return {
        # Batch counts, in the two kinds this module distinguishes: the first
        # three are unfinished work (now), the last three are what the window
        # closed out. `total` stays the sum so the shape of this dict is
        # unchanged for existing callers.
        "batches": {
            "scheduled": live_batch_counts.get(BATCH_SCHEDULED, 0),
            "queued": live_batch_counts.get(BATCH_QUEUED, 0),
            "running": live_batch_counts.get(BATCH_RUNNING, 0),
            "completed": window_batch_counts.get(BATCH_COMPLETED, 0),
            "failed": failed_batches_in_window,
            "cancelled": window_batch_counts.get(BATCH_CANCELLED, 0),
            "total": sum(live_batch_counts.values()),
        },
        "deliveries": {
            "sent": sent,
            "failed": failed,
            "pending": pending,
            "attempted": attempted,
            "confirmed": confirmed,
            "active_retries": active_retries,
            "final_failures": final_failures,
            "success_rate": _pct(sent, attempted),
            "failure_rate": _pct(failed, attempted),
            # Movement against the equal-length window before this one. None
            # where there was no such window (or nothing in it) — see _change.
            "sent_change": _change(sent, prior_sent),
            "success_rate_change": _change(
                _pct(sent, attempted), _pct(prior_sent, prior_attempted)
            ),
            "prior_attempted": prior_attempted,
        },
        "window": {
            "days": window_days,
            "label": window_label(window_days),
            # "in the last 30 days" / "across all time" — one phrase, written
            # once, so every caption on the page states the same basis.
            "phrase": (
                f"in the last {window_label(window_days).lower()}"
                if window_days
                else "across all time"
            ),
            "since": since,
            "options": WINDOW_OPTIONS,
        },
        "backlog": backlog,
        # Is anything actually moving? Phase 7, Task 7.2 — the monitor used to
        # poll every five seconds for as long as the tab was open, by design
        # ("there is no terminal state"). A monitor of an idle queue is a
        # five-second heartbeat against the database forever, on every open tab.
        # It polls while there is work in flight and stops when there is not;
        # the page then offers a manual refresh, so nothing is hidden — only the
        # polling stops.
        "live": bool(
            live_batch_counts.get(BATCH_QUEUED, 0)
            or live_batch_counts.get(BATCH_RUNNING, 0)
            or active_retries
        ),
        "recent_batches": recent_batches,
        "running_batches": [_running_batch_progress(b) for b in running],
        "in_flight": _in_flight(now),
        "throughput": _throughput(completed_recent),
        "last_processed_at": last_processed_at,
        "worker": worker,
        "worker_inline": bool(current_app.config.get("DISTRIBUTION_WORKER_INLINE")),
        "worker_fleet": _worker_fleet(heartbeats),
        "sla": sla,
        "attention": _attention(
            sla, worker, final_failures, failed_batches_in_window, backlog
        ),
    }


def _sla_statement(thresholds):
    """The service levels, in words, from the configured thresholds.

    "Within SLA" is an assertion, and an assertion nobody can check is not
    reassurance — it is a green badge. This states the levels being met, built
    from the SAME config sla.evaluate_sla judges against, so the claim and its
    definition cannot drift apart.
    """
    parts = []
    if thresholds.get("batch_minutes"):
        parts.append(f"every batch finishes within {thresholds['batch_minutes']} minutes")
    if thresholds.get("failure_rate") and thresholds.get("window_hours"):
        parts.append(
            f"failures stay under {round(thresholds['failure_rate'] * 100)}% over "
            f"{thresholds['window_hours']}h"
        )
    if thresholds.get("confirm_hours"):
        parts.append(
            f"provider receipts arrive within {thresholds['confirm_hours']}h"
        )
    if not parts:
        return "No service levels are configured."
    if len(parts) == 1:
        return f"Target: {parts[0]}."
    return f"Target: {', '.join(parts[:-1])}, and {parts[-1]}."


def _sla_snapshot():
    from .sla import evaluate_sla

    try:
        result = evaluate_sla()
    except Exception:  # noqa: BLE001 - the dashboard must never fail on SLA eval
        db.session.rollback()
        result = {"ok": True, "breaches": [], "thresholds": {}}
    result["statement"] = _sla_statement(result.get("thresholds") or {})
    return result
