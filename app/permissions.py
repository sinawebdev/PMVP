"""Operator (platform-plane) capability groups — the single source of truth for
"which operator roles may do X".

Previously these role lists were duplicated inline across templates (the
`base.html` sidebar, payroll action buttons) and route decorators
(`REP_ROLES` in employees, `PAYROLL_ROLES` in distribution, and ad-hoc
``@role_required("admin", "md", ...)`` tuples). Centralising them here means the
navigation a user sees and the routes they may hit can never drift apart.

Membership matched the pre-refactor lists exactly at introduction (pure
de-duplication); the one deliberate policy change since is granting
``chrisnat_admin`` full operator access (confirmed with Sina) — it joins every
group below. Two legacy behaviours are deliberately left untouched:

  * The ``md`` role additionally passes *every* ``role_required`` check via a
    special case in :func:`app.auth.role_required`. That is independent of these
    groups (which gate templates literally) and is unchanged here.
  * Tenant (client) roles are governed separately in :mod:`app.roles` /
    :func:`app.tenancy.tenant_role_required`; this module is operator-plane only.
"""

from app.payroll_status import (
    APPROVED,
    DELETABLE_STATUSES,
    DRAFT,
    PENDING_STATUSES,
    SENDABLE_STATUSES,
)
from app.roles import CHRISNAT_ADMIN, normalise_role

# ``chrisnat_admin`` is the SaaS-era platform superuser: it joins every operator
# capability group here (drives nav), and app.auth.role_required passes it on
# every operator route (mirroring ``md``). Legacy operator roles are unchanged.

# Full payroll operations: upload, process, view payslips, distribute.
PAYROLL_ROLES = frozenset(
    {"admin", "md", "payroll_officer", "accounts_officer", CHRISNAT_ADMIN}
)

# Employee-roster maintenance. Historically excludes ``md`` from the decorator
# list; ``md`` still reaches these *routes* via role_required's md special-case.
REP_ROLES = frozenset({"admin", "payroll_officer", "accounts_officer", CHRISNAT_ADMIN})

# Oversight of expenses + the audit trail.
AUDIT_ROLES = frozenset({"admin", "md", CHRISNAT_ADMIN})

# Statutory-rate administration.
STATUTORY_ROLES = frozenset({"admin", CHRISNAT_ADMIN})


def _in(role, group):
    return normalise_role(role) in group


def can_operate_payroll(role):
    """May run/process payroll, view payslips, and distribute them."""
    return _in(role, PAYROLL_ROLES)


def can_maintain_roster(role):
    """May create/edit/deactivate employees on the operator plane."""
    return _in(role, REP_ROLES)


def can_view_audit(role):
    """May view the expenses + audit-trail oversight area."""
    return _in(role, AUDIT_ROLES)


def can_manage_statutory(role):
    """May administer statutory rates."""
    return _in(role, STATUTORY_ROLES)


# --- Payroll-run lifecycle authorization -------------------------------------
# Each transition of a PayrollRun is gated by a "who" (a role group below) AND a
# "when" (which run statuses permit it). Both used to live inline as
# ``current_user.role in [...] and payroll_run.status in [...]`` expressions
# scattered across payroll_detail.html, with the role halves duplicated again in
# each route's ``@role_required(...)`` tuple. The predicates here are the single
# source of truth for both the button a user sees and the route they may hit, so
# the two can never drift apart.
#
# Membership mirrors the pre-refactor template role lists exactly, plus
# ``chrisnat_admin`` — which already passed every one of these routes via
# app.auth.role_required's superuser bypass, so it can perform the action; adding
# it here just lets it SEE the corresponding button (completing the "full
# operator access" policy). ``md`` likewise passes every route via that bypass,
# but is listed explicitly wherever it is a first-class actor (approval, delete,
# …) so the template shows it the button without relying on the bypass.

# Recalculate statutory pay (Calculate Pay) — admin only, historically.
CALCULATE_ROLES = frozenset({"admin", CHRISNAT_ADMIN})

# Open the raw-figures grid (Edit Figures). Row edits are further gated to Draft
# inside the route; the grid is viewable read-only at any status.
EDIT_FIGURES_ROLES = frozenset({"admin", "payroll_officer", CHRISNAT_ADMIN})

# Move a Draft run to Pending Approval (Submit for Approval).
SUBMIT_APPROVAL_ROLES = frozenset({"admin", "accounts_officer", CHRISNAT_ADMIN})

# Approve or reject a run awaiting sign-off.
APPROVAL_ROLES = frozenset({"admin", "md", CHRISNAT_ADMIN})

# Close an approved run (Mark Processed).
MARK_PROCESSED_ROLES = frozenset(
    {"admin", "accounts_officer", "md", CHRISNAT_ADMIN}
)

# Hard-delete a run. Deliberately narrower than export: destroying payroll
# history is admin/MD only (see the delete route).
DELETE_ROLES = frozenset({"admin", "md", CHRISNAT_ADMIN})


def can_calculate_run(role, run):
    """May run Calculate Pay — only while the run is still open (Draft or
    Pending Approval); a closed/rejected run's figures are frozen."""
    return _in(role, CALCULATE_ROLES) and run.status in PENDING_STATUSES


def can_edit_run_figures(role):
    """May open the raw-figures grid. Status-independent (the grid is read-only
    once the run leaves Draft, enforced in the route), so button visibility is
    role-only — matching the pre-refactor template."""
    return _in(role, EDIT_FIGURES_ROLES)


def can_submit_run_for_approval(role, run):
    """May submit a Draft run for approval."""
    return _in(role, SUBMIT_APPROVAL_ROLES) and run.status == DRAFT


def can_bulk_approve_reject(role):
    """May see the bulk approve/reject controls on the runs list (role-only —
    each selected run's actual eligibility is still checked per-row by
    can_approve_run/can_reject_run when the bulk action runs)."""
    return _in(role, APPROVAL_ROLES)


def can_approve_run(role, run):
    """May approve a run awaiting sign-off (Draft or Pending Approval)."""
    return _in(role, APPROVAL_ROLES) and run.status in PENDING_STATUSES


def can_reject_run(role, run):
    """May reject a run awaiting sign-off (Draft or Pending Approval)."""
    return _in(role, APPROVAL_ROLES) and run.status in PENDING_STATUSES


def can_mark_run_processed(role, run):
    """May mark an Approved run as Processed (accounts closes the run)."""
    return _in(role, MARK_PROCESSED_ROLES) and run.status == APPROVED


# Distribute a run's payslips. Unlike the transitions above it needs no bespoke
# lifecycle role group: "who may distribute" is exactly "who may operate payroll",
# so it reuses the canonical PAYROLL_ROLES group that the /distribution routes
# already gate on via ``@role_required(*PAYROLL_ROLES)``. The "when" is the
# centralized SENDABLE_STATUSES (Approved or Processed) — the same group the
# distribution routes check — so a run stays distributable after it closes
# (Processed). The legacy "Paid" status was renamed to Processed and no longer
# exists; this predicate replaces the last inline ``status in ["Approved",
# "Paid"]`` gate in payroll_detail.html.


def can_distribute_run(role, run):
    """May distribute a run's payslips (open the delivery surface, send/resend).
    Role AND status must both allow it: an operator payroll role (PAYROLL_ROLES)
    and a finalized run (SENDABLE_STATUSES = Approved or Processed). Backs both
    the payroll-detail "Distribute Payslips" button and the /distribution routes'
    status guard, so the button a user sees and the route they may hit derive
    from one rule."""
    return _in(role, PAYROLL_ROLES) and run.status in SENDABLE_STATUSES


def can_delete_run(role, run):
    """May hard-delete a run. Role AND status must both allow it; the delete
    route additionally checks record-level blockers (voucher, remittances, sent
    payslips, linked expenses) that this predicate does not — it gates the
    button, not the irreversible action."""
    return _in(role, DELETE_ROLES) and run.status in DELETABLE_STATUSES
