"""Canonical PayrollRun status values and groupings.

Every route and filter must import from here — a typo becomes an
AttributeError instead of a silently empty query filter.
"""

DRAFT = "Draft"
PENDING_APPROVAL = "Pending Approval"  # single intermediate state (no review stage)
APPROVED = "Approved"
REJECTED = "Rejected"
PROCESSED = "Processed"  # terminal; accounts closes the run

# Risk-gate lifecycle (PMVP v1 Phase 5). A submitted run is scored by app/risk.py:
#   SUBMITTED      -> just submitted, awaiting the risk gate (transient)
#   HELD           -> tripped a risk rule; parked for Chrisnat oversight review
#   AUTO_ACCEPTED  -> passed every risk rule; ready for operator approval
# HELD/AUTO_ACCEPTED sit between submission and PENDING_APPROVAL; Chrisnat
# releases a HELD run into PENDING_APPROVAL (or REJECTED).
SUBMITTED = "Submitted"
HELD = "Held"
AUTO_ACCEPTED = "Auto-Accepted"

# Statuses the risk gate may (re)evaluate — never a closed/rejected run.
RISK_GATED_STATUSES = (DRAFT, PENDING_APPROVAL, SUBMITTED, HELD, AUTO_ACCEPTED)

# Dashboard counter + client card "still needs action" count.
PENDING_STATUSES = (DRAFT, PENDING_APPROVAL)

# Payslip distribution gate.
SENDABLE_STATUSES = {APPROVED, PROCESSED}

# Validators previous-run lookup (runs considered finalized for comparison).
CLOSED_STATUSES = (APPROVED, PROCESSED)

# Hard-delete gate: the only statuses a run may be permanently deleted from.
# Draft/Previewed are pre-approval; Rejected is a terminal dead-end that (per
# the approval workflow) can never have produced a voucher, remittance, or sent
# payslip — so it is exactly as safe to delete as a Draft, and reuploading over
# it should replace it. The delete route layers additional record-level blockers
# (voucher/remittance/linked expenses) on top of this in app/payroll.py.
DELETABLE_STATUSES = {DRAFT, "Previewed", REJECTED}


# --- Lifecycle progress (presentation only) ---------------------------------
# The operator-facing progression, rendered as a visual stepper on the dashboard,
# runs list, and run detail. Purely status-derived — NO business rule lives here;
# the authoritative transitions stay in app/permissions.py + the lifecycle
# routes. "Calculated" and "Distributed" are derived signals (the run has
# computed figures / at least one payslip was sent), not stored statuses.

LIFECYCLE_STAGES = (
    ("draft", "Draft"),
    ("calculated", "Calculated"),
    ("submitted", "Submitted"),
    ("held", "Held"),
    ("approved", "Approved"),
    ("processed", "Processed"),
    ("distributed", "Distributed"),
)
_STAGE_INDEX = {key: index for index, (key, _label) in enumerate(LIFECYCLE_STAGES)}

_STATUS_BADGE = {
    DRAFT: "text-bg-secondary",
    SUBMITTED: "text-bg-info",
    AUTO_ACCEPTED: "text-bg-info",
    HELD: "text-bg-warning",
    PENDING_APPROVAL: "text-bg-warning",
    APPROVED: "text-bg-success",
    PROCESSED: "text-bg-primary",
    REJECTED: "text-bg-danger",
}


def status_badge_class(status):
    """Bootstrap badge class for a run status (used by the status pill macro)."""
    return _STATUS_BADGE.get(status, "text-bg-secondary")


def _reached_stage_index(status, calculated, distributed):
    """Highest stage index the run has reached, from its status + derived flags."""
    if distributed:
        return _STAGE_INDEX["distributed"]
    if status == PROCESSED:
        return _STAGE_INDEX["processed"]
    if status == APPROVED:
        return _STAGE_INDEX["approved"]
    if status == HELD:
        return _STAGE_INDEX["held"]
    if status in (SUBMITTED, AUTO_ACCEPTED, PENDING_APPROVAL):
        return _STAGE_INDEX["submitted"]
    if status == DRAFT and calculated:
        return _STAGE_INDEX["calculated"]
    return _STAGE_INDEX["draft"]


def lifecycle_steps(status, calculated=False, distributed=False, held=False):
    """Ordered stepper for a run: a list of ``{key, label, state}`` where state is
    ``done`` | ``current`` | ``upcoming`` | ``skipped``.

    ``held`` marks whether the run ever entered the risk-hold branch (so the Held
    step reads as passed vs skipped). A Rejected run is terminal: everything up to
    and including Submitted is done, the rest skipped, and no step is current."""
    rejected = status == REJECTED
    reached = _reached_stage_index(status, calculated, distributed)
    submitted_idx = _STAGE_INDEX["submitted"]
    steps = []
    for key, label in LIFECYCLE_STAGES:
        idx = _STAGE_INDEX[key]
        if key == "held" and not held:
            state = "skipped"
        elif rejected:
            state = "done" if idx <= submitted_idx else "skipped"
        elif idx < reached:
            state = "done"
        elif idx == reached:
            # A fully-distributed run is complete — its final step is done, not current.
            state = "done" if (key == "distributed" and distributed) else "current"
        else:
            state = "upcoming"
        steps.append({"key": key, "label": label, "state": state})
    return steps


def run_progress(run, distributed=False):
    """Convenience wrapper: derive ``calculated`` and ``held`` from a run's scalar
    columns (no extra query) and return :func:`lifecycle_steps`. ``distributed``
    is passed in — detail pages compute it with one query; list pages pass a
    precomputed membership test — so this stays N+1-free."""
    calculated = (getattr(run, "total_workers", 0) or 0) > 0
    held = run.risk_status == "held" or run.status == HELD
    return lifecycle_steps(
        run.status, calculated=calculated, distributed=distributed, held=held
    )
