"""Tenant resolution + scoping — the one choke point for multi-tenancy.

Non-negotiable rules (PMVP v1 §4):

  * The active tenant is resolved ONLY from ``current_user.client_company_id``.
    Never from a URL, form field, or query param. A tenant user cannot widen
    their horizon by editing a request.
  * Every tenant-scoped query goes through :func:`tenant_query` so scoping can't
    be forgotten. Platform (Chrisnat) users have ``client_company_id`` NULL and
    intentionally see across all tenants (the oversight/control plane).
  * Child tables with no direct ``client_company_id`` (payroll items, vouchers,
    remittances, payslip deliveries, raw entries/archives) are scoped by joining
    through ``payroll_run`` — the single documented strategy, applied uniformly.

Client-facing routes must use :func:`tenant_query` (or the object guards below)
instead of bare ``Model.query`` — see AUDIT.md (Phase 2).
"""

from functools import wraps

from flask import flash, redirect, url_for
from flask_login import current_user, login_required
from werkzeug.exceptions import NotFound

from app import db
from app.roles import normalise_role
from app.models import (
    Employee,
    EmployeeDeployment,
    Expense,
    ImportBatch,
    PaymentVoucher,
    PayrollItem,
    PayrollRun,
    PayslipDelivery,
    Proposal,
    RawPayEntry,
    RawUploadArchive,
    Remittance,
    User,
    WageRateProfile,
)


class TenantScopeError(Exception):
    """Raised when a model is scoped the wrong way (a programming error, not user input)."""


# Models carrying client_company_id directly.
TENANT_OWNED_MODELS = frozenset(
    {User, Employee, EmployeeDeployment, PayrollRun, Expense, Proposal, ImportBatch, WageRateProfile}
)

# Child models with no client_company_id — scoped by joining through payroll_run.
# (Every one of these has a payroll_run_id FK.)
CHILD_VIA_RUN_MODELS = frozenset(
    {PayrollItem, PaymentVoucher, Remittance, PayslipDelivery, RawPayEntry, RawUploadArchive}
)


def active_tenant_id():
    """The active tenant id, resolved ONLY from the logged-in user.

    Returns the tenant's ``client_company_id`` for a tenant user, or ``None`` for
    a platform (Chrisnat) user or an anonymous request. ``None`` means "not tenant
    scoped" — platform users legitimately span all tenants.
    """
    if not getattr(current_user, "is_authenticated", False):
        return None
    return getattr(current_user, "client_company_id", None)


def is_platform_context():
    """True when the current request is a platform user (sees across tenants)."""
    return (
        getattr(current_user, "is_authenticated", False)
        and getattr(current_user, "client_company_id", None) is None
    )


def tenant_query(model):
    """A query for ``model`` auto-scoped to the active tenant.

    * Platform user (tenant id None) -> unscoped query (oversight across tenants).
    * Tenant user -> filtered to their ``client_company_id`` (directly, or via a
      join through ``payroll_run`` for child tables).
    * A model that is neither tenant-owned nor a known child raises
      :class:`TenantScopeError` — fail loud rather than silently leak.
    """
    query = model.query
    tenant_id = active_tenant_id()
    if tenant_id is None:
        return query
    if model in TENANT_OWNED_MODELS:
        return query.filter(model.client_company_id == tenant_id)
    if model in CHILD_VIA_RUN_MODELS:
        return query.join(PayrollRun, model.payroll_run_id == PayrollRun.id).filter(
            PayrollRun.client_company_id == tenant_id
        )
    raise TenantScopeError(
        f"{model.__name__} is not a recognised tenant-scoped model. Add it to "
        "TENANT_OWNED_MODELS or CHILD_VIA_RUN_MODELS, or scope it explicitly."
    )


def owns_object(obj):
    """True if ``obj`` belongs to the active tenant (always True for platform users).

    Works for tenant-owned models (direct client_company_id) and run-linked child
    rows (checked through their payroll_run). Use in detail/mutation routes so a
    tenant user requesting another tenant's row gets denied, never data.
    """
    tenant_id = active_tenant_id()
    if tenant_id is None:  # platform oversight
        return True
    if obj is None:
        return False
    direct = getattr(obj, "client_company_id", None)
    if direct is not None or type(obj) in TENANT_OWNED_MODELS:
        return direct == tenant_id
    run = getattr(obj, "payroll_run", None)
    if run is not None:
        return getattr(run, "client_company_id", None) == tenant_id
    run_id = getattr(obj, "payroll_run_id", None)
    if run_id is not None:
        return (
            PayrollRun.query.filter_by(id=run_id, client_company_id=tenant_id).first()
            is not None
        )
    raise TenantScopeError(f"Cannot determine tenant ownership for {type(obj).__name__}.")


def landing_endpoint():
    """Where a just-authenticated user should land.

    Tenant (client) users -> their scoped Company Dashboard; platform (Chrisnat)
    users -> the cross-tenant oversight console (the operator dashboard).
    """
    if active_tenant_id() is not None:
        return "main.company_dashboard"
    return "main.dashboard"


def platform_required(view):
    """Restrict a view to platform (Chrisnat) users — the oversight/control plane.

    A tenant (client) user hitting an oversight/operator route (which shows data
    across all tenants) is redirected to their own scoped Company Dashboard, never
    served the cross-tenant page. Anonymous users go through login first.
    Use on every route that intentionally spans tenants (dashboards, client lists,
    search, operator payroll/payslip views) so app-layer isolation holds even
    though those routes only carry @login_required today.
    """

    @wraps(view)
    @login_required
    def wrapped(*args, **kwargs):
        if active_tenant_id() is not None:  # a tenant user
            flash("That area is limited to your company dashboard.", "warning")
            return redirect(url_for("main.company_dashboard"))
        return view(*args, **kwargs)

    return wrapped


def tenant_required(view):
    """Restrict a view to tenant (client) users — the client plane.

    The mirror of :func:`platform_required`: a platform (Chrisnat) user has no
    single company, so they are sent to the oversight console. Anonymous users
    go through login first. Every client-plane route carries this so a platform
    user never lands inside a single tenant's scoped views by accident.
    """

    @wraps(view)
    @login_required
    def wrapped(*args, **kwargs):
        if active_tenant_id() is None:  # a platform user
            return redirect(url_for("main.dashboard"))
        return view(*args, **kwargs)

    return wrapped


def tenant_role_required(*roles):
    """Restrict a client-plane view to specific tenant roles (e.g. client_admin).

    Layered on top of :func:`tenant_required`: a platform (Chrisnat) user is sent
    to the oversight console; a tenant user whose role is not in ``roles`` is
    bounced to their Company Dashboard with a flash. The plane is still decided by
    ``client_company_id`` — the role only narrows permissions *within* the tenant
    plane, so it can never widen a user's data horizon. Used for actions a client
    preparer may not take (e.g. distributing payslips is client_admin-only).
    """
    allowed = {normalise_role(r) for r in roles}

    def decorator(view):
        @wraps(view)
        @login_required
        def wrapped(*args, **kwargs):
            if active_tenant_id() is None:  # platform user
                return redirect(url_for("main.dashboard"))
            if normalise_role(getattr(current_user, "role", None)) not in allowed:
                flash("That action needs a company administrator.", "warning")
                return redirect(url_for("main.company_dashboard"))
            return view(*args, **kwargs)

        return wrapped

    return decorator


def tenant_get_or_404(model, ident):
    """Fetch ``model`` by primary key, scoped to the active tenant, or 404.

    A tenant user asking for another tenant's id gets a 404 (never a 403 that
    confirms the row exists, and never the row itself).
    """
    obj = db.session.get(model, ident)
    if obj is None or not owns_object(obj):
        raise NotFound()
    return obj
