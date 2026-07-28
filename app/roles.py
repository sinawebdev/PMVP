"""Role vocabulary for Payrolla's two planes.

Tenancy is determined by ``User.client_company_id`` — NULL means a platform
(operator) user who operates the oversight/control plane above all tenants; a
non-NULL value means a tenant (client) user hard-scoped to that one company.
Role strings determine *permissions within* a plane; the plane itself is always
decided by ``client_company_id`` (never by the role string alone), so a
misassigned role can never widen a tenant user's data horizon.
"""

# --- Platform (operator) roles: client_company_id IS NULL --------------------
# The platform role vocabulary carries the product's name. It was originally
# ``chrisnat_*`` after the founding operator (Chrisnat Limited); the strings are
# persisted in ``User.role``, so the rename to ``payrolla_*`` shipped as a data
# migration (see ``a4e7c2b81d95_rename_chrisnat_roles_to_payrolla``) rather than
# a bare constant edit.
PAYROLLA_ADMIN = "payrolla_admin"
PAYROLLA_REVIEWER = "payrolla_reviewer"

# Pre-rename spellings, mapped to their current equivalent. Every role string
# entering the app is canonicalised through :func:`normalise_role`, so a row that
# predates the migration — an un-migrated replica, a restored backup, a fixture
# or an external caller still saying ``chrisnat_admin`` — keeps exactly the
# permissions it had. Nothing writes these values any more; they are read-side
# compatibility only, and can be dropped once no such data remains.
LEGACY_ROLE_ALIASES = {
    "chrisnat_admin": PAYROLLA_ADMIN,
    "chrisnat_reviewer": PAYROLLA_REVIEWER,
}

# Legacy internal roles from the single-operator bureau app. Still platform-side
# (client_company_id NULL); kept working so the existing seeded operator logins
# and role_required("admin"/"md"/…) checks don't break during the transition.
LEGACY_PLATFORM_ROLES = frozenset(
    {"admin", "md", "payroll_officer", "accounts_officer", "operations_supervisor", "viewer"}
)
PLATFORM_ROLES = frozenset({PAYROLLA_ADMIN, PAYROLLA_REVIEWER}) | LEGACY_PLATFORM_ROLES

# --- Tenant (client) roles: client_company_id = their company ----------------
CLIENT_ADMIN = "client_admin"        # can approve (maker-checker OFF for v1)
CLIENT_PREPARER = "client_preparer"  # create/edit employees, prepare runs
TENANT_ROLES = frozenset({CLIENT_ADMIN, CLIENT_PREPARER})


def normalise_role(role):
    """The canonical spelling of a role string.

    Trims and lowercases, then folds any pre-rename alias
    (:data:`LEGACY_ROLE_ALIASES`) onto its current name. Because every
    permission check in the app compares roles *after* this call — the capability
    groups in :mod:`app.permissions`, :func:`app.auth.role_required`, and
    :func:`app.tenancy.tenant_role_required` — canonicalising here is what makes
    a user still carrying ``chrisnat_admin`` behave identically to
    ``payrolla_admin`` everywhere, with no per-call-site special cases.
    """
    role = str(role or "").strip().lower()
    return LEGACY_ROLE_ALIASES.get(role, role)


def is_platform_role(role):
    return normalise_role(role) in PLATFORM_ROLES


def is_tenant_role(role):
    return normalise_role(role) in TENANT_ROLES


def is_platform_user(user):
    """A platform (operator) user: authenticated and NOT bound to a tenant."""
    return bool(
        getattr(user, "is_authenticated", False)
        and getattr(user, "client_company_id", None) is None
    )


def is_tenant_user(user):
    """A tenant (client) user: authenticated and bound to one client company."""
    return bool(
        getattr(user, "is_authenticated", False)
        and getattr(user, "client_company_id", None) is not None
    )
