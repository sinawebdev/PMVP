"""Employee roster module — the single source of truth for distribution contacts.

Each client company has a roster of employees maintained exclusively by Chrisnat reps
inside the system. Contact details (email, phone) used when sending payslips always come
from these records, never from uploaded payroll Excel files.

Access is rep-only (admin / md / payroll_officer / accounts_officer). Client self-service
is deferred to Phase 3. Deactivation is the default for a worker who has left — it keeps
past payroll runs intact. Hard delete exists too but is deliberately narrow (admin/md
only) and refused outright for any employee with payroll history, so payslip records can
never be silently destroyed. The join key is ``staff_id``, normalised on every read and
write so "DCL 9" and "DCL9" resolve to the same record.
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
    make_response,
    redirect,
    render_template,
    request,
    send_file,
    session,
    url_for,
)
from flask_login import login_required

from app import db
from app.audit import record_audit
from app.auth import role_required
from app.htmx_utils import wants_htmx, with_toast
from app.models import ClientCompany, Employee, PayrollItem, WageRateProfile
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


def _roster_context(client_id):
    """Shared data for the roster page and its HTMX partial: the client plus its
    employees (active first, then inactive, each alphabetical) and the counts."""
    client = db.get_or_404(ClientCompany, client_id)
    employees = (
        Employee.query.filter_by(client_company_id=client_id)
        .order_by((Employee.status == ACTIVE).desc(), Employee.full_name)
        .all()
    )
    active_count = sum(1 for e in employees if e.status == ACTIVE)
    return {
        "client": client,
        "employees": employees,
        "active_count": active_count,
        "inactive_count": len(employees) - active_count,
    }


def _roster_body_response(client_id, category, message):
    """Re-render just the roster body partial (table + counts) and attach a toast.
    Used to answer an HTMX add/deactivate/reactivate/delete without a full reload."""
    body = render_template("employees/_roster_body.html", **_roster_context(client_id))
    return with_toast(make_response(body), category, message)


@employees_bp.route("/clients/<int:client_id>/roster")
@role_required(*REP_ROLES)
def roster(client_id):
    """List a client's employees — active first, then inactive, each alphabetical."""
    return render_template("employees/roster.html", **_roster_context(client_id))


# ── Add single employee ───────────────────────────────────────────────────────


def _parse_tax_relief(raw, fallback=0):
    """GRA tax relief as a non-negative monthly amount; blank keeps/zeros it.
    Never hardcode relief category figures here — the amount comes from the
    current GRA circular and is entered per employee."""
    if raw is None or not str(raw).strip():
        return fallback or 0
    try:
        value = float(raw)
    except ValueError:
        return fallback or 0
    return value if value >= 0 else (fallback or 0)


@employees_bp.route("/clients/<int:client_id>/add", methods=["GET", "POST"])
@role_required(*REP_ROLES)
def add(client_id):
    client = db.get_or_404(ClientCompany, client_id)

    if request.method == "POST":
        # Validate first, and on any error re-render the form with the submitted
        # values preserved and per-field messages, instead of redirecting to a
        # blank form (which wiped everything the rep had typed).
        staff_id = normalise_emp_id(request.form.get("staff_id", ""))
        full_name = request.form.get("full_name", "").strip()
        errors = {}
        if not staff_id:
            errors["staff_id"] = "Employee ID is required."
        if not full_name:
            errors["full_name"] = "Full name is required."
        if staff_id and "staff_id" not in errors:
            existing = Employee.query.filter_by(
                client_company_id=client_id, staff_id=staff_id
            ).first()
            if existing:
                errors["staff_id"] = (
                    f"Employee ID {staff_id} already exists for {client.name}."
                )
        if errors:
            return render_template(
                "employees/add.html",
                client=client,
                submitted=request.form,
                errors=errors,
            )

        emp = Employee(
            client_company_id=client_id,
            staff_id=staff_id,
            full_name=full_name,
            email=request.form.get("email", "").strip() or None,
            phone=request.form.get("phone", "").strip() or None,
            department=request.form.get("department", "").strip() or None,
            job_title=request.form.get("job_title", "").strip() or None,
            # Standing identity fields (GRA/SSNIT/MoMo). Safe to capture manually;
            # pay-driving fields (basic salary, pay_type, ICU membership) stay
            # import/seed-only so manual entry can't collide with the raw-engine
            # seed guards. See PMVP_INVESTIGATION notes / B6 decision.
            ssnit_number=request.form.get("ssnit_number", "").strip() or None,
            ghana_card_number=request.form.get("ghana_card_number", "").strip() or None,
            tin=request.form.get("tin", "").strip() or None,
            momo_number=request.form.get("momo_number", "").strip() or None,
            bank_name=request.form.get("bank_name", "").strip() or None,
            bank_branch=request.form.get("bank_branch", "").strip() or None,
            bank_account_number=request.form.get("bank_account", "").strip() or None,
            tax_relief_monthly=_parse_tax_relief(request.form.get("tax_relief")),
            status=ACTIVE,
        )
        db.session.add(emp)
        db.session.commit()
        flash(f"{emp.full_name} added successfully.", "success")
        return redirect(url_for("employees.roster", client_id=client_id))

    return render_template("employees/add.html", client=client, submitted=None, errors={})


# ── Edit employee ─────────────────────────────────────────────────────────────


def _projected_basic(emp, pay_type):
    """The *basic* an employee resolves to under ``pay_type``, evaluated with no
    monthly hours in hand — i.e. a classification change, not a payroll run.

    Returns ``(amount, description)``. ``amount`` is a float, or ``None`` when
    basic is derived monthly from hours × rate (an hourly worker who *does*
    carry a basic rate). The raw engine derives an hourly worker's basic as
    hours × rate and only falls back to the flat basic wage for salaried
    workers, so an hourly worker with no basic rate on file collapses to GH₵0 —
    that is the silent-zeroing this guard exists to surface."""
    flat = float(emp.basic_salary or 0)
    pt = (pay_type or "").strip().lower()
    if pt == "hourly":
        has_basic_rate = (
            WageRateProfile.query.filter_by(
                employee_id=emp.id, category=WageRateProfile.CATEGORY_BASIC
            ).first()
            is not None
        )
        if has_basic_rate:
            return None, "hours × rate each month (a basic hourly rate is on file)"
        return 0.0, (
            f"GH₵0.00 — no hourly basic rate is on file, so the previous flat "
            f"wage of GH₵{flat:,.2f} would no longer apply"
        )
    # Salaried, or an unset/standard-engine worker: the flat basic wage stands.
    return flat, f"flat basic wage of GH₵{flat:,.2f}"


def pay_type_change_guard(emp, current_pay_type, requested_pay_type):
    """Warn-and-confirm payload for a pay_type change: the basic the worker
    resolves to now vs under the requested classification, flagged when the
    change would zero a previously non-zero basic (the George 1800→0 case)."""
    current_amount, current_desc = _projected_basic(emp, current_pay_type or "salaried")
    projected_amount, projected_desc = _projected_basic(emp, requested_pay_type)
    # "Would zero" = the new basic is a hard 0 while the worker currently carries
    # a positive flat basic — the silent-zeroing the guard exists to stop.
    will_zero = projected_amount == 0.0 and float(emp.basic_salary or 0) > 0
    return {
        "current_pay_type": current_pay_type or "not set",
        "requested_pay_type": requested_pay_type,
        "current_basic": current_amount,
        "current_desc": current_desc,
        "projected_basic": projected_amount,
        "projected_desc": projected_desc,
        "will_zero": will_zero,
    }


@employees_bp.route("/clients/<int:client_id>/edit/<int:emp_id>", methods=["GET", "POST"])
@role_required(*REP_ROLES)
def edit(client_id, emp_id):
    client = db.get_or_404(ClientCompany, client_id)
    emp = Employee.query.filter_by(id=emp_id, client_company_id=client_id).first_or_404()

    if request.method == "POST":
        # Guard the raw-engine pay_type change FIRST, before touching the DB. A
        # salaried→hourly flip for a worker with no hourly basic rate silently
        # zeroes their basic on the next compute (George: 1800 → 0). Require an
        # explicit confirm and show the resulting basic before committing
        # anything at all — no field is written until the change is confirmed.
        requested_pay_type = (request.form.get("pay_type") or "").strip().lower()
        current_pay_type = (emp.pay_type or "").strip().lower()
        pay_type_changing = (
            requested_pay_type in ("hourly", "salaried")
            and requested_pay_type != current_pay_type
        )
        if pay_type_changing and request.form.get("confirm_pay_type") != "1":
            return render_template(
                "employees/edit.html",
                client=client,
                emp=emp,
                submitted=request.form,
                pay_type_guard=pay_type_change_guard(
                    emp, current_pay_type, requested_pay_type
                ),
            )

        emp.full_name = request.form.get("full_name", emp.full_name).strip()
        emp.email = request.form.get("email", "").strip() or None
        emp.phone = request.form.get("phone", "").strip() or None
        emp.department = request.form.get("department", "").strip() or None
        emp.bank_name = request.form.get("bank_name", "").strip() or None
        emp.bank_branch = request.form.get("bank_branch", "").strip() or None
        emp.bank_account_number = request.form.get("bank_account", "").strip() or None
        emp.tax_relief_monthly = _parse_tax_relief(
            request.form.get("tax_relief"), emp.tax_relief_monthly
        )
        # status comes through as "Active"/"Inactive"; reject anything else.
        new_status = request.form.get("status", emp.status)
        emp.status = new_status if new_status in (ACTIVE, INACTIVE) else emp.status
        # Raw-engine hourly/salaried classification — correcting a mis-seeded
        # worker here re-derives basic on the next compute (no re-seed needed).
        # Blank leaves it unchanged; only the two valid values are accepted, and
        # a material change is confirmed above before we reach this line.
        if requested_pay_type in ("hourly", "salaried"):
            if pay_type_changing:
                record_audit(
                    "Employee pay_type changed",
                    emp,
                    f"pay_type {current_pay_type or 'not set'} → {requested_pay_type} "
                    f"(confirmed). Basic now: {_projected_basic(emp, requested_pay_type)[1]}.",
                )
            emp.pay_type = requested_pay_type
        # staff_id (the join key) is intentionally immutable after creation.
        db.session.commit()
        flash(f"{emp.full_name} updated.", "success")
        return redirect(url_for("employees.roster", client_id=client_id))

    return render_template(
        "employees/edit.html", client=client, emp=emp, submitted=None, pay_type_guard=None
    )


# ── Deactivate / reactivate (soft, never hard delete) ─────────────────────────


@employees_bp.route("/clients/<int:client_id>/deactivate/<int:emp_id>", methods=["POST"])
@role_required(*REP_ROLES)
def deactivate(client_id, emp_id):
    emp = Employee.query.filter_by(id=emp_id, client_company_id=client_id).first_or_404()
    emp.status = INACTIVE
    db.session.commit()
    message = f"{emp.full_name} deactivated."
    if wants_htmx():
        return _roster_body_response(client_id, "info", message)
    flash(message, "info")
    return redirect(url_for("employees.roster", client_id=client_id))


@employees_bp.route("/clients/<int:client_id>/reactivate/<int:emp_id>", methods=["POST"])
@role_required(*REP_ROLES)
def reactivate(client_id, emp_id):
    emp = Employee.query.filter_by(id=emp_id, client_company_id=client_id).first_or_404()
    emp.status = ACTIVE
    db.session.commit()
    message = f"{emp.full_name} reactivated."
    if wants_htmx():
        return _roster_body_response(client_id, "success", message)
    flash(message, "success")
    return redirect(url_for("employees.roster", client_id=client_id))


# ── Hard delete (admin/md only; refused when payroll history exists) ──────────


def employee_delete_blockers(emp):
    """Why this employee may NOT be hard-deleted, as human-readable reasons.
    Empty list means deletion is allowed. The only blocker is payroll history:
    PayrollItem.employee_id is a plain FK with no cascade, so deleting past it
    would either raise IntegrityError or destroy real payslip records."""
    blockers = []
    item_count = PayrollItem.query.filter_by(employee_id=emp.id).count()
    if item_count:
        blockers.append(
            f"{item_count} payroll record(s) reference this employee "
            "(deactivate instead to keep payroll history intact)"
        )
    return blockers


# Deletion is more sensitive than deactivation — it erases the roster record
# permanently — so it is restricted to admin and MD, unlike the rep-wide
# deactivate/reactivate actions.
@employees_bp.route("/clients/<int:client_id>/delete/<int:emp_id>", methods=["POST"])
@role_required("admin", "md")
def delete(client_id, emp_id):
    emp = Employee.query.filter_by(id=emp_id, client_company_id=client_id).first_or_404()
    blockers = employee_delete_blockers(emp)
    if blockers:
        message = f"Cannot delete {emp.full_name}: {'; '.join(blockers)}."
        if wants_htmx():
            # Row is unchanged — re-render the body so nothing is removed.
            return _roster_body_response(client_id, "danger", message)
        flash(message, "danger")
        return redirect(url_for("employees.roster", client_id=client_id))

    client = emp.client_company
    # Snapshot identity before deletion — after commit the instance is expired
    # and its attributes can't be read back off the deleted row.
    full_name = emp.full_name
    # Audit BEFORE the row is gone — AuditTrail.related_record_id has no enforced
    # FK, so it stays a valid reference after deletion.
    record_audit(
        "Employee hard-deleted",
        emp,
        f"Deleted employee {emp.staff_id} ({full_name}) from "
        f"{client.name if client else 'no client'}. No payroll history existed.",
    )
    # EmployeeDeployment rows cascade via the relationship's delete-orphan.
    db.session.delete(emp)
    db.session.commit()
    message = f"{full_name} permanently deleted."
    if wants_htmx():
        return _roster_body_response(client_id, "success", message)
    flash(message, "success")
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
    "bank_account": ["bank account", "account number", "acct no", "bank acct", "account",
                     "a/c number", "a/c no"],
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
