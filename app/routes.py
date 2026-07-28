from calendar import month_name
from datetime import datetime
import os
import re

from flask import Blueprint, current_app, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from sqlalchemy import func, or_, text
from sqlalchemy.orm import selectinload

from app import db
from app.audit import record_audit
from app.auth import role_required
from app.tenancy import platform_required
from app.models import (
    AuditTrail,
    ClientCompany,
    DELIVERY_SENT,
    Employee,
    Expense,
    PaymentVoucher,
    PayrollItem,
    PayrollRun,
    PayslipDelivery,
    Remittance,
    User,
)

from app.payroll_status import PENDING_STATUSES, PROCESSED

main_bp = Blueprint("main", __name__)


@main_bp.route("/")
def index():
    # Signed-in users go to their plane's landing (tenant -> Company Dashboard,
    # platform operator -> oversight console); everyone else sees the public
    # marketing landing that positions push-distribution vs portal-only competitors.
    if current_user.is_authenticated:
        from app.tenancy import landing_endpoint

        return redirect(url_for(landing_endpoint()))
    return render_template("landing.html")


@main_bp.route("/health")
def health():
    return {
        "status": "ok",
        "service": current_app.config.get("SERVICE_SLUG", "payrolla"),
    }


@main_bp.route("/db-health")
@role_required("admin")
def db_health_json():
    uri = current_app.config.get("SQLALCHEMY_DATABASE_URI", "")
    database_type = current_app.config.get("DATABASE_TYPE_LABEL") or db.engine.name.title()
    status = "connected"
    try:
        db.session.execute(text("SELECT 1"))
    except Exception as exc:
        db.session.rollback()
        status = f"error: {exc.__class__.__name__}"

    return {
        "database_type": database_type,
        "status": status,
        "uri_prefix": uri.split(":", 1)[0] + "://" if ":" in uri else "unknown",
    }


@main_bp.route("/admin/db-health")
@role_required("admin")
def db_health():
    database_type = current_app.config.get("DATABASE_TYPE_LABEL") or db.engine.name.title()
    sqlite_on_render_warning = (
        db.engine.name == "sqlite" and bool(os.getenv("RENDER"))
    )
    return render_template(
        "db_health.html",
        database_type=database_type,
        database_url_detected=bool(os.getenv("DATABASE_URL")),
        sqlite_on_render_warning=sqlite_on_render_warning,
        counts={
            "users": User.query.count(),
            "clients": ClientCompany.query.count(),
            "employees": Employee.query.count(),
            "payroll_runs": PayrollRun.query.count(),
            "payroll_items": PayrollItem.query.count(),
            "vouchers": PaymentVoucher.query.count(),
            "remittances": Remittance.query.count(),
            "expenses": Expense.query.count(),
            "audit_logs": AuditTrail.query.count(),
        },
    )


@main_bp.route("/dashboard")
@platform_required
def dashboard():
    now = datetime.now()
    valid_months = [month_name[index] for index in range(1, 13)]
    selected_month = request.args.get("month") or now.strftime("%B")
    if selected_month not in valid_months:
        selected_month = now.strftime("%B")
    try:
        selected_year = int(request.args.get("year") or now.year)
    except ValueError:
        selected_year = now.year

    current_runs = PayrollRun.query.filter_by(
        month=selected_month,
        year=selected_year,
    ).all()
    pending_statuses = PENDING_STATUSES
    # Per-client cost history for the sparkline + % change on the Payroll Cost
    # Per Client card. Computed from the client.payroll_runs relationship that is
    # already loaded for the cost/pending figures below — no extra query per
    # client — and truncated at the selected period so looking back at March
    # never charts months after it.
    from app.analytics import (
        MONTH_INDEX,
        client_cost_trend,
        totals_trend,
        up_to_period,
    )

    # Eager-load the two relationships every figure below walks. Without this,
    # the per-client cost rows, the executive analytics, and the Top Clients
    # table each lazy-load employees + runs per company (2N queries); with it the
    # whole dashboard costs three queries no matter how many companies exist.
    all_clients = (
        ClientCompany.query.options(
            selectinload(ClientCompany.employees),
            selectinload(ClientCompany.payroll_runs),
        )
        .order_by(ClientCompany.name)
        .all()
    )
    client_costs = [
        {
            "client": client.name,
            "workers": len(client.employees),
            "payroll_cost": sum(
                run.total_net_pay
                for run in client.payroll_runs
                if run.month == selected_month and run.year == selected_year
            ),
            "trend": client_cost_trend(
                up_to_period(client.payroll_runs, selected_year, selected_month)
            ),
            "pending": sum(
                1
                for run in client.payroll_runs
                if run.month == selected_month
                and run.year == selected_year
                and run.status in pending_statuses
            ),
            "runs": [
                run
                for run in client.payroll_runs
                if run.month == selected_month and run.year == selected_year
            ],
        }
        for client in all_clients
    ]
    # Book-wide trend for the card header, summed from the same already-loaded
    # relationship the per-client rows use (no additional query).
    portfolio_trend = totals_trend(
        up_to_period(
            [run for client in all_clients for run in client.payroll_runs],
            selected_year,
            selected_month,
        )
    )
    max_cost = max((item["payroll_cost"] for item in client_costs), default=0)
    for item in client_costs:
        item["bar_percent"] = round((item["payroll_cost"] / max_cost) * 100, 1) if max_cost else 0
        statuses = {run.status for run in item["runs"]}
        if not item["runs"]:
            item["submission_status"] = "No run submitted"
            item["submission_class"] = "text-bg-light"
        elif item["pending"]:
            item["submission_status"] = "Needs approval"
            item["submission_class"] = "text-bg-warning"
        elif "Exported" in statuses:
            item["submission_status"] = "Exported"
            item["submission_class"] = "text-bg-success"
        elif "Approved" in statuses:
            item["submission_status"] = (
                "Approved: GH\u20b5 0.00" if item["payroll_cost"] == 0 else "Approved"
            )
            item["submission_class"] = "text-bg-success"
        else:
            item["submission_status"] = "Submitted"
            item["submission_class"] = "text-bg-secondary"

    highest_client = max(client_costs, key=lambda item: item["payroll_cost"], default=None)
    if highest_client and highest_client["payroll_cost"] <= 0:
        highest_client = None
    known_years = {
        row[0]
        for row in db.session.query(PayrollRun.year).distinct().all()
        if row[0]
    }
    known_years.update({now.year - 1, now.year, now.year + 1, selected_year})
    pending_approvals = PayrollRun.query.filter(PayrollRun.status.in_(pending_statuses)).count()
    warning_count = PayrollItem.query.filter_by(validation_status="Warning").count()

    # Payslip delivery rate for the selected period: distinct workers whose payslip was
    # actually pushed (SMS/WhatsApp/email) over the total payslips in those runs. This is
    # our differentiator — competitors stop at "payslip available in a portal".
    period_run_ids = [run.id for run in current_runs]
    payslips_total = sum(len(run.items) for run in current_runs)
    payslips_delivered = (
        db.session.query(PayslipDelivery.payroll_item_id)
        .filter(
            PayslipDelivery.payroll_run_id.in_(period_run_ids),
            PayslipDelivery.status == DELIVERY_SENT,
        )
        .distinct()
        .count()
        if period_run_ids
        else 0
    )
    delivery_rate = round(payslips_delivered / payslips_total * 100) if payslips_total else 0

    # Held payrolls (risk gate) and recently completed runs, plus the
    # 'distributed' signal for the stepper — all reusing existing state.
    #
    # The counter, the Action Required row, the Held panel, and the risk queue all
    # come from this one call (app.risk.risk_summary) so they cannot disagree, and
    # a release/approval is reflected on the next render with no cache to clear.
    from app.risk import risk_summary

    risk = risk_summary(limit=8)
    held_count = risk["held_count"]
    held_runs = risk["held_runs"]
    recent_runs = (
        PayrollRun.query.filter_by(month=selected_month, year=selected_year)
        .order_by(PayrollRun.created_at.desc())
        .limit(8)
        .all()
    )
    recently_completed = (
        PayrollRun.query.filter_by(status=PROCESSED)
        .order_by(PayrollRun.created_at.desc())
        .limit(6)
        .all()
    )
    from app.payroll import distributed_run_ids

    dashboard_distributed_ids = distributed_run_ids(
        [run.id for run in recent_runs]
        + [run.id for run in held_runs]
        + [run.id for run in recently_completed]
    )

    # Executive analytics (revenue trend / cost ranking / status mix / client
    # growth / quick stats / top clients). Computed entirely from `all_clients`,
    # which is already loaded with its employees and runs above — the whole
    # section costs no additional queries, matching the tenant dashboard's
    # "aggregate what you already have" approach.
    from app.analytics import platform_dashboard_analytics
    from app.events import platform_activity

    active_employees = Employee.query.filter_by(status="Active").count()
    # Summed in the database rather than by loading every Expense row — this
    # figure is a single number and the table grows with every client's monthly
    # spend.
    total_expenses = (
        db.session.query(func.coalesce(func.sum(Expense.amount), 0.0)).scalar() or 0.0
    )
    analytics = platform_dashboard_analytics(
        all_clients,
        expense_total=total_expenses,
        active_employees=active_employees,
        cutoff=(selected_year, MONTH_INDEX.get(selected_month, 0)),
    )

    return render_template(
        "dashboard.html",
        total_employees=Employee.query.count(),
        active_employees=active_employees,
        total_clients=ClientCompany.query.count(),
        current_month_total=sum(run.total_net_pay for run in current_runs),
        pending_approvals=pending_approvals,
        paye_total=sum(run.total_paye for run in current_runs),
        # Combined employee (5.5%) + employer (13%) SSF — the figure actually
        # remitted to SSNIT, not just the worker-side deduction.
        ssnit_total=sum(
            run.total_ssnit + run.total_ssnit_employer for run in current_runs
        ),
        total_expenses=total_expenses,
        analytics=analytics,
        activity=platform_activity(limit=10),
        recent_runs=recent_runs,
        held_runs=held_runs,
        held_count=held_count,
        recently_completed=recently_completed,
        distributed_ids=dashboard_distributed_ids,
        warning_count=warning_count,
        delivery_rate=delivery_rate,
        payslips_delivered=payslips_delivered,
        payslips_total=payslips_total,
        client_costs=client_costs,
        portfolio_trend=portfolio_trend,
        highest_client=highest_client,
        selected_month=selected_month,
        selected_year=selected_year,
        month_options=valid_months,
        year_options=sorted(known_years, reverse=True),
        action_required_count=pending_approvals + warning_count,
    )


@main_bp.route("/company")
@login_required
def company_dashboard():
    """Tenant plane landing — a client user's own company at a glance.

    Hard-scoped to ``current_user.client_company_id`` via tenant_query. A platform
    (operator) user has no single company, so they are sent to the oversight
    console instead. The full client interface (payroll runs, payslips,
    employees, etc.) hangs off this landing.
    """
    from app.tenancy import active_tenant_id, is_platform_context, tenant_query

    if is_platform_context():
        return redirect(url_for("main.dashboard"))

    company = db.session.get(ClientCompany, active_tenant_id())
    if company is None:  # tenant user whose company vanished — deny softly
        flash(f"Your company profile is unavailable. Contact {current_app.config['APP_BRAND_NAME']}.", "warning")
        return redirect(url_for("auth.logout"))

    employee_count = tenant_query(Employee).count()
    active_employee_count = tenant_query(Employee).filter(Employee.status == "Active").count()
    runs = tenant_query(PayrollRun).order_by(PayrollRun.created_at.desc()).all()
    pending_runs = sum(1 for run in runs if run.status in PENDING_STATUSES)
    # Risk holds on this company's own runs, from the same centralized rule the
    # operator dashboard uses — so a release clears here on the next load too.
    from app.risk import is_held

    held_runs_count = sum(1 for run in runs if is_held(run))

    from app.models import ImportBatch

    draft_count = (
        tenant_query(ImportBatch)
        .filter(ImportBatch.payroll_run_id.is_(None), ImportBatch.status == "Draft")
        .count()
    )

    # Executive analytics (trend / cost by month / cost mix / quick stats).
    # Computed from the runs + expenses already scoped to this tenant, so the
    # charts can never show another company's figures.
    from app.analytics import client_dashboard_analytics

    expense_total = sum(
        expense.amount or 0 for expense in tenant_query(Expense).all()
    )
    analytics = client_dashboard_analytics(
        runs, employee_count=employee_count, expense_total=expense_total
    )

    return render_template(
        "client/dashboard.html",
        company=company,
        employee_count=employee_count,
        active_employee_count=active_employee_count,
        run_count=len(runs),
        pending_runs=pending_runs,
        held_runs_count=held_runs_count,
        draft_count=draft_count,
        recent_runs=runs[:8],
        analytics=analytics,
    )


@main_bp.route("/clients")
@platform_required
def clients():
    clients = ClientCompany.query.order_by(ClientCompany.name).all()
    return render_template("clients.html", clients=clients)


# --- Client onboarding ------------------------------------------------------
# Onboarding a company is deliberately independent of authentication: this
# workflow only ever creates the ClientCompany record. Supabase Auth users are
# provisioned by hand afterwards (see the onboarding summary page), so a company
# can exist, be edited, and be deleted without any credential ever being minted
# by the app — and a credential outage never blocks onboarding.

COMPANY_STATUSES = ("Active", "Inactive")

# A company code is an identifier an operator types and quotes, so it is kept to
# an unambiguous shape: A-Z, 0-9 and dashes, 2-20 characters, starting on a
# letter or digit.
COMPANY_CODE_PATTERN = re.compile(r"^[A-Z0-9][A-Z0-9-]{1,19}$")


def normalise_company_code(value):
    """Canonical form of a typed company code: uppercase, separators collapsed
    to single dashes, no leading/trailing dash ("msc  ltd" -> "MSC-LTD")."""
    code = re.sub(r"[\s_]+", "-", str(value or "").strip().upper())
    return re.sub(r"-{2,}", "-", code).strip("-")


def validate_client_form(form, client=None):
    """(values, errors) for a submitted client company form.

    ``values`` is the normalised submission, echoed back verbatim when the form
    is re-rendered so the operator never retypes a long address. ``errors`` maps
    field name -> message; an empty dict means the submission is savable.
    Uniqueness is checked against the database excluding ``client`` itself, so
    editing a company and saving its own name/code back is not a conflict.
    """
    values = {
        "name": (form.get("name") or "").strip(),
        "company_code": normalise_company_code(form.get("company_code")),
        "contact_person": (form.get("contact_person") or "").strip(),
        "email": (form.get("email") or "").strip(),
        "phone": (form.get("phone") or "").strip(),
        "address": (form.get("address") or "").strip(),
        "location": (form.get("location") or "").strip(),
        "service_type": (form.get("service_type") or "").strip(),
        "status": (form.get("status") or "Active").strip(),
        "notes": (form.get("notes") or "").strip(),
    }
    errors = {}
    other_id = client.id if client else -1

    if not values["name"]:
        errors["name"] = "Company name is required."
    elif (
        ClientCompany.query.filter(
            ClientCompany.name == values["name"], ClientCompany.id != other_id
        ).first()
        is not None
    ):
        errors["name"] = f"Another company is already registered as {values['name']}."

    if not values["company_code"]:
        errors["company_code"] = "Company code is required."
    elif not COMPANY_CODE_PATTERN.match(values["company_code"]):
        errors["company_code"] = (
            "Use 2-20 letters, digits or dashes (e.g. MSC or ACME-GH)."
        )
    elif (
        ClientCompany.query.filter(
            ClientCompany.company_code == values["company_code"],
            ClientCompany.id != other_id,
        ).first()
        is not None
    ):
        errors["company_code"] = f"Company code {values['company_code']} is already in use."

    # Email is optional, but a typo'd address is worse than a blank one — this is
    # where the client's own onboarding correspondence goes.
    if values["email"] and not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", values["email"]):
        errors["email"] = "Enter a valid email address."

    if values["status"] not in COMPANY_STATUSES:
        errors["status"] = "Choose a valid status."

    return values, errors


@main_bp.route("/clients/add", methods=["GET", "POST"])
@role_required("admin")
def add_client():
    return client_form()


@main_bp.route("/clients/<int:client_id>/edit", methods=["GET", "POST"])
@role_required("admin")
def edit_client(client_id):
    client = db.get_or_404(ClientCompany, client_id)
    return client_form(client)


def client_form(client=None):
    if request.method == "POST":
        values, errors = validate_client_form(request.form, client)
        if errors:
            # Re-render at 200 with the submission intact and the messages bound
            # to their fields (the login/employee-form pattern), rather than
            # bouncing to an empty form.
            return render_template(
                "client_form.html", client=client, values=values, errors=errors
            )
        creating = client is None
        if creating:
            client = ClientCompany()
            db.session.add(client)
        client.name = values["name"]
        client.company_code = values["company_code"]
        client.contact_person = values["contact_person"] or None
        client.phone = values["phone"] or None
        client.email = values["email"] or None
        client.address = values["address"] or None
        client.location = values["location"] or None
        client.service_type = values["service_type"] or None
        client.status = values["status"]
        client.notes = values["notes"] or None
        db.session.flush()  # assign client.id before the audit entry references it
        record_audit(
            "Client company onboarded" if creating else "Client company updated",
            client,
            f"{client.name} ({client.company_code}) "
            f"{'onboarded' if creating else 'updated'} by {current_user.name}.",
        )
        db.session.commit()
        if creating:
            # A new company lands on its onboarding summary, which is where the
            # manual Supabase credential step is spelled out.
            return redirect(url_for("main.client_onboarding", client_id=client.id))
        flash(f"{client.name} updated.", "success")
        return redirect(url_for("main.clients"))

    values = {
        "name": client.name if client else "",
        "company_code": client.company_code if client else "",
        "contact_person": client.contact_person if client else "",
        "email": client.email if client else "",
        "phone": client.phone if client else "",
        "address": client.address if client else "",
        "location": client.location if client else "",
        "service_type": client.service_type if client else "",
        "status": client.status if client else "Active",
        "notes": client.notes if client else "",
    }
    return render_template("client_form.html", client=client, values=values, errors={})


@main_bp.route("/clients/<int:client_id>/onboarding")
@platform_required
def client_onboarding(client_id):
    """The post-creation onboarding summary.

    Confirms what was saved and states the one manual step left: an
    administrator provisions the company's login credentials in Supabase
    Authentication. Deliberately NOT automated — company records and auth
    identities stay independent (see the module note above), so this page is
    informational and creates nothing.
    """
    client = db.get_or_404(ClientCompany, client_id)
    return render_template("client_onboarding.html", client=client)


@main_bp.route("/clients/<int:client_id>")
@platform_required
def client_detail(client_id):
    client = db.get_or_404(ClientCompany, client_id)
    now = datetime.now()
    current_month = now.strftime("%B")
    current_year = now.year
    previous_month_index = 12 if now.month == 1 else now.month - 1
    previous_year = now.year - 1 if now.month == 1 else now.year
    previous_month = month_name[previous_month_index]
    current_runs = [
        run
        for run in client.payroll_runs
        if run.month == current_month and run.year == current_year
    ]
    previous_runs = [
        run
        for run in client.payroll_runs
        if run.month == previous_month and run.year == previous_year
    ]
    return render_template(
        "client_detail.html",
        client=client,
        current_month=current_month,
        current_year=current_year,
        previous_month=previous_month,
        previous_year=previous_year,
        current_month_payroll=sum(run.total_net_pay for run in current_runs),
        previous_month_payroll=sum(run.total_net_pay for run in previous_runs),
        payroll_status=", ".join({run.status for run in current_runs}) if current_runs else "No run submitted",
        paye_total=sum(run.total_paye for run in current_runs),
        # Combined employee (5.5%) + employer (13%) SSF — the remittable figure.
        ssnit_total=sum(
            run.total_ssnit + run.total_ssnit_employer for run in current_runs
        ),
        pending_approvals=sum(
            1 for run in client.payroll_runs if run.status in PENDING_STATUSES
        ),
        validation_warnings=sum(run.warning_count for run in client.payroll_runs),
    )


@main_bp.route("/search")
@platform_required
def search():
    q = request.args.get("q", "").strip()
    clients = []
    items = []
    if q:
        like = f"%{q}%"
        clients = (
            ClientCompany.query.filter(ClientCompany.name.ilike(like))
            .order_by(ClientCompany.name)
            .limit(25)
            .all()
        )
        items = (
            PayrollItem.query.filter(
                or_(
                    PayrollItem.full_name.ilike(like),
                    PayrollItem.staff_id.ilike(like),
                )
            )
            .order_by(PayrollItem.id.desc())
            .limit(50)
            .all()
        )
    return render_template("search_results.html", q=q, clients=clients, items=items)
