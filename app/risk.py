"""Run risk gate — deterministic pre-approval checks (Payrolla Phase 5).

When a payroll run is submitted, three rules decide whether it can auto-accept or
must be **held for platform review**. A run tripping ANY rule is HELD; a run that
trips none is AUTO-ACCEPTED. The rules are pure functions of the run and the
client's previous *closed* run (Approved/Processed) — no side effects, no
randomness — so the same run always yields the same verdict.

Thresholds settled with Sina (2026-07-16); see the pmvp-v1-decisions memory:

  Rule 1 — New-client hold: a client's first ``FIRST_N_RUNS_HELD`` runs are held.
  Rule 2 — Net-pay variance: total net pay differs from the previous closed run
           by more than ``NET_PAY_VARIANCE_PCT``.
  Rule 3 — Headcount swing: worker count differs from the previous closed run by
           more than ``HEADCOUNT_SWING_PCT``.

``apply_risk_gate`` persists the verdict onto the run (risk_status / risk_reasons
/ risk_checked_at); the caller owns the PayrollRun.status lifecycle transition.
"""

from dataclasses import dataclass, field

from sqlalchemy import func

from app import db
from app.models import PayrollRun
from app.payroll_status import (
    CLOSED_STATUSES,
    HELD,
    REJECTED,
    RISK_ACCEPTED,
    RISK_EVER_HELD,
    RISK_HELD,
    RISK_RELEASED,
)

# --- Settled thresholds (Sina, 2026-07-16) ---------------------------------
FIRST_N_RUNS_HELD = 2        # Rule 1: a client's first N runs are always held.
NET_PAY_VARIANCE_PCT = 0.15  # Rule 2: |Δ total net pay| vs previous closed run.
HEADCOUNT_SWING_PCT = 0.20   # Rule 3: |Δ worker count| vs previous closed run.

# Re-exported from app.payroll_status (the canonical vocabulary) so
# ``from app.risk import RISK_HELD`` keeps working.
__all_risk_states__ = (RISK_HELD, RISK_ACCEPTED, RISK_RELEASED)


@dataclass
class RiskCheck:
    code: str
    name: str
    tripped: bool
    detail: str


@dataclass
class RiskVerdict:
    held: bool
    checks: list = field(default_factory=list)

    @property
    def status(self):
        return RISK_HELD if self.held else RISK_ACCEPTED

    @property
    def tripped(self):
        return [c for c in self.checks if c.tripped]

    @property
    def reasons(self):
        return [c.detail for c in self.tripped]

    def reasons_text(self):
        return " | ".join(self.reasons)


def _previous_closed_run(run):
    """The client's most recent CLOSED run (Approved/Processed) before this one.

    Ordered by created_at then id so the baseline is deterministic even when two
    runs share a timestamp. Excludes ``run`` itself. None if there is no prior
    closed run (a brand-new client, or only pending runs so far)."""
    if not run.client_company_id:
        return None
    return (
        PayrollRun.query.filter(
            PayrollRun.client_company_id == run.client_company_id,
            PayrollRun.id != run.id,
            PayrollRun.status.in_(CLOSED_STATUSES),
        )
        .order_by(PayrollRun.created_at.desc(), PayrollRun.id.desc())
        .first()
    )


def _prior_run_count(run):
    """How many runs the client already has, excluding this one — i.e. this run's
    zero-based ordinal. Run #1 has 0 priors, run #2 has 1, and so on."""
    if not run.client_company_id:
        return 0
    return PayrollRun.query.filter(
        PayrollRun.client_company_id == run.client_company_id,
        PayrollRun.id != run.id,
    ).count()


def _pct(current, previous):
    """Fractional change |current-previous| / |previous|, or None if previous is 0."""
    if not previous:
        return None
    return abs((current or 0) - previous) / abs(previous)


def evaluate_run(run):
    """Score ``run`` against the three rules. Returns a :class:`RiskVerdict`.

    Pure/read-only: computes but does not persist. ``apply_risk_gate`` persists.
    """
    checks = []

    # Rule 1 — New-client hold.
    prior = _prior_run_count(run)
    ordinal = prior + 1
    new_client = prior < FIRST_N_RUNS_HELD
    checks.append(
        RiskCheck(
            "new_client",
            f"New-client review (first {FIRST_N_RUNS_HELD} runs)",
            new_client,
            (
                f"Run #{ordinal} for this client; the first {FIRST_N_RUNS_HELD} "
                "runs are always reviewed."
                if new_client
                else f"Client has {prior} prior run(s); past the new-client window."
            ),
        )
    )

    prev = _previous_closed_run(run)
    if prev is None:
        no_baseline = "No previous closed run to compare against."
        checks.append(RiskCheck("net_pay_variance", "Net-pay variance", False, no_baseline))
        checks.append(RiskCheck("headcount_swing", "Headcount swing", False, no_baseline))
        return RiskVerdict(held=any(c.tripped for c in checks), checks=checks)

    # Rule 2 — Net-pay variance vs the previous closed run.
    prev_net = prev.total_net_pay or 0
    this_net = run.total_net_pay or 0
    net_pct = _pct(this_net, prev_net)
    if net_pct is None:  # previous run had zero net pay — any nonzero is a swing
        net_tripped = bool(this_net)
        net_detail = (
            f"Previous run net pay was 0; this run is {this_net:,.2f}."
            if net_tripped
            else "Previous and current net pay are both 0."
        )
    else:
        net_tripped = net_pct > NET_PAY_VARIANCE_PCT
        net_detail = (
            f"Net pay {this_net:,.2f} vs previous {prev_net:,.2f} "
            f"({net_pct * 100:.1f}% change; threshold {NET_PAY_VARIANCE_PCT * 100:.0f}%)."
        )
    checks.append(RiskCheck("net_pay_variance", "Net-pay variance", net_tripped, net_detail))

    # Rule 3 — Headcount swing vs the previous closed run.
    prev_n = prev.total_workers or 0
    this_n = run.total_workers or 0
    hc_pct = _pct(this_n, prev_n)
    if hc_pct is None:  # previous run had zero workers
        hc_tripped = bool(this_n)
        hc_detail = (
            f"Previous run had 0 workers; this run has {this_n}."
            if hc_tripped
            else "Previous and current worker counts are both 0."
        )
    else:
        hc_tripped = hc_pct > HEADCOUNT_SWING_PCT
        hc_detail = (
            f"{this_n} workers vs previous {prev_n} "
            f"({hc_pct * 100:.1f}% change; threshold {HEADCOUNT_SWING_PCT * 100:.0f}%)."
        )
    checks.append(RiskCheck("headcount_swing", "Headcount swing", hc_tripped, hc_detail))

    return RiskVerdict(held=any(c.tripped for c in checks), checks=checks)


# --- Run comparison (operator productivity, Phase 2) ------------------------
# A read-only comparison of a run against the client's previous closed run,
# reusing the SAME baseline (_previous_closed_run) and thresholds the risk gate
# uses — so "unusual change" on the comparison panel and "held" from the gate
# stay consistent. No side effects; nothing here changes a lifecycle decision.

# (key, label, threshold, is_money) — threshold reuses the risk-gate constants.
_COMPARISON_METRICS = (
    ("workers", "Workers", HEADCOUNT_SWING_PCT, False),
    ("gross", "Gross pay", NET_PAY_VARIANCE_PCT, True),
    ("deductions", "Deductions", NET_PAY_VARIANCE_PCT, True),
    ("taxes", "PAYE + SSNIT", NET_PAY_VARIANCE_PCT, True),
    ("net", "Net pay", NET_PAY_VARIANCE_PCT, True),
)


def _metric_value(run, key):
    if key == "workers":
        return run.total_workers or 0
    if key == "gross":
        return run.total_gross_pay or 0
    if key == "deductions":
        return run.total_deductions or 0
    if key == "taxes":
        return (run.total_paye or 0) + (run.total_ssnit or 0)
    if key == "net":
        return run.total_net_pay or 0
    return 0


def compare_to_previous(run):
    """Compare ``run`` to the client's previous closed run across headcount,
    gross, deductions, taxes, and net pay.

    Returns ``{"previous": prev_run_or_None, "rows": [...]}`` where each row is
    ``{key, label, current, previous, delta, pct, flag, is_money}``. ``pct`` is the
    fractional change (None when the previous value is 0) and ``flag`` marks a
    change beyond that metric's risk threshold — the 'unusual change' highlight."""
    prev = _previous_closed_run(run)
    if prev is None:
        return {"previous": None, "rows": []}
    rows = []
    for key, label, threshold, is_money in _COMPARISON_METRICS:
        current = _metric_value(run, key)
        previous = _metric_value(prev, key)
        delta = (current or 0) - (previous or 0)
        pct = _pct(current, previous)  # magnitude, for the threshold flag
        flag = pct > threshold if pct is not None else bool(current)
        rows.append(
            {
                "key": key,
                "label": label,
                "current": current,
                "previous": previous,
                "delta": delta,
                "pct": pct,
                "signed_pct": (delta / previous) if previous else None,  # for display
                "flag": flag,
                "is_money": is_money,
            }
        )
    return {"previous": prev, "rows": rows}


# --- Possible-duplicate detection (operator awareness, Phase 2) ------------
# A separate concern from the exact same-client/month/year block enforced at
# import time (see has_duplicate_payroll in app/payroll.py): this looks for
# OTHER runs — any period — whose totals exactly match, which is what a client
# re-uploading the same payroll under the wrong month looks like. Advisory
# only; the caller decides what to show, and nothing here blocks a lifecycle
# transition.


def find_possible_duplicates(run):
    """Other runs for the same client whose worker count and net pay exactly
    match ``run``'s — a signal the same payroll may have been submitted twice.
    Rejected runs are excluded (a resubmission after rejection is expected,
    not a duplicate). Zero-total runs are excluded too, since matching on
    zero is meaningless. Ordered most-recent-first, capped at 5."""
    if not run.client_company_id or not run.total_net_pay or not run.total_workers:
        return []
    return (
        PayrollRun.query.filter(
            PayrollRun.client_company_id == run.client_company_id,
            PayrollRun.id != run.id,
            PayrollRun.status != REJECTED,
            PayrollRun.total_net_pay == run.total_net_pay,
            PayrollRun.total_workers == run.total_workers,
        )
        .order_by(PayrollRun.created_at.desc())
        .limit(5)
        .all()
    )


# --- Combined risk & validation summary (operator awareness, Phase 2) ------
# The risk gate's per-check detail, the row-level validation warning count,
# comparison-to-previous flags, and possible-duplicate matches each already
# exist (evaluate_run, PayrollRun.warning_count, compare_to_previous,
# find_possible_duplicates) but were scattered across the detail page — one in
# a hover tooltip, one nowhere at all. This distills them into a single list
# of plain-English next steps for the "Risk & Validation Summary" panel.
# Advisory only: presentation over existing signals, no new rule and no
# lifecycle decision.


def build_recommendations(run, verdict, comparison, duplicates):
    """Plain-English follow-ups derived from ``verdict`` (evaluate_run),
    ``run``'s row-level warnings, ``comparison`` (compare_to_previous), and
    ``duplicates`` (find_possible_duplicates). Empty list when nothing needs a
    second look."""
    recommendations = [check.detail for check in verdict.tripped]
    if run.warning_count:
        recommendations.append(
            f"{run.warning_count} row-level warning(s) in the Payroll Items Grid — "
            "review before approving."
        )
    if duplicates:
        recommendations.append(
            f"{len(duplicates)} possible duplicate run(s) found — verify before processing."
        )
    flagged = [row["label"] for row in comparison.get("rows", []) if row["flag"]]
    if flagged:
        recommendations.append(
            f"Unusual change vs the previous run in {', '.join(flagged)} — confirm with the client."
        )
    return recommendations


def apply_risk_gate(run, when=None):
    """Evaluate ``run`` and persist the verdict onto it. Returns the verdict.

    Sets run.risk_status / run.risk_reasons / run.risk_checked_at. Does NOT change
    run.status or commit — the caller owns the lifecycle transition and the commit
    (so the status move and the verdict are written in one transaction).

    ``when`` is the timestamp to stamp (pass one in; this module never calls
    datetime.now itself so it stays pure and testable).
    """
    verdict = evaluate_run(run)
    run.risk_status = verdict.status
    run.risk_reasons = verdict.reasons_text() or None
    run.risk_checked_at = when
    return verdict


def release_risk_hold(run):
    """Clear the *hold* from a run's persisted verdict, keeping its history.

    The caller owns the PayrollRun.status transition (Held -> Pending Approval)
    and the commit; this only moves risk_status ``held`` -> ``released`` so no
    risk_status-derived indicator keeps reporting a hold that no longer exists.
    ``risk_reasons`` is deliberately preserved — *why* it was held stays on the
    record for the audit trail and the operator's own reference.

    Idempotent: a run that was never held is left untouched."""
    if run.risk_status == RISK_HELD:
        run.risk_status = RISK_RELEASED


# --- Risk summary (one source of truth for every risk indicator) ------------
# "Currently held" is PayrollRun.status == Held — the lifecycle state, which is
# what the operator actually acts on. risk_status is the *verdict record*, and
# is NOT a substitute: both gate call sites (oversight.risk_check and the client
# import in app/client/__init__.py) write status and risk_status together, so the
# two agree at scoring time, but only status keeps tracking reality afterwards
# as the run is released, approved, or rejected.
#
# Every risk counter/list/badge in the app routes through the helpers below, so a
# lifecycle change is reflected on the operator dashboard, the risk queue, the run
# detail page, and the client's own dashboard from the same query and the same
# labels. Nothing here caches — each page render re-reads the current state, so
# there is no cache to invalidate and no polling to add.


def held_run_criterion():
    """SQLAlchemy criterion for 'this run is currently held by the risk gate'."""
    return PayrollRun.status == HELD


def held_run_count():
    """How many runs are held right now, across every tenant."""
    return PayrollRun.query.filter(held_run_criterion()).count()


# The orders the review queue offers, and the one it opens on.
#
# `oldest` is the default, and the change is deliberate: this is a WORK queue,
# and a work queue sorted newest-first serves the client who has waited least.
# A held run is a payroll nobody can pay until an operator looks at it, so the
# run at the top should be the one that has been waiting longest.
#
# The dashboard's Held panel keeps calling held_runs() with no argument and so
# keeps its newest-first order — there it is a feed of what just happened, not a
# list of work to do, and the two want opposite orders for good reasons.
QUEUE_ORDERS = {
    "oldest": "Longest held",
    "newest": "Most recent",
    "value": "Largest payroll",
}
DEFAULT_QUEUE_ORDER = "oldest"


def held_runs_query(order="newest"):
    """Held runs, unexecuted — so the queue page can page it rather than
    materialising every held run (Phase 7, Task 7.1).

    ``order`` is one of QUEUE_ORDERS, or ``newest`` (the historical default,
    which the operator dashboard's panel relies on). An unknown value falls back
    to the default rather than raising: it arrives from a query string.
    """
    query = PayrollRun.query.filter(held_run_criterion())
    if order == "oldest":
        # NULLs last would need a dialect-specific clause; a run scored before
        # risk_checked_at existed sorts first here, which is the safe direction
        # for a queue whose whole job is to surface the longest wait.
        return query.order_by(PayrollRun.risk_checked_at.asc(), PayrollRun.id.asc())
    if order == "value":
        return query.order_by(PayrollRun.total_net_pay.desc(), PayrollRun.id.desc())
    return query.order_by(PayrollRun.risk_checked_at.desc(), PayrollRun.id.desc())


def held_runs(limit=None):
    """Held runs, newest first. ``limit`` caps the list for dashboard panels."""
    query = held_runs_query()
    return (query.limit(limit).all() if limit else query.all())


# --- The review queue's own view of a held run -----------------------------
# Everything below is presentation support for /oversight/risk. It measures
# nothing new: the verdict is the one the gate persisted, and the comparison is
# the one _previous_closed_run already defines. What it adds is the shape the
# queue needs — a rule per chip rather than a run-on sentence, the movement that
# caused the hold as a number, and the stake, so an operator can rank five holds
# without opening five runs.

# Maps a persisted reason sentence back to the rule that wrote it, for a short
# chip label. Matched on the fixed phrasing evaluate_run emits (above), longest
# first so "worker count" cannot be claimed by a looser pattern.
_REASON_RULES = (
    ("new-client", "New client", ("new-client", "runs are always reviewed", "first ")),
    ("net_pay", "Net pay", ("net pay",)),
    ("headcount", "Headcount", ("workers vs previous", "worker count", "0 workers")),
)


def reason_items(run):
    """``run.risk_reasons`` as one item per tripped rule.

    The queue used to print this field raw, so a run tripping two rules read as
    one long sentence with a pipe in the middle of it. The persisted text is the
    record of WHY the run was held and is not rewritten here — it is split on
    the separator reasons_text() joins with, and each part is labelled with the
    rule that produced it. Text this cannot classify keeps its own words under a
    neutral label rather than being dropped, so a reason written by an older
    version of the gate still reaches the operator.
    """
    raw = (run.risk_reasons or "").strip()
    if not raw:
        return []
    items = []
    for part in (p.strip() for p in raw.split("|")):
        if not part:
            continue
        lowered = part.lower()
        code, label = "other", "Flagged"
        for rule_code, rule_label, needles in _REASON_RULES:
            if any(needle in lowered for needle in needles):
                code, label = rule_code, rule_label
                break
        items.append({"code": code, "label": label, "detail": part})
    return items


def queue_rows(runs):
    """Each held run with the facts a reviewer ranks on: why, by how much, and
    what is at stake.

    One extra query per run, for that run's previous closed run — the SAME
    baseline function the gate scored against, so the movement shown here and
    the verdict shown beside it can never describe different comparisons. A
    cheaper batched lookup would have to re-implement "most recent closed run
    per client" and could then drift from it; at a page of held runs (the queue
    is work waiting on a human, so it is small by definition, and paged besides)
    correctness is worth more than the round trips.
    """
    rows = []
    for run in runs:
        previous = _previous_closed_run(run)
        prev_net = (previous.total_net_pay or 0) if previous else None
        prev_workers = (previous.total_workers or 0) if previous else None
        this_net = run.total_net_pay or 0
        this_workers = run.total_workers or 0
        # Signed fractional change, for macros/charts.html::delta. None where
        # there is no baseline — which is itself the reason a first run is held,
        # and the template says so in those words rather than drawing a dash.
        net_change = (this_net - prev_net) / prev_net if prev_net else None
        worker_change = (
            (this_workers - prev_workers) / prev_workers if prev_workers else None
        )
        reasons = reason_items(run)
        rows.append(
            {
                "run": run,
                "reasons": reasons,
                "previous": previous,
                "net_change": net_change,
                "worker_change": worker_change,
                "previous_net": prev_net,
                "previous_workers": prev_workers,
                "stale": _verdict_is_stale(reasons, net_change, worker_change),
                # The ranking fact, as a duration. Same phrasing as the summary
                # band's "Longest wait" tile, so the tile and the column it
                # describes cannot read as two different measurements.
                "age": _age_phrase(run.risk_checked_at),
            }
        )
    return rows


def _verdict_is_stale(reasons, net_change, worker_change):
    """Whether the recorded reason no longer describes today's comparison.

    A verdict is a photograph: it was taken against whichever closed run was the
    client's most recent AT SCORING TIME. If a later run has since been approved,
    the baseline moves under it, and the sentence stored in ``risk_reasons`` can
    end up describing a comparison nobody can reproduce from the current data —
    "net pay moved +38.4%" beside a row that now reads -2.1%.

    Putting the movement on the row is what makes that visible; this is what
    stops it reading as a contradiction. The queue keeps showing the recorded
    reason (it is the audit record of why the hold exists, and it is not
    rewritten here) and marks the row so the operator knows to re-check rather
    than to distrust the page.

    Costs nothing: it re-uses the comparison already computed above, and never
    re-runs the gate — re-scoring a run is a deliberate operator action with its
    own route and its own audit entry, not something a list page does silently.
    """
    tripped = {reason["code"] for reason in reasons}
    if "net_pay" in tripped and net_change is not None:
        if abs(net_change) <= NET_PAY_VARIANCE_PCT:
            return True
    if "headcount" in tripped and worker_change is not None:
        if abs(worker_change) <= HEADCOUNT_SWING_PCT:
            return True
    return False


def queue_summary():
    """What the whole queue is holding: how many runs, how many workers, how
    much money, and how long the oldest has waited.

    The page listed five runs and stated none of this. "Five runs held" is a
    number of rows; "GH₵ 223,813 of payroll and 109 workers, oldest waiting
    three days" is the reason the page exists. Three aggregates in one query,
    over a table already filtered to a state a human is expected to clear."""
    row = (
        db.session.query(
            func.count(PayrollRun.id),
            func.coalesce(func.sum(PayrollRun.total_net_pay), 0),
            func.coalesce(func.sum(PayrollRun.total_workers), 0),
            func.min(PayrollRun.risk_checked_at),
        )
        .filter(held_run_criterion())
        .one()
    )
    count, net_pay, workers, oldest = row
    return {
        "count": int(count or 0),
        "net_pay": float(net_pay or 0),
        "workers": int(workers or 0),
        "oldest_checked_at": oldest,
        "oldest_age": _age_phrase(oldest),
    }


def _age_phrase(moment, now=None):
    """How long ago, as a DURATION rather than as a date.

    app.events.relative_time is right for a feed ("26 Jul 2026" is what you want
    beside an entry from July), and wrong for a tile whose label is "Longest
    wait" — a date there makes the reader do the subtraction, which is the one
    piece of arithmetic the tile exists to save them. Coarsens the same way, and
    clamps a future timestamp to zero rather than reporting a negative wait: two
    app servers with a few seconds of clock skew must not make the queue print
    nonsense."""
    if moment is None:
        return None
    from datetime import datetime, timezone

    from app.events import as_utc

    moment = as_utc(moment)
    now = as_utc(now) or datetime.now(timezone.utc)
    seconds = max((now - moment).total_seconds(), 0)
    if seconds < 3600:
        minutes = int(seconds // 60)
        return "under a minute" if minutes < 1 else f"{minutes} minute{'' if minutes == 1 else 's'}"
    if seconds < 86400:
        hours = int(seconds // 3600)
        return f"{hours} hour{'' if hours == 1 else 's'}"
    days = int(seconds // 86400)
    return f"{days} day{'' if days == 1 else 's'}"


def risk_summary(limit=8):
    """The platform-wide risk picture: ``{"held_count": int, "held_runs": [...]}``.

    Used by the operator dashboard (counter tiles, Action Required, the Held
    panel) so those three numbers can never disagree with each other or with the
    risk queue."""
    return {"held_count": held_run_count(), "held_runs": held_runs(limit=limit)}


# Badge label + tone per persisted verdict. One mapping, so the run detail page
# and the tenant portal describe the same run identically.
_RISK_BADGE = {
    RISK_HELD: ("Held", "warning"),
    RISK_ACCEPTED: ("Auto-accepted", "success"),
    RISK_RELEASED: ("Released", "info"),
}


def risk_badge(run):
    """``{"label", "tone", "reasons"}`` for a run's risk verdict, or None if the
    run was never scored. ``tone`` is a semantic name (warning/success/info) the
    template maps to its own shell's classes."""
    label_tone = _RISK_BADGE.get(run.risk_status)
    if label_tone is None:
        return None
    label, tone = label_tone
    return {"label": label, "tone": tone, "reasons": run.risk_reasons or ""}


def is_held(run):
    """Whether ``run`` is currently held — the row-level twin of
    :func:`held_run_criterion`, for templates and in-Python checks."""
    return run.status == HELD


def was_ever_held(run):
    """Whether ``run`` passed through the risk-hold branch at any point."""
    return run.risk_status in RISK_EVER_HELD or run.status == HELD
