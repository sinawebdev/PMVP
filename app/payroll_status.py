"""Canonical PayrollRun status values and groupings.

Every route and filter must import from here — a typo becomes an
AttributeError instead of a silently empty query filter.
"""

DRAFT = "Draft"
PENDING_APPROVAL = "Pending Approval"  # single intermediate state (no review stage)
APPROVED = "Approved"
REJECTED = "Rejected"
PROCESSED = "Processed"  # terminal; accounts closes the run

# Risk-gate lifecycle (Payrolla Phase 5). A submitted run is scored by app/risk.py:
#   SUBMITTED      -> just submitted, awaiting the risk gate (transient)
#   HELD           -> tripped a risk rule; parked for platform oversight review
#   AUTO_ACCEPTED  -> passed every risk rule; ready for operator approval
# HELD/AUTO_ACCEPTED sit between submission and PENDING_APPROVAL; the platform operator
# releases a HELD run into PENDING_APPROVAL (or REJECTED).
SUBMITTED = "Submitted"
HELD = "Held"
AUTO_ACCEPTED = "Auto-Accepted"

# Statuses the risk gate may (re)evaluate — never a closed/rejected run.
RISK_GATED_STATUSES = (DRAFT, PENDING_APPROVAL, SUBMITTED, HELD, AUTO_ACCEPTED)

# --- PayrollRun.risk_status vocabulary --------------------------------------
# The persisted *verdict* of the risk gate, distinct from PayrollRun.status (the
# lifecycle state). Kept here — beside the status vocabulary and with no imports
# of its own — so both app/risk.py and app/payroll_status.py can read it without
# a circular import.
#
#   held      — a rule tripped; the run is parked for oversight review
#   accepted  — no rule tripped
#   released  — WAS held, and an operator released it into approval
#   None      — never scored
#
# RISK_RELEASED exists because a released run must stop reading as held
# everywhere (dashboard counters, run badges, the client's own view) while the
# lifecycle stepper still shows that it passed *through* the hold branch. Before
# it, releasing a run moved PayrollRun.status off Held but left risk_status at
# 'held' forever, so every risk_status-derived indicator went stale.
RISK_HELD = "held"
RISK_ACCEPTED = "accepted"
RISK_RELEASED = "released"

# The run went through the risk-hold branch at some point (stepper history).
RISK_EVER_HELD = (RISK_HELD, RISK_RELEASED)

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
    # A released run sits at Pending Approval, which maps back to the Submitted
    # stage — so the ``idx < reached`` rule alone would render its Held step as a
    # *future* step even though the hold is already behind it. It was held and is
    # no longer: that step is done.
    cleared_hold = held and status != HELD
    steps = []
    for key, label in LIFECYCLE_STAGES:
        idx = _STAGE_INDEX[key]
        if key == "held" and not held:
            state = "skipped"
        elif key == "held" and cleared_hold and not rejected:
            state = "done"
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


# --- What to do next --------------------------------------------------------
# PENDING_STATUSES / SENDABLE_STATUSES / DELETABLE_STATUSES all answer the same
# question — "is this allowed?" — which is why a Draft run can legitimately offer
# Calculate, Submit, Approve, Reject and Delete at once: five permitted actions
# and nothing saying which one actually moves the run forward.
#
# This answers the other question: given the run's real state, what is the ONE
# next step? It is deliberately role-blind — it describes what the *run* needs,
# not what a given user may do. Callers intersect it with the permission
# predicates in app/permissions.py, so neither concern quietly absorbs the other.
#
# Presentation-free too: it returns a key and a plain-language label, never
# markup or a URL, so the same answer serves the operator console, the tenant
# portal and any test.

ACTION_CALCULATE = "calculate"
ACTION_FIX_IMPORT = "fix_import"
ACTION_SUBMIT = "submit"
ACTION_AWAIT_RISK = "await_risk"
ACTION_REVIEW_HOLD = "review_hold"
ACTION_APPROVE = "approve"
ACTION_MARK_PROCESSED = "mark_processed"
ACTION_DISTRIBUTE = "distribute"
ACTION_RESOLVE_REJECTION = "resolve_rejection"

# key -> (label, why). `why` is the one-line justification a Decision Header
# shows under the action, so the user is told the reason and not just the verb.
_ACTION_COPY = {
    ACTION_FIX_IMPORT: (
        "Fix the import",
        "This run has no worker rows — the uploaded workbook produced nothing to pay.",
    ),
    ACTION_CALCULATE: (
        "Calculate pay",
        "Statutory figures have not been computed for this run yet.",
    ),
    ACTION_SUBMIT: (
        "Submit for approval",
        "Figures are calculated and the run is ready for sign-off.",
    ),
    ACTION_AWAIT_RISK: (
        "Awaiting risk check",
        "The risk gate is still scoring this run; no action is needed yet.",
    ),
    ACTION_REVIEW_HOLD: (
        "Review the risk hold",
        "A risk rule tripped — release the run into approval or reject it.",
    ),
    ACTION_APPROVE: (
        "Approve",
        "The run is waiting for sign-off before payslips can go out.",
    ),
    ACTION_MARK_PROCESSED: (
        "Mark processed",
        "Approved — close the run once payment has been settled.",
    ),
    ACTION_DISTRIBUTE: (
        "Distribute payslips",
        "The run is finalized and its workers have not been sent their payslips.",
    ),
    ACTION_RESOLVE_REJECTION: (
        "Resolve the rejection",
        "This run was rejected — correct and re-upload it, or delete it.",
    ),
}


def _action(key):
    label, why = _ACTION_COPY[key]
    return {"key": key, "label": label, "why": why}


def recommended_action_for(run, distributed=False):
    """The single next step for ``run``, as ``{key, label, why}`` — or ``None``
    when the run needs nothing further (fully distributed).

    ``distributed`` is passed in rather than queried, for the same N+1 reason
    :func:`run_progress` takes it: list pages precompute one membership test.
    """
    status = run.status
    has_workers = (getattr(run, "total_workers", 0) or 0) > 0

    if status == REJECTED:
        return _action(ACTION_RESOLVE_REJECTION)
    if status == DRAFT:
        if not has_workers:
            return _action(ACTION_FIX_IMPORT)
        if not _is_calculated(run):
            return _action(ACTION_CALCULATE)
        return _action(ACTION_SUBMIT)
    if status == SUBMITTED:
        return _action(ACTION_AWAIT_RISK)
    if status == HELD:
        return _action(ACTION_REVIEW_HOLD)
    if status in (AUTO_ACCEPTED, PENDING_APPROVAL):
        return _action(ACTION_APPROVE)
    if status == APPROVED:
        return _action(ACTION_MARK_PROCESSED)
    if status == PROCESSED:
        return None if distributed else _action(ACTION_DISTRIBUTE)
    return None


def _is_calculated(run):
    """Whether statutory figures exist for the run.

    ``total_workers > 0`` says rows were imported, not that they were costed, so
    this keys on money having been computed. Falls back to the worker count only
    when the totals column is absent (a stub in a test)."""
    total_net = getattr(run, "total_net_pay", None)
    if total_net is None:
        return (getattr(run, "total_workers", 0) or 0) > 0
    return (total_net or 0) > 0


def run_progress(run, distributed=False):
    """Convenience wrapper: derive ``calculated`` and ``held`` from a run's scalar
    columns (no extra query) and return :func:`lifecycle_steps`. ``distributed``
    is passed in — detail pages compute it with one query; list pages pass a
    precomputed membership test — so this stays N+1-free."""
    calculated = (getattr(run, "total_workers", 0) or 0) > 0
    # A released run keeps its Held step (it really did pass through the hold),
    # which is why this tests RISK_EVER_HELD rather than RISK_HELD alone.
    held = run.risk_status in RISK_EVER_HELD or run.status == HELD
    return lifecycle_steps(
        run.status, calculated=calculated, distributed=distributed, held=held
    )
