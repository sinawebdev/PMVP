"""Run risk gate — deterministic pre-approval checks (PMVP v1 Phase 5).

When a payroll run is submitted, three rules decide whether it can auto-accept or
must be **held for Chrisnat review**. A run tripping ANY rule is HELD; a run that
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

from app.models import PayrollRun
from app.payroll_status import CLOSED_STATUSES, REJECTED

# --- Settled thresholds (Sina, 2026-07-16) ---------------------------------
FIRST_N_RUNS_HELD = 2        # Rule 1: a client's first N runs are always held.
NET_PAY_VARIANCE_PCT = 0.15  # Rule 2: |Δ total net pay| vs previous closed run.
HEADCOUNT_SWING_PCT = 0.20   # Rule 3: |Δ worker count| vs previous closed run.

RISK_HELD = "held"
RISK_ACCEPTED = "accepted"


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
