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
