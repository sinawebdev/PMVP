"""Payroll bureaus on the operator plane: what a bureau's staff may reach.

A bureau user (``User.bureau_id`` set, ``client_company_id`` NULL) works in the
operator interface, but only over the companies whose ``bureau_id`` is theirs.
Payrolla's own staff (both NULL) are untouched by everything here.

Deny by default. ``enforce_operator_scope`` runs before every request from a
bureau user and lets through only:

* endpoints a bureau may use at all (views marked ``@bureau_scoped``, plus the
  few shared endpoints in ``SHARED_ENDPOINTS`` such as logout), and
* only when every object id in the URL, and in the request's form and query
  keys that name one, belongs to one of the bureau's companies.

Anything else is a 404, never a 403 that confirms the object exists. So a new
operator route is closed to bureaus until someone marks it and checks that its
lists are scoped, and a route that is marked still cannot be pointed at another
firm's run or worker by editing a URL.

List pages a bureau may open must also scope their queries; ``operator_query``
and ``scoped_company_ids`` are the tools for that, and the bureau isolation
tests check the pages show nothing of anyone else's.
"""
from functools import wraps

from flask import abort, request
from flask_login import current_user

from app import db
from app.roles import is_bureau_user


# Endpoints every signed-in bureau user needs that carry no client data.
SHARED_ENDPOINTS = frozenset({"static", "auth.logout"})

# Operator endpoints a bureau may use. Each one either takes the company from an
# id the gate checks (OWNERSHIP_RESOLVERS) or scopes its lists through
# operator_query / scoped_company_ids. Everything not listed stays Payrolla's
# alone: access requests, desktop licences, database health, the distribution
# monitor (it reports worker health across every tenant). On the statutory
# pages a bureau reads the defaults and manages only its own versions
# (app.statutory). tests/test_bureau_isolation.py checks these pages show a
# bureau nothing of anyone else's.
BUREAU_ENDPOINTS = frozenset({
    "main.index", "main.dashboard", "main.search",
    "main.clients", "main.client_detail", "main.client_onboarding",
    "main.add_client", "main.edit_client",
    "invites.invite_admin", "invites.resend_invite",
    "employees.roster", "employees.add", "employees.edit", "employees.deactivate",
    "employees.reactivate", "employees.delete", "employees.roster_template",
    "employees.bulk_import", "employees.bulk_import_confirm",
    "payroll.runs", "payroll.new_run", "payroll.preview", "payroll.error_report",
    "payroll.confirm", "payroll.detail", "payroll.calculate", "payroll.edit_items",
    "payroll.submit_for_approval", "payroll.approve", "payroll.reject",
    "payroll.bulk_approve", "payroll.bulk_reject", "payroll.bulk_distribute",
    "payroll.mark_paid", "payroll.delete_run", "payroll.run_reports", "payroll.export",
    "payroll.export_bank_listing_route", "payroll.export_wages_sheet_route",
    "payroll.export_gra_paye_route", "payroll.payslip", "payroll.wage_rates",
    "raw_engine.upload", "raw_engine.confirm", "raw_engine.download_template",
    "raw_engine.run_exports", "raw_engine.download_archive",
    "oversight.risk_queue", "oversight.risk_check", "oversight.release_hold",
    "payslip.index", "payslip.generate",
    "distribution.run_status", "distribution.run_status_fragment",
    "distribution.confirm_send", "distribution.bulk_confirm", "distribution.send",
    "distribution.resend_failed", "distribution.cancel", "distribution.schedule",
    "distribution.reschedule", "distribution.set_preferred_channel",
    "distribution.settle_delivery", "distribution.batch_detail",
    "distribution.history", "distribution.analytics",
    "distribution.history_export_csv", "distribution.history_export_xlsx",
    "audit.audit_trail", "audit.add_expense",
    "notifications.inbox", "notifications.mark_read", "notifications.mark_all_read",
    "statutory.index", "statutory.new", "statutory.copy", "statutory.edit",
})


def current_bureau_id():
    """The signed-in user's bureau id, or None (Payrolla staff, tenants, anonymous)."""
    if not getattr(current_user, "is_authenticated", False):
        return None
    return getattr(current_user, "bureau_id", None)


def is_payrolla_staff(user=None):
    """Payrolla's own operators: platform plane, no bureau, no bureau role."""
    user = user if user is not None else current_user
    return bool(
        getattr(user, "is_authenticated", False)
        and getattr(user, "client_company_id", None) is None
        and not is_bureau_user(user)
    )


def scoped_company_ids():
    """A subquery of the company ids the signed-in user may see, or None for all.

    None means "no restriction" (Payrolla staff). A bureau user gets their
    bureau's companies; a bureau user with no bureau set gets none at all.
    """
    from app.models import ClientCompany

    if not is_bureau_user(current_user):
        return None
    bureau_id = current_bureau_id()
    query = db.session.query(ClientCompany.id)
    if bureau_id is None:
        return query.filter(db.false())
    return query.filter(ClientCompany.bureau_id == bureau_id)


def company_in_scope(company_id):
    """True if the signed-in operator may reach this company."""
    from app.models import ClientCompany

    if not is_bureau_user(current_user):
        return True
    if company_id is None:
        return False
    company = db.session.get(ClientCompany, company_id)
    bureau_id = current_bureau_id()
    return company is not None and bureau_id is not None and company.bureau_id == bureau_id


def operator_query(model):
    """``model.query`` limited to the companies the signed-in operator may see."""
    from app.models import ClientCompany, PayrollRun
    from app.tenancy import CHILD_VIA_RUN_MODELS, TENANT_OWNED_MODELS, TenantScopeError

    query = model.query
    ids = scoped_company_ids()
    if ids is None:
        return query
    if model is ClientCompany:
        return query.filter(ClientCompany.id.in_(ids))
    if model in TENANT_OWNED_MODELS:
        return query.filter(model.client_company_id.in_(ids))
    if model in CHILD_VIA_RUN_MODELS:
        return query.join(PayrollRun, model.payroll_run_id == PayrollRun.id).filter(
            PayrollRun.client_company_id.in_(ids)
        )
    raise TenantScopeError(
        f"{model.__name__} has no company to scope by. Scope it explicitly, or add it to "
        "TENANT_OWNED_MODELS or CHILD_VIA_RUN_MODELS in app/tenancy.py."
    )


def bureau_scoped(view):
    """Mark an operator view as open to bureau users.

    Only mark a view whose lists go through ``operator_query`` (or are filtered
    by ``scoped_company_ids``) and whose object ids are all resolvable by
    ``OWNERSHIP_RESOLVERS``. The URL-id check happens centrally; list scoping is
    the view's own job.
    """
    view._bureau_scoped = True
    return view


# --- Who owns an id -----------------------------------------------------------------
# Each resolver turns an id from a URL or form into the company that owns it, or
# None if the object does not exist. Keys are the names used in route rules and
# forms; an id with no resolver is refused.


def _company_of(model_name, attr="client_company_id"):
    def resolve(value):
        from app import models

        obj = db.session.get(getattr(models, model_name), _as_int(value))
        return getattr(obj, attr, None) if obj is not None else None

    return resolve


def _company_via_run(model_name):
    def resolve(value):
        from app import models

        obj = db.session.get(getattr(models, model_name), _as_int(value))
        if obj is None:
            return None
        run = db.session.get(models.PayrollRun, obj.payroll_run_id)
        return run.client_company_id if run is not None else None

    return resolve


def _same(value):
    return _as_int(value)


# Returned by a resolver that has already decided the id is in scope (an object
# that belongs to the user or the bureau rather than to one company).
IN_SCOPE = object()


def _own_notification(value):
    from app.models import Notification

    note = db.session.get(Notification, _as_int(value))
    return IN_SCOPE if note is not None and note.user_id == getattr(current_user, "id", None) else None


def _staged_import(value):
    """A staged payroll import (ImportBatch) is in scope only if every company it
    would create a run for is: a multi-company workbook names several."""
    import json

    from app.models import ImportBatch

    batch = db.session.get(ImportBatch, _as_int(value))
    if batch is None:
        return None
    try:
        payload = json.loads(batch.payload_json or "{}")
    except ValueError:
        return None
    company_ids = {batch.client_company_id, payload.get("client_company_id")}
    company_ids.update(run.get("client_company_id") for run in payload.get("runs") or [])
    company_ids.discard(None)
    if not company_ids:
        return None
    return IN_SCOPE if all(company_in_scope(cid) for cid in company_ids) else None


def _rate_version(value):
    """A statutory rate version: the defaults are readable by every bureau, a
    bureau's own versions by that bureau only. (Who may EDIT which is decided in
    app.statutory: a bureau never edits a default.)"""
    from app.models import StatutoryRate

    rate = db.session.get(StatutoryRate, _as_int(value))
    if rate is None:
        return None
    if rate.bureau_id is None or rate.bureau_id == current_bureau_id():
        return IN_SCOPE
    return None


def _user_of_company(value):
    """A client login (invite resend): owned through its company."""
    from app.models import User

    user = db.session.get(User, _as_int(value))
    return user.client_company_id if user is not None else None


def _as_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return -1  # never a real primary key


OWNERSHIP_RESOLVERS = {
    "client_id": _same,
    "company_id": _same,
    "client_company_id": _same,
    "run_id": _company_of("PayrollRun"),
    "payroll_run_id": _company_of("PayrollRun"),
    "emp_id": _company_of("Employee"),
    "employee_id": _company_of("Employee"),
    "item_id": _company_via_run("PayrollItem"),
    "voucher_id": _company_via_run("PaymentVoucher"),
    "delivery_id": _company_via_run("PayslipDelivery"),
    "archive_id": _company_via_run("RawUploadArchive"),
    "import_id": _staged_import,
    "expense_id": _company_of("Expense"),
    "batch_id": _company_of("DistributionBatch"),
    "note_id": _own_notification,
    "user_id": _user_of_company,
    "rate_id": _rate_version,
}

# Form or query keys that carry an id, checked on every bureau request whether
# or not the route declares them, so a bulk action can't smuggle one in.
REQUEST_ID_KEYS = (
    "client_id", "client_company_id", "company_id", "run_id", "run_ids",
    "payroll_run_id", "employee_id", "emp_id", "item_id", "payroll_item_ids",
)
# Keys whose value is a list of ids, mapped to the resolver that checks each one.
_LIST_KEYS = {"run_ids": "run_id", "payroll_item_ids": "item_id"}


def _owned(key, value):
    resolver_key = _LIST_KEYS.get(key, key)
    resolver = OWNERSHIP_RESOLVERS.get(resolver_key)
    if resolver is None:
        return False
    owner = resolver(value)
    if owner is IN_SCOPE:
        return True
    return company_in_scope(owner)


def _request_values():
    for source in (request.args, request.form):
        for key in REQUEST_ID_KEYS:
            for value in source.getlist(key):
                if str(value).strip():
                    yield key, value


def enforce_operator_scope():
    """before_request: hold a bureau user to their own companies (see module doc)."""
    if not is_bureau_user(current_user):
        return None
    endpoint = request.endpoint
    if endpoint is None:
        return None  # 404 from routing, nothing to protect
    if endpoint in SHARED_ENDPOINTS:
        return None
    from flask import current_app

    view = current_app.view_functions.get(endpoint)
    if endpoint not in BUREAU_ENDPOINTS and not getattr(view, "_bureau_scoped", False):
        abort(404)
    for key, value in (request.view_args or {}).items():
        if not _owned(key, value):
            abort(404)
    for key, value in _request_values():
        if not _owned(key, value):
            abort(404)
    return None


def register(app):
    app.before_request(enforce_operator_scope)

    @app.context_processor
    def inject_bureau_scope():
        # Templates ask "is this Payrolla's own staff?" through these, never by
        # comparing role strings (the constitution lint forbids role literals).
        from flask_login import current_user as user

        bureau = getattr(user, "bureau", None) if getattr(user, "is_authenticated", False) else None
        return {"is_payrolla_staff": is_payrolla_staff(), "current_bureau": bureau}


def require_bureau_consistency(user):
    """Raise if a login mixes scopes. Called on insert and update of a User."""
    from app.roles import BUREAU_ROLES, normalise_role

    role = normalise_role(user.role)
    if user.bureau_id is not None and user.client_company_id is not None:
        raise ValueError("A login belongs to a company or to a bureau, never both.")
    if role in BUREAU_ROLES and user.bureau_id is None:
        raise ValueError(f"A {role} login needs a bureau.")
    if user.bureau_id is not None and role not in BUREAU_ROLES:
        raise ValueError("A bureau's login must have a bureau role.")


def payrolla_staff_required(view):
    """404 for anyone but Payrolla's own staff (used on top of role_required)."""

    @wraps(view)
    def wrapped(*args, **kwargs):
        if not is_payrolla_staff():
            abort(404)
        return view(*args, **kwargs)

    wrapped._payrolla_only = True
    return wrapped
