"""Operator (platform-plane) capability groups — the single source of truth for
"which operator roles may do X".

Previously these role lists were duplicated inline across templates (the
`base.html` sidebar, payroll action buttons) and route decorators
(`REP_ROLES` in employees, `PAYROLL_ROLES` in distribution, and ad-hoc
``@role_required("admin", "md", ...)`` tuples). Centralising them here means the
navigation a user sees and the routes they may hit can never drift apart.

Membership matches the pre-refactor lists **exactly** — this is de-duplication,
not a policy change. Two behaviours are deliberately left untouched:

  * The ``md`` role additionally passes *every* ``role_required`` check via a
    special case in :func:`app.auth.role_required`. That is independent of these
    groups (which gate templates literally) and is unchanged here.
  * Tenant (client) roles are governed separately in :mod:`app.roles` /
    :func:`app.tenancy.tenant_role_required`; this module is operator-plane only.
"""

from app.roles import normalise_role

# Full payroll operations: upload, process, view payslips, distribute.
PAYROLL_ROLES = frozenset({"admin", "md", "payroll_officer", "accounts_officer"})

# Employee-roster maintenance. Historically excludes ``md`` from the decorator
# list; ``md`` still reaches these *routes* via role_required's md special-case,
# so dropping it here changes nothing.
REP_ROLES = frozenset({"admin", "payroll_officer", "accounts_officer"})

# Oversight of expenses + the audit trail.
AUDIT_ROLES = frozenset({"admin", "md"})

# Statutory-rate administration.
STATUTORY_ROLES = frozenset({"admin"})


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
