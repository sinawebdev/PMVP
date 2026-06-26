"""Employee roster module — the single source of truth for distribution contacts.

Each client company has a roster of employees maintained exclusively by Chrisnat reps
inside the system. Contact details (email, phone) used when sending payslips always come
from these records, never from uploaded payroll Excel files.

Access is rep-only (admin / md / payroll_officer / accounts_officer). Client self-service
is deferred to Phase 3. Employees are never hard-deleted — only deactivated — so past
payroll runs and the audit trail stay intact. The join key is ``staff_id``, normalised on
every read and write so "DCL 9" and "DCL9" resolve to the same record.
"""
import io
import json
import os
import uuid

import pandas as pd
from flask import (
    Blueprint,
    current_app,
    flash,
    redirect,
    render_template,
    request,
    send_file,
    session,
    url_for,
)
from flask_login import login_required

from app import db
from app.auth import role_required
from app.models import ClientCompany, Employee
from app.raw_import import normalise_emp_id

employees_bp = Blueprint("employees", __name__, url_prefix="/employees")

# Chrisnat reps only — md is always allowed by role_required; client_user is blocked.
REP_ROLES = ("admin", "payroll_officer", "accounts_officer")

ACTIVE = "Active"
INACTIVE = "Inactive"


def _stage_path(token):
    folder = current_app.config["IMPORT_SESSION_FOLDER"]
    os.makedirs(folder, exist_ok=True)
    return os.path.join(folder, f"emp_import_{token}.json")


# ── Roster page ──────────────────────────────────────────────────────────────


@employees_bp.route("/clients/<int:client_id>/roster")
@role_required(*REP_ROLES)
def roster(client_id):
    """List a client's employees — active first, then inactive, each alphabetical."""
    client = db.get_or_404(ClientCompany, client_id)
    employees = (
        Employee.query.filter_by(client_company_id=client_id)
        .order_by((Employee.status == ACTIVE).desc(), Employee.full_name)
        .all()
    )
    active_count = sum(1 for e in employees if e.status == ACTIVE)
    inactive_count = len(employees) - active_count
    return render_template(
        "employees/roster.html",
        client=client,
        employees=employees,
        active_count=active_count,
        inactive_count=inactive_count,
    )


# ── Add single employee ───────────────────────────────────────────────────────


@employees_bp.route("/clients/<int:client_id>/add", methods=["GET", "POST"])
@role_required(*REP_ROLES)
def add(client_id):
    client = db.get_or_404(ClientCompany, client_id)

    if request.method == "POST":
        staff_id = normalise_emp_id(request.form.get("staff_id", ""))
        if not staff_id:
            flash("Employee ID is required.", "danger")
            return redirect(url_for("employees.add", client_id=client_id))

        existing = Employee.query.filter_by(
            client_company_id=client_id, staff_id=staff_id
        ).first()
        if existing:
            flash(f"Employee ID {staff_id} already exists for {client.name}.", "danger")
            return redirect(url_for("employees.add", client_id=client_id))

        emp = Employee(
            client_company_id=client_id,
            staff_id=staff_id,
            full_name=request.form.get("full_name", "").strip(),
            email=request.form.get("email", "").strip() or None,
            phone=request.form.get("phone", "").strip() or None,
            department=request.form.get("department", "").strip() or None,
            bank_account_number=request.form.get("bank_account", "").strip() or None,
            status=ACTIVE,
        )
        db.session.add(emp)
        db.session.commit()
        flash(f"{emp.full_name} added successfully.", "success")
        return redirect(url_for("employees.roster", client_id=client_id))

    return render_template("employees/add.html", client=client)


# ── Edit employee ─────────────────────────────────────────────────────────────


@employees_bp.route("/clients/<int:client_id>/edit/<int:emp_id>", methods=["GET", "POST"])
@role_required(*REP_ROLES)
def edit(client_id, emp_id):
    client = db.get_or_404(ClientCompany, client_id)
    emp = Employee.query.filter_by(id=emp_id, client_company_id=client_id).first_or_404()

    if request.method == "POST":
        emp.full_name = request.form.get("full_name", emp.full_name).strip()
        emp.email = request.form.get("email", "").strip() or None
        emp.phone = request.form.get("phone", "").strip() or None
        emp.department = request.form.get("department", "").strip() or None
        emp.bank_account_number = request.form.get("bank_account", "").strip() or None
        # status comes through as "Active"/"Inactive"; reject anything else.
        new_status = request.form.get("status", emp.status)
        emp.status = new_status if new_status in (ACTIVE, INACTIVE) else emp.status
        # staff_id (the join key) is intentionally immutable after creation.
        db.session.commit()
        flash(f"{emp.full_name} updated.", "success")
        return redirect(url_for("employees.roster", client_id=client_id))

    return render_template("employees/edit.html", client=client, emp=emp)


# ── Deactivate / reactivate (soft, never hard delete) ─────────────────────────


@employees_bp.route("/clients/<int:client_id>/deactivate/<int:emp_id>", methods=["POST"])
@role_required(*REP_ROLES)
def deactivate(client_id, emp_id):
    emp = Employee.query.filter_by(id=emp_id, client_company_id=client_id).first_or_404()
    emp.status = INACTIVE
    db.session.commit()
    flash(f"{emp.full_name} deactivated.", "info")
    return redirect(url_for("employees.roster", client_id=client_id))


@employees_bp.route("/clients/<int:client_id>/reactivate/<int:emp_id>", methods=["POST"])
@role_required(*REP_ROLES)
def reactivate(client_id, emp_id):
    emp = Employee.query.filter_by(id=emp_id, client_company_id=client_id).first_or_404()
    emp.status = ACTIVE
    db.session.commit()
    flash(f"{emp.full_name} reactivated.", "success")
    return redirect(url_for("employees.roster", client_id=client_id))


# ── Excel template download ───────────────────────────────────────────────────


@employees_bp.route("/clients/<int:client_id>/roster-template")
@role_required(*REP_ROLES)
def roster_template(client_id):
    """Serve a minimal Excel template so reps know exactly what columns to use."""
    db.get_or_404(ClientCompany, client_id)
    df = pd.DataFrame(
        columns=["Employee ID", "Full Name", "Email", "Phone", "Department", "Bank Account"]
    )
    df.loc[0] = [
        "DCL001",
        "Kwame Mensah",
        "kwame@example.com",
        "+233244000000",
        "Operations",
        "1234567890",
    ]
    buf = io.BytesIO()
    df.to_excel(buf, index=False)
    buf.seek(0)
    return send_file(
        buf,
        download_name="chrisnat_employee_roster_template.xlsx",
        as_attachment=True,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


# ── Bulk import via Excel (upload -> preview -> confirm) ───────────────────────

COLUMN_ALIASES = {
    "staff_id": ["employee id", "emp id", "staff id", "id", "clk no", "staff no",
                 "emp no", "employee_id"],
    "full_name": ["full name", "name", "employee name", "emp name", "staff name",
                  "full_name"],
    "email": ["email", "email address", "e-mail", "mail"],
    "phone": ["phone", "phone number", "mobile", "whatsapp", "contact", "tel"],
    "department": ["department", "dept", "division", "unit"],
    "bank_account": ["bank account", "account number", "acct no", "bank acct", "account"],
}


def _clean(value):
    text = str(value).strip()
    if not text or text.lower() == "nan":
        return None
    return text


@employees_bp.route("/clients/<int:client_id>/bulk-import", methods=["GET", "POST"])
@role_required(*REP_ROLES)
def bulk_import(client_id):
    client = db.get_or_404(ClientCompany, client_id)

    if request.method == "GET":
        return render_template("employees/bulk_import.html", client=client)

    file = request.files.get("file")
    if not file or not file.filename:
        flash("No file uploaded.", "danger")
        return redirect(request.url)

    try:
        df = pd.read_excel(file, dtype=str)
    except Exception as exc:  # noqa: BLE001 - surfaced to the rep
        flash(f"Could not read file: {exc}", "danger")
        return redirect(request.url)

    reverse = {}
    for canonical, aliases in COLUMN_ALIASES.items():
        for alias in aliases:
            reverse[alias.lower().strip()] = canonical

    col_map = {}
    for col in df.columns:
        canonical = reverse.get(str(col).lower().strip())
        if canonical and canonical not in col_map:
            col_map[canonical] = col

    if "staff_id" not in col_map or "full_name" not in col_map:
        flash(
            "Could not find required columns 'Employee ID' and 'Full Name'. "
            "Check the column headers in your file.",
            "danger",
        )
        return redirect(request.url)

    existing_ids = {
        e.staff_id
        for e in Employee.query.filter_by(client_company_id=client_id).all()
    }

    to_create, to_update, errors, seen = [], [], [], set()
    for i, row in df.iterrows():
        raw_id = _clean(row.get(col_map["staff_id"]))
        if not raw_id:
            continue
        norm_id = normalise_emp_id(raw_id)
        full_name = _clean(row.get(col_map["full_name"]))
        if not full_name:
            errors.append({"row": i + 2, "staff_id": norm_id, "issue": "Full Name is blank"})
            continue
        if norm_id in seen:
            errors.append({"row": i + 2, "staff_id": norm_id,
                           "issue": "Duplicate Employee ID within the file"})
            continue
        seen.add(norm_id)

        record = {
            "staff_id": norm_id,
            "full_name": full_name,
            "email": _clean(row.get(col_map["email"])) if "email" in col_map else None,
            "phone": _clean(row.get(col_map["phone"])) if "phone" in col_map else None,
            "department": _clean(row.get(col_map["department"])) if "department" in col_map else None,
            "bank_account": _clean(row.get(col_map["bank_account"])) if "bank_account" in col_map else None,
        }
        (to_update if norm_id in existing_ids else to_create).append(record)

    token = uuid.uuid4().hex
    with open(_stage_path(token), "w", encoding="utf-8") as handle:
        json.dump(
            {"client_id": client_id, "to_create": to_create, "to_update": to_update},
            handle,
        )
    session["emp_import_token"] = token

    return render_template(
        "employees/bulk_import_preview.html",
        client=client,
        to_create=to_create,
        to_update=to_update,
        errors=errors,
        token=token,
    )


@employees_bp.route("/clients/<int:client_id>/bulk-import/confirm", methods=["POST"])
@role_required(*REP_ROLES)
def bulk_import_confirm(client_id):
    client = db.get_or_404(ClientCompany, client_id)
    token = request.form.get("token") or session.get("emp_import_token")
    if not token or session.get("emp_import_token") != token:
        flash("Session mismatch — please re-upload the file.", "danger")
        return redirect(url_for("employees.bulk_import", client_id=client_id))

    path = _stage_path(token)
    if not os.path.exists(path):
        flash("Import session expired — please re-upload the file.", "danger")
        return redirect(url_for("employees.bulk_import", client_id=client_id))
    with open(path, "r", encoding="utf-8") as handle:
        staged = json.load(handle)
    if staged.get("client_id") != client_id:
        flash("Session mismatch — please re-upload the file.", "danger")
        return redirect(url_for("employees.bulk_import", client_id=client_id))

    existing = {
        e.staff_id: e
        for e in Employee.query.filter_by(client_company_id=client_id).all()
    }
    created = updated = 0

    for record in staged.get("to_create", []):
        # Guard against a record created since the preview was generated.
        if record["staff_id"] in existing:
            continue
        emp = Employee(
            client_company_id=client_id,
            staff_id=record["staff_id"],
            full_name=record["full_name"],
            email=record["email"],
            phone=record["phone"],
            department=record["department"],
            bank_account_number=record["bank_account"],
            status=ACTIVE,
        )
        db.session.add(emp)
        created += 1

    for record in staged.get("to_update", []):
        emp = existing.get(record["staff_id"])
        if not emp:
            continue
        emp.full_name = record["full_name"]
        emp.email = record["email"] or emp.email
        emp.phone = record["phone"] or emp.phone
        emp.department = record["department"] or emp.department
        emp.bank_account_number = record["bank_account"] or emp.bank_account_number
        updated += 1

    db.session.commit()

    session.pop("emp_import_token", None)
    try:
        os.remove(path)
    except OSError:
        pass

    flash(f"{created} employees added, {updated} updated.", "success")
    return redirect(url_for("employees.roster", client_id=client_id))
