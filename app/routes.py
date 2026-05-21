from calendar import month_name
from datetime import datetime
import os

from flask import Blueprint, current_app, flash, redirect, render_template, request, send_file, url_for
from flask_login import login_required
from sqlalchemy import func, text

from app import db
from app.auth import role_required
from app.excel_utils import export_employees
from app.models import (
    AuditTrail,
    ClientCompany,
    Employee,
    Expense,
    PaymentVoucher,
    PayrollItem,
    PayrollRun,
    Remittance,
    User,
)

main_bp = Blueprint("main", __name__)


@main_bp.route("/")
def index():
    return redirect(url_for("main.dashboard"))


@main_bp.route("/health")
def health():
    return {"status": "ok", "service": "chrisnat-payroll-mvp"}


@main_bp.route("/db-health")
@role_required("admin")
def db_health_json():
    uri = current_app.config.get("SQLALCHEMY_DATABASE_URI", "")
    database_type = current_app.config.get("DATABASE_TYPE_LABEL") or db.engine.name.title()
    connection_status = "ok"
    try:
        db.session.execute(text("SELECT 1"))
    except Exception as exc:
        db.session.rollback()
        connection_status = f"error: {exc.__class__.__name__}"

    return {
        "database_type": database_type,
        "connection_status": connection_status,
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
@login_required
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
    pending_statuses = ("Draft", "Pending Review", "Pending MD Approval")
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

    return render_template(
        "dashboard.html",
        total_employees=Employee.query.count(),
        active_employees=Employee.query.filter_by(status="Active").count(),
        total_clients=ClientCompany.query.count(),
        current_month_total=sum(run.total_net_pay for run in current_runs),
        pending_approvals=pending_approvals,
        paye_total=sum(run.total_paye for run in current_runs),
        ssnit_total=sum(run.total_ssnit for run in current_runs),
        total_expenses=sum(expense.amount for expense in Expense.query.all()),
        recent_runs=PayrollRun.query.filter_by(month=selected_month, year=selected_year)
        .order_by(PayrollRun.created_at.desc())
        .limit(8)
        .all(),
        warning_count=warning_count,
        client_costs=client_costs,
        highest_client=highest_client,
        selected_month=selected_month,
        selected_year=selected_year,
        month_options=valid_months,
        year_options=sorted(known_years, reverse=True),
        action_required_count=pending_approvals + warning_count,
    )


@main_bp.route("/clients")
@login_required
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
@login_required
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
        ssnit_total=sum(run.total_ssnit for run in current_runs),
        pending_approvals=sum(
            1 for run in client.payroll_runs if run.status in ("Draft", "Pending Review", "Pending MD Approval")
        ),
        validation_warnings=sum(run.warning_count for run in client.payroll_runs),
    )


@main_bp.route("/employees")
@login_required
def employees():
    employees = Employee.query.order_by(Employee.full_name).all()
    return render_template("employees.html", employees=employees)


@main_bp.route("/employees/add", methods=["GET", "POST"])
@role_required("admin")
def add_employee():
    return employee_form()


@main_bp.route("/employees/<int:employee_id>/edit", methods=["GET", "POST"])
@role_required("admin")
def edit_employee(employee_id):
    employee = db.get_or_404(Employee, employee_id)
    return employee_form(employee)


def employee_form(employee=None):
    clients = ClientCompany.query.order_by(ClientCompany.name).all()
    if request.method == "POST":
        if employee is None:
            employee = Employee()
            db.session.add(employee)
        employee.staff_id = request.form["staff_id"]
        employee.full_name = request.form["full_name"]
        employee.phone = request.form.get("phone")
        employee.ghana_card_number = request.form.get("ghana_card_number")
        employee.ssnit_number = request.form.get("ssnit_number")
        employee.bank_name = request.form.get("bank_name")
        employee.bank_account_number = request.form.get("bank_account_number")
        employee.momo_number = request.form.get("momo_number")
        employee.employment_type = request.form.get("employment_type")
        employee.service_line = request.form.get("service_line")
        employee.assigned_client = request.form.get("assigned_client")
        employee.client_company_id = request.form.get("client_company_id") or None
        employee.status = request.form.get("status", "Active")
        employee.basic_salary = float(request.form.get("basic_salary") or 0)
        db.session.commit()
        flash("Employee saved.", "success")
        return redirect(url_for("main.employees"))
    return render_template("employee_form.html", employee=employee, clients=clients)


@main_bp.route("/employees/<int:employee_id>")
@login_required
def employee_detail(employee_id):
    employee = db.get_or_404(Employee, employee_id)
    return render_template("employee_detail.html", employee=employee)


@main_bp.route("/employees/export")
@login_required
def export_employee_list():
    file_path = export_employees(
        Employee.query.order_by(Employee.full_name).all(),
        current_app.config["EXPORT_FOLDER"],
    )
    return send_file(file_path, as_attachment=True)
