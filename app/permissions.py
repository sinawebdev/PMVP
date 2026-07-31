"""Operator (platform-plane) capability groups — the single source of truth for
"which operator roles may do X".

Previously these role lists were duplicated inline across templates (the
`base.html` sidebar, payroll action buttons) and route decorators
(`REP_ROLES` in employees, `PAYROLL_ROLES` in distribution, and ad-hoc
``@role_required("admin", "md", ...)`` tuples). Centralising them here means the
navigation a user sees and the routes they may hit can never drift apart.

Membership matched the pre-refactor lists exactly at introduction (pure
de-duplication); the one deliberate policy change since is granting
``payrolla_admin`` full operator access (confirmed with Sina) — it joins every
group below.

**Superusers are no longer a special case (Phase 1, Task 1.1).** ``md`` and
``payrolla_admin`` used to pass *every* ``role_required`` check via a bypass in
:func:`app.auth.role_required`, while these groups gated the templates. Two
vocabularies answered the same question, so a superuser was routinely authorized
for an action whose button it was never shown — Export Payroll, Wage Rates, Raw
Hours and Employee delete among them. The bypass is gone; both superusers are now
listed explicitly in every group they previously reached through it, so a single
predicate backs the route guard *and* the affordance. Effective access is
unchanged by that move — ``tests/test_permission_parity.py`` pins it.

Adding a route therefore means putting its roles in a group here, never a literal
tuple in the decorator: a literal cannot be read by a template, which is how the
two drifted apart in the first place.

Tenant (client) roles are governed separately in :mod:`app.roles` /
:func:`app.tenancy.tenant_role_required`. The operator groups here never apply to
them. A small tenant-capability section lives at the bottom of this file so
"which role may do X" has one home across both planes — but the *plane* is still
decided solely by ``User.client_company_id``, so a tenant role can never widen a
tenant user's data horizon.
"""

from app.payroll_status import (
    APPROVED,
    DELETABLE_STATUSES,
    DRAFT,
    PENDING_STATUSES,
    SENDABLE_STATUSES,
)
from app.roles import CLIENT_ADMIN, CLIENT_PREPARER, PAYROLLA_ADMIN, normalise_role

# The two operator superusers. Every group below contains both, which is what
# retires the old role_required bypass: "superuser" is now membership, not an
# exemption, so a template asking "may this role do X" gets the same answer the
# route guard does.
SUPERUSERS = ("md", PAYROLLA_ADMIN)

# Full payroll operations: upload, process, view payslips, export, distribute.
PAYROLL_ROLES = frozenset(
    {"admin", "payroll_officer", "accounts_officer", *SUPERUSERS}
)

# Employee-roster maintenance. ``md`` reached these routes through the bypass
# rather than the list; it is now a member outright, which is also what puts the
# roster's own controls in front of it.
REP_ROLES = frozenset({"admin", "payroll_officer", "accounts_officer", *SUPERUSERS})

# Oversight of expenses + the audit trail.
AUDIT_ROLES = frozenset({"admin", *SUPERUSERS})

# Statutory-rate administration. ``md`` reached these routes via the bypass.
STATUTORY_ROLES = frozenset({"admin", *SUPERUSERS})

# Destructive roster action: remove an employee record outright.
ROSTER_DELETE_ROLES = frozenset({"admin", *SUPERUSERS})

# The payroll import pipeline: preview an upload, read its error report, confirm it.
PAYROLL_IMPORT_ROLES = frozenset({"admin", *SUPERUSERS})

# Per-client hourly wage-rate profiles.
WAGE_RATE_ROLES = frozenset({"admin", *SUPERUSERS})

# The raw-hours engine: upload/confirm raw timesheets and pull its exports.
RAW_ENGINE_ROLES = frozenset({"admin", *SUPERUSERS})

# Create/edit client company records on the platform plane.
CLIENT_MANAGEMENT_ROLES = frozenset({"admin", *SUPERUSERS})

# Platform infrastructure surfaces (database health). Operator-only telemetry —
# not business function; Phase 3 moves its links out of user-facing nav.
PLATFORM_OPS_ROLES = frozenset({"admin", *SUPERUSERS})


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


def can_delete_employee(role):
    """May remove an employee record from the roster (destructive)."""
    return _in(role, ROSTER_DELETE_ROLES)


def can_import_payroll(role):
    """May preview, error-report and confirm a payroll workbook import."""
    return _in(role, PAYROLL_IMPORT_ROLES)


def can_manage_wage_rates(role):
    """May view/edit a client's hourly wage-rate profiles."""
    return _in(role, WAGE_RATE_ROLES)


def can_use_raw_engine(role):
    """May upload/confirm raw hours and pull the raw-engine exports."""
    return _in(role, RAW_ENGINE_ROLES)


def can_manage_clients(role):
    """May create/edit client company records."""
    return _in(role, CLIENT_MANAGEMENT_ROLES)


def can_view_platform_ops(role):
    """May open platform infrastructure/telemetry surfaces (database health)."""
    return _in(role, PLATFORM_OPS_ROLES)


# --- Payroll-run lifecycle authorization -------------------------------------
# Each transition of a PayrollRun is gated by a "who" (a role group below) AND a
# "when" (which run statuses permit it). Both used to live inline as
# ``current_user.role in [...] and payroll_run.status in [...]`` expressions
# scattered across payroll_detail.html, with the role halves duplicated again in
# each route's ``@role_required(...)`` tuple. The predicates here are the single
# source of truth for both the button a user sees and the route they may hit, so
# the two can never drift apart.
#
# Membership mirrors the pre-refactor template role lists, plus payrolla_admin.
#
# ``md`` is deliberately NOT in the three groups below, and retiring the bypass
# therefore *narrows* its route access rather than preserving it. That is the
# correction, not a regression: md approves and rejects runs, so letting it also
# prepare or alter the figures it signs off would collapse maker-checker into one
# role. The templates already refused to show md these buttons; the bypass was
# quietly granting the route anyway, which is exactly the drift Task 1.1 exists to
# remove. Pinned by test_md_approves_and_deletes_but_cannot_submit.

# Recalculate statutory pay (Calculate Pay) — admin only, historically.
CALCULATE_ROLES = frozenset({"admin", PAYROLLA_ADMIN})

# Open the raw-figures grid (Edit Figures). Row edits are further gated to Draft
# inside the route; the grid is viewable read-only at any status.
EDIT_FIGURES_ROLES = frozenset({"admin", "payroll_officer", PAYROLLA_ADMIN})

# Move a Draft run to Pending Approval (Submit for Approval).
SUBMIT_APPROVAL_ROLES = frozenset({"admin", "accounts_officer", PAYROLLA_ADMIN})

# Approve or reject a run awaiting sign-off.
APPROVAL_ROLES = frozenset({"admin", *SUPERUSERS})

# Close an approved run (Mark Processed).
MARK_PROCESSED_ROLES = frozenset({"admin", "accounts_officer", *SUPERUSERS})

# Hard-delete a run. Deliberately narrower than export: destroying payroll
# history is admin/MD only (see the delete route).
DELETE_ROLES = frozenset({"admin", *SUPERUSERS})


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
    """May approve a run awaiting sign-off (Draft or Pending Approval).

    A run with no worker rows cannot be approved at all, by anyone. Approval is
    the sign-off that releases payslips, and signing off on an empty run means
    attesting to a payroll that pays nobody — the likeliest cause is an import
    that silently produced nothing, which approving would then bless. The status
    check alone used to allow it (Phase 2, Task 2.4)."""
    if (getattr(run, "total_workers", 0) or 0) <= 0:
        return False
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


# --- Tenant (client plane) capabilities --------------------------------------
# The mirror of the operator groups above, for roles inside ONE tenant. Same
# contract: the predicate gates both the control a user sees and the route they
# may hit, so the two cannot drift. These say what a tenant role may DO; the
# plane (and therefore the data horizon) is still resolved only from
# ``User.client_company_id`` in app/tenancy.py.

# Record, edit and delete the company's own operational expenses. Deliberately
# the same pair that may prepare a payroll run — expense capture introduces no
# new permission concept, it reuses the "may act on this company's books" role.
EXPENSE_ROLES = frozenset({CLIENT_ADMIN, CLIENT_PREPARER})


def can_record_expenses(role):
    """May create/edit/delete their own company's expenses (tenant plane)."""
    return _in(role, EXPENSE_ROLES)


# Edit the company's own payslip branding pack. client_admin-only, mirroring the
# route's ``@tenant_role_required(CLIENT_ADMIN)``.
BRANDING_ROLES = frozenset({CLIENT_ADMIN})


def can_manage_branding(role):
    """May edit their own company's payslip branding (tenant plane)."""
    return _in(role, BRANDING_ROLES)


def can_delete_run(role, run):
    """May hard-delete a run. Role AND status must both allow it; the delete
    route additionally checks record-level blockers (voucher, remittances, sent
    payslips, linked expenses) that this predicate does not — it gates the
    button, not the irreversible action."""
    return _in(role, DELETE_ROLES) and run.status in DELETABLE_STATUSES
