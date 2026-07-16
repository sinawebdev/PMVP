"""Role vocabulary for PMVP v1's two planes.

Tenancy is determined by ``User.client_company_id`` — NULL means a platform
(Chrisnat) user who operates the oversight/control plane above all tenants; a
non-NULL value means a tenant (client) user hard-scoped to that one company.
Role strings determine *permissions within* a plane; the plane itself is always
decided by ``client_company_id`` (never by the role string alone), so a
misassigned role can never widen a tenant user's data horizon.
"""

# --- Platform (Chrisnat) roles: client_company_id IS NULL --------------------
CHRISNAT_ADMIN = "chrisnat_admin"
CHRISNAT_REVIEWER = "chrisnat_reviewer"

# Legacy internal roles from the single-operator bureau app. Still platform-side
# (client_company_id NULL); kept working so the existing seeded operator logins
# and role_required("admin"/"md"/…) checks don't break during the transition.
LEGACY_PLATFORM_ROLES = frozenset(
    {"admin", "md", "payroll_officer", "accounts_officer", "operations_supervisor", "viewer"}
)
PLATFORM_ROLES = frozenset({CHRISNAT_ADMIN, CHRISNAT_REVIEWER}) | LEGACY_PLATFORM_ROLES

# --- Tenant (client) roles: client_company_id = their company ----------------
CLIENT_ADMIN = "client_admin"        # can approve (maker-checker OFF for v1)
CLIENT_PREPARER = "client_preparer"  # create/edit employees, prepare runs
TENANT_ROLES = frozenset({CLIENT_ADMIN, CLIENT_PREPARER})


def normalise_role(role):
    return str(role or "").strip().lower()


def is_platform_role(role):
    return normalise_role(role) in PLATFORM_ROLES


def is_tenant_role(role):
    return normalise_role(role) in TENANT_ROLES


def is_platform_user(user):
    """A platform (Chrisnat) user: authenticated and NOT bound to a tenant."""
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
