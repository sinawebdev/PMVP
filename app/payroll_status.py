"""Canonical PayrollRun status values and groupings.

Every route and filter must import from here — a typo becomes an
AttributeError instead of a silently empty query filter.
"""

DRAFT = "Draft"
PENDING_APPROVAL = "Pending Approval"  # single intermediate state (no review stage)
APPROVED = "Approved"
REJECTED = "Rejected"
PROCESSED = "Processed"  # terminal; accounts closes the run

# Dashboard counter + client card "still needs action" count.
PENDING_STATUSES = (DRAFT, PENDING_APPROVAL)

# Payslip distribution gate.
SENDABLE_STATUSES = {APPROVED, PROCESSED}

# Validators previous-run lookup (runs considered finalized for comparison).
CLOSED_STATUSES = (APPROVED, PROCESSED)
