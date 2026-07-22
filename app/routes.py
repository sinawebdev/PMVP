from calendar import month_name
from datetime import datetime
import os

from flask import Blueprint, current_app, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from sqlalchemy import or_, text

from app import db
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

from app.payroll_status import PENDING_STATUSES

main_bp = Blueprint("main", __name__)


@main_bp.route("/")
def index():
    # Signed-in users go to their plane's landing (tenant -> Company Dashboard,
    # Chrisnat -> oversight console); everyone else sees the public marketing
    # landing that positions push-distribution vs portal-only competitors.
    if current_user.is_authenticated:
        from app.tenancy import landing_endpoint

        return redirect(url_for(landing_endpoint()))
    return render_template("landing.html")


@main_bp.route("/health")
def health():
    return {
        "status": "ok",
        "service": current_app.config.get("SERVICE_SLUG", "chrisnat-payroll-mvp"),
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
    client_costs = [
        {
            "client": client.name,
            "workers": len(client.employees),
            "payroll_cost": sum(
                run.total_net_pay
                for run in client.payroll_runs
                if run.month == selected_month and run.year == selected_year
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
        for client in ClientCompany.query.order_by(ClientCompany.name).all()
    ]
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

    return render_template(
        "dashboard.html",
        total_employees=Employee.query.count(),
        active_employees=Employee.query.filter_by(status="Active").count(),
        total_clients=ClientCompany.query.count(),
        current_month_total=sum(run.total_net_pay for run in current_runs),
        pending_approvals=pending_approvals,
        paye_total=sum(run.total_paye for run in current_runs),
        # Combined employee (5.5%) + employer (13%) SSF — the figure actually
        # remitted to SSNIT, not just the worker-side deduction.
        ssnit_total=sum(
            run.total_ssnit + run.total_ssnit_employer for run in current_runs
        ),
        total_expenses=sum(expense.amount for expense in Expense.query.all()),
        recent_runs=PayrollRun.query.filter_by(month=selected_month, year=selected_year)
        .order_by(PayrollRun.created_at.desc())
        .limit(8)
        .all(),
        warning_count=warning_count,
        delivery_rate=delivery_rate,
        payslips_delivered=payslips_delivered,
        payslips_total=payslips_total,
        client_costs=client_costs,
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
    (Chrisnat) user has no single company, so they are sent to the oversight
    console instead. This is a Phase 1 shell; the full client interface (payroll
    runs, payslips, employees, etc.) is built out in Phase 3.
    """
    from app.tenancy import active_tenant_id, is_platform_context, tenant_query

    if is_platform_context():
        return redirect(url_for("main.dashboard"))

    company = db.session.get(ClientCompany, active_tenant_id())
    if company is None:  # tenant user whose company vanished — deny softly
        flash("Your company profile is unavailable. Contact Chrisnat.", "warning")
        return redirect(url_for("auth.logout"))

    employee_count = tenant_query(Employee).count()
    active_employee_count = tenant_query(Employee).filter(Employee.status == "Active").count()
    runs = tenant_query(PayrollRun).order_by(PayrollRun.created_at.desc()).all()
    pending_runs = sum(1 for run in runs if run.status in PENDING_STATUSES)

    return render_template(
        "client/dashboard.html",
        company=company,
        employee_count=employee_count,
        active_employee_count=active_employee_count,
        run_count=len(runs),
        pending_runs=pending_runs,
        recent_runs=runs[:8],
    )


@main_bp.route("/clients")
@platform_required
def clients():
    clients = ClientCompany.query.order_by(ClientCompany.name).all()
    return render_template("clients.html", clients=clients)


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
        if client is None:
            client = ClientCompany()
            db.session.add(client)
        client.name = request.form["name"]
        client.contact_person = request.form.get("contact_person")
        client.phone = request.form.get("phone")
        client.email = request.form.get("email")
        client.location = request.form.get("location")
        client.service_type = request.form.get("service_type")
        client.status = request.form.get("status", "Active")
        db.session.commit()
        flash("Client company saved.", "success")
        return redirect(url_for("main.clients"))
    return render_template("client_form.html", client=client)


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
