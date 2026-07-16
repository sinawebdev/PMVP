"""Client (tenant) plane — the self-service interface a client company uses to
see and manage ONLY its own data.

Every route is `@tenant_required` (a platform user is redirected to the oversight
console) and reads/writes through the tenancy choke point — `tenant_query()` for
lists and `tenant_get_or_404()` for objects — so a client user can never touch
another tenant's row. Templates are a standalone client shell (no operator base),
so cross-company / operator-only controls simply do not exist here.

Full self-service (Sina, 2026-07-16): client_admin/client_preparer manage their
own employees. Payroll-run *preparation/upload* reuses the Chrisnat raw-hours
pipeline and is exposed here read-first; the upload entry point is wired in a
follow-up within this phase. Statutory rates are global and view-only for clients.
"""

import os
from datetime import date

from flask import (
    Blueprint,
    current_app,
    flash,
    redirect,
    render_template,
    request,
    send_file,
    url_for,
)
from flask_login import current_user

from app import db
from app.audit import record_audit
from app.models import ClientCompany, Employee, Expense, PayrollItem, PayrollRun, StatutoryRate
from app.pdf_service import generate_payslip_pdf
from app.raw_import import normalise_emp_id
from app.tenancy import active_tenant_id, tenant_get_or_404, tenant_query, tenant_required

client_bp = Blueprint("client", __name__, url_prefix="/company")


def _company():
    """The active tenant's ClientCompany (guaranteed present by tenant_required)."""
    return db.session.get(ClientCompany, active_tenant_id())


def _parse_money(value):
    try:
        return round(float(str(value or "0").replace(",", "").strip() or 0), 2)
    except (TypeError, ValueError):
        return 0.0


# The Dashboard lives at main.company_dashboard (/company) so /company stays the
# canonical tenant landing referenced by login routing and platform_required.

# --- Employees (self-service CRUD) -----------------------------------------
@client_bp.route("/employees")
@tenant_required
def employees():
    rows = tenant_query(Employee).order_by(Employee.full_name).all()
    return render_template("client/employees.html", company=_company(), employees=rows)


@client_bp.route("/employees/add", methods=["GET", "POST"])
@tenant_required
def employee_add():
    company = _company()
    if request.method == "POST":
        error = _save_employee(None, company)
        if error:
            flash(error, "warning")
            return render_template(
                "client/employee_form.html", company=company, employee=None, form=request.form
            )
        return redirect(url_for("client.employees"))
    return render_template("client/employee_form.html", company=company, employee=None, form={})


@client_bp.route("/employees/<int:emp_id>/edit", methods=["GET", "POST"])
@tenant_required
def employee_edit(emp_id):
    employee = tenant_get_or_404(Employee, emp_id)  # 404 if another tenant's employee
    company = _company()
    if request.method == "POST":
        error = _save_employee(employee, company)
        if error:
            flash(error, "warning")
            return render_template(
                "client/employee_form.html", company=company, employee=employee, form=request.form
            )
        return redirect(url_for("client.employees"))
    return render_template(
        "client/employee_form.html", company=company, employee=employee, form={}
    )


@client_bp.route("/employees/<int:emp_id>/deactivate", methods=["POST"])
@tenant_required
def employee_deactivate(emp_id):
    employee = tenant_get_or_404(Employee, emp_id)
    employee.status = "Inactive"
    record_audit("Employee deactivated", employee, f"{employee.full_name} deactivated by client.")
    db.session.commit()
    flash(f"{employee.full_name} deactivated.", "success")
    return redirect(url_for("client.employees"))


@client_bp.route("/employees/<int:emp_id>/reactivate", methods=["POST"])
@tenant_required
def employee_reactivate(emp_id):
    employee = tenant_get_or_404(Employee, emp_id)
    employee.status = "Active"
    record_audit("Employee reactivated", employee, f"{employee.full_name} reactivated by client.")
    db.session.commit()
    flash(f"{employee.full_name} reactivated.", "success")
    return redirect(url_for("client.employees"))


def _save_employee(employee, company):
    """Create/update an employee bound to the ACTIVE tenant. Returns an error
    string or None. client_company_id is forced to the tenant — never taken from
    the form — so a client can only ever create employees under their own company."""
    staff_id = normalise_emp_id(request.form.get("staff_id", ""))
    full_name = request.form.get("full_name", "").strip()
    if not staff_id or not full_name:
        return "Staff ID and full name are required."
    # Uniqueness of staff_id within this tenant.
    dup = (
        tenant_query(Employee)
        .filter(Employee.staff_id == staff_id, Employee.id != (employee.id if employee else -1))
        .first()
    )
    if dup:
        return f"Staff ID {staff_id} already exists for another of your employees."
    if employee is None:
        employee = Employee(client_company_id=company.id, assigned_client=company.name)
        db.session.add(employee)
    employee.staff_id = staff_id
    employee.full_name = full_name
    employee.email = request.form.get("email", "").strip() or None
    employee.phone = request.form.get("phone", "").strip() or None
    employee.momo_number = request.form.get("momo_number", "").strip() or None
    employee.department = request.form.get("department", "").strip() or None
    employee.job_title = request.form.get("job_title", "").strip() or None
    employee.ssnit_number = request.form.get("ssnit_number", "").strip() or None
    employee.ghana_card_number = request.form.get("ghana_card_number", "").strip() or None
    employee.tin = request.form.get("tin", "").strip() or None
    employee.bank_name = request.form.get("bank_name", "").strip() or None
    employee.bank_branch = request.form.get("bank_branch", "").strip() or None
    employee.bank_account_number = request.form.get("bank_account", "").strip() or None
    employee.basic_salary = _parse_money(request.form.get("basic_salary"))
    employee.status = request.form.get("status", "Active") or "Active"
    # Keep the tenant binding invariant even on edit.
    employee.client_company_id = company.id
    record_audit(
        "Employee saved",
        employee,
        f"{employee.full_name} ({employee.staff_id}) saved by client.",
    )
    db.session.commit()
    flash(f"{employee.full_name} saved.", "success")
    return None


# --- Payroll runs (read) ----------------------------------------------------
@client_bp.route("/runs")
@tenant_required
def runs():
    rows = tenant_query(PayrollRun).order_by(PayrollRun.created_at.desc()).all()
    return render_template("client/runs.html", company=_company(), runs=rows)


@client_bp.route("/runs/<int:run_id>")
@tenant_required
def run_detail(run_id):
    run = tenant_get_or_404(PayrollRun, run_id)  # 404 if another tenant's run
    return render_template(
        "client/run_detail.html", company=_company(), run=run, items=run.items
    )


@client_bp.route("/items/<int:item_id>/payslip")
@tenant_required
def payslip(item_id):
    item = tenant_get_or_404(PayrollItem, item_id)  # child scoped via payroll_run
    file_path = generate_payslip_pdf(item, current_app.config["EXPORT_FOLDER"])
    return send_file(
        file_path,
        mimetype="application/pdf",
        as_attachment=True,
        download_name=os.path.basename(file_path),
    )


# --- Statutory (view-only) --------------------------------------------------
@client_bp.route("/statutory")
@tenant_required
def statutory():
    # Global, platform-owned rates; clients are read-only (§4).
    rate = StatutoryRate.active_for(date.today())
    history = StatutoryRate.query.order_by(StatutoryRate.effective_from.desc()).all()
    return render_template(
        "client/statutory.html", company=_company(), rate=rate, history=history
    )


# --- Expenses (read, tenant-scoped) ----------------------------------------
@client_bp.route("/expenses")
@tenant_required
def expenses():
    rows = (
        tenant_query(Expense).order_by(Expense.expense_date.desc()).all()
        if hasattr(Expense, "client_company_id")
        else []
    )
    return render_template("client/expenses.html", company=_company(), expenses=rows)
