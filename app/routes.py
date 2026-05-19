from datetime import datetime

from flask import Blueprint, current_app, flash, redirect, render_template, request, send_file, url_for
from flask_login import login_required
from sqlalchemy import func

from app import db
from app.auth import role_required
from app.excel_utils import export_employees
from app.models import ClientCompany, Employee, Expense, PayrollItem, PayrollRun

main_bp = Blueprint("main", __name__)


@main_bp.route("/")
def index():
    return redirect(url_for("main.dashboard"))


@main_bp.route("/health")
def health():
    return {"status": "ok", "service": "chrisnat-payroll-mvp"}


@main_bp.route("/dashboard")
@login_required
def dashboard():
    now = datetime.now()
    current_month = now.strftime("%B")

    current_runs = PayrollRun.query.filter_by(month=current_month, year=now.year).all()
    client_costs = [
        {
            "client": client.name,
            "workers": len(client.employees),
            "payroll_cost": sum(
                run.total_net_pay
                for run in client.payroll_runs
                if run.month == current_month and run.year == now.year
            ),
            "pending": sum(
                1
                for run in client.payroll_runs
                if run.status in ("Draft", "Reviewed")
            ),
        }
        for client in ClientCompany.query.order_by(ClientCompany.name).all()
    ]
    highest_client = max(client_costs, key=lambda item: item["payroll_cost"], default=None)

    return render_template(
        "dashboard.html",
        total_employees=Employee.query.count(),
        active_employees=Employee.query.filter_by(status="Active").count(),
        total_clients=ClientCompany.query.count(),
        current_month_total=sum(run.total_net_pay for run in current_runs),
        pending_approvals=PayrollRun.query.filter(PayrollRun.status.in_(["Draft", "Reviewed"])).count(),
        paye_total=sum(run.total_paye for run in current_runs),
        ssnit_total=sum(run.total_ssnit for run in current_runs),
        total_expenses=sum(expense.amount for expense in Expense.query.all()),
        recent_runs=PayrollRun.query.order_by(PayrollRun.created_at.desc()).limit(8).all(),
        warning_count=PayrollItem.query.filter_by(validation_status="Warning").count(),
        client_costs=client_costs,
        highest_client=highest_client,
    )


@main_bp.route("/clients")
@login_required
def clients():
    clients = ClientCompany.query.order_by(ClientCompany.name).all()
    return render_template("clients.html", clients=clients)


@main_bp.route("/clients/add", methods=["GET", "POST"])
@role_required("admin", "payroll_officer")
def add_client():
    return client_form()


@main_bp.route("/clients/<int:client_id>/edit", methods=["GET", "POST"])
@role_required("admin", "payroll_officer")
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
    return render_template("client_detail.html", client=client)


@main_bp.route("/employees")
@login_required
def employees():
    employees = Employee.query.order_by(Employee.full_name).all()
    return render_template("employees.html", employees=employees)


@main_bp.route("/employees/add", methods=["GET", "POST"])
@role_required("admin", "payroll_officer")
def add_employee():
    return employee_form()


@main_bp.route("/employees/<int:employee_id>/edit", methods=["GET", "POST"])
@role_required("admin", "payroll_officer")
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
