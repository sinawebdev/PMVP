"""Employee roster module — the single source of truth for distribution contacts.

Each client company has a roster of employees maintained exclusively by operator reps
inside the system. Contact details (email, phone) used when sending payslips always come
from these records, never from uploaded payroll Excel files.

Access is rep-only (admin / md / payroll_officer / accounts_officer). Client self-service
goes through the client portal (app/client), and both portals bulk-import rosters
through app/roster_import.py. Deactivation is the default for a worker who has left — it keeps
past payroll runs intact. Hard delete exists too but is deliberately narrow (admin/md
only) and refused outright for any employee with payroll history, so payslip records can
never be silently destroyed. The join key is ``staff_id``, normalised on every read and
write so "DCL 9" and "DCL9" resolve to the same record.
"""
import os
from datetime import date
from math import isfinite

from flask import (
    Blueprint,
    flash,
    make_response,
    redirect,
    render_template,
    request,
    send_file,
    session,
    url_for,
)

from app import db, roster_import
from app.audit import record_audit
from app.auth import role_required
from app.htmx_utils import wants_htmx, with_toast
from app.paging import paginate
from app.models import ClientCompany, Employee, EmployeeTaxReliefApproval, PayrollItem, WageRateProfile
from app.raw_import import normalise_emp_id
from app.payroll import save_temporary_upload
from app.spreadsheet_uploads import (
    LEGACY_WORKBOOK_EXTENSIONS,
    ZIP_WORKBOOK_EXTENSIONS,
    SpreadsheetValidationError,
    validate_spreadsheet_upload,
)

employees_bp = Blueprint("employees", __name__, url_prefix="/employees")

# Operator reps only — md is always allowed by role_required; client_user is blocked.
from app.permissions import (  # canonical operator capability groups
    REP_ROLES,
    ROSTER_DELETE_ROLES,
)

ACTIVE = "Active"
INACTIVE = "Inactive"


# ── Roster page ──────────────────────────────────────────────────────────────


def _roster_context(client_id):
    """Shared data for the roster page and its HTMX partial: the client plus its
    employees (active first, then inactive, each alphabetical) and the counts."""
    client = db.get_or_404(ClientCompany, client_id)
    # Paged: a roster is the one collection that grows without limit for a
    # single customer, and this partial is re-rendered on every add/deactivate.
    page = paginate(
        Employee.query.filter_by(client_company_id=client_id).order_by(
            (Employee.status == ACTIVE).desc(), Employee.full_name
        )
    )
    employees = page.items
    # Counts describe the whole roster, not the page — they are the reason to
    # look at the page, so they must not shrink when you turn it.
    active_count = Employee.query.filter_by(
        client_company_id=client_id, status=ACTIVE
    ).count()
    return {
        "client": client,
        "employees": employees,
        "page": page,
        "active_count": active_count,
        "inactive_count": Employee.query.filter_by(client_company_id=client_id)
        .filter(Employee.status != ACTIVE)
        .count(),
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


def parse_deduction_metadata(form, employee=None):
    """Validate evidence supplied by the employer; an amount never grants approval."""
    errors = {}
    values = {}

    def raw(name, default=None):
        return form.get(name) if name in form else getattr(employee, name, default)

    junior = raw("overtime_is_junior")
    if junior in (None, "", "unknown"):
        values["overtime_is_junior"] = None
    elif junior in (True, "true", "yes", "1"):
        values["overtime_is_junior"] = True
    elif junior in (False, "false", "no", "0"):
        values["overtime_is_junior"] = False
    else:
        errors["overtime_is_junior"] = "Choose unknown, junior staff, or not junior staff."

    def amount(name, value, default=0):
        if value in (None, ""):
            return default
        try:
            number = float(value)
            if not isfinite(number) or number < 0:
                raise ValueError
            return number
        except (TypeError, ValueError):
            errors[name] = "Enter a finite amount of zero or more."
            return default

    values["overtime_qualifying_annual_income"] = amount(
        "overtime_qualifying_annual_income", raw("overtime_qualifying_annual_income"), None
    )
    year = raw("overtime_income_year")
    try:
        values["overtime_income_year"] = int(year) if year not in (None, "") else None
        if values["overtime_income_year"] is not None and not 1900 <= values["overtime_income_year"] <= 9999:
            raise ValueError
    except (TypeError, ValueError):
        errors["overtime_income_year"] = "Enter a valid four-digit assessment year."
    if (values.get("overtime_income_year") is None) != (values["overtime_qualifying_annual_income"] is None):
        errors["overtime_income_year"] = "Supply both qualifying annual income and its assessment year."

    approved = raw("pf_scheme_approved", False)
    if approved not in (True, False, None, "", "true", "false", "yes", "no", "1", "0"):
        errors["pf_scheme_approved"] = "Choose whether the provident fund scheme is approved."
    values["pf_scheme_approved"] = approved in (True, "true", "yes", "1")
    values["pf_scheme_reference"] = str(raw("pf_scheme_reference") or "").strip() or None
    if values["pf_scheme_reference"] and len(values["pf_scheme_reference"]) > 160:
        errors["pf_scheme_reference"] = "Keep the scheme reference within 160 characters."
    if values["pf_scheme_approved"] and not values["pf_scheme_reference"]:
        errors["pf_scheme_reference"] = "Enter the approved scheme's reference."

    # Previous code accepted an unverified number alone:
    # employee.tax_relief_monthly = _parse_tax_relief(form.get("tax_relief"))
    relief = form.get("tax_relief") if "tax_relief" in form else raw("tax_relief_monthly", 0)
    values["tax_relief_monthly"] = amount("tax_relief", relief)
    for name in ("tax_relief_approval_ref", "tax_relief_category"):
        values[name] = str(raw(name) or "").strip() or None
        limit = 160 if name == "tax_relief_approval_ref" else 80
        if values[name] and len(values[name]) > limit:
            errors[name] = f"Keep this value within {limit} characters."
    for name in ("tax_relief_valid_from", "tax_relief_valid_to"):
        value = raw(name)
        try:
            values[name] = value if isinstance(value, date) else (date.fromisoformat(str(value)) if value else None)
        except (TypeError, ValueError):
            values[name] = None
            errors[name] = "Enter a valid date."
    if values["tax_relief_valid_from"] and values["tax_relief_valid_to"] and values["tax_relief_valid_to"] < values["tax_relief_valid_from"]:
        errors["tax_relief_valid_to"] = "The end date must be on or after the start date."
    relief_evidence_fields = (
        "tax_relief_approval_ref", "tax_relief_category",
        "tax_relief_valid_from", "tax_relief_valid_to",
    )
    # A migrated amount alone was never an approved award. Clearing that
    # request must not require inventing a GRA withdrawal document.
    existing_award = bool(employee and (
        employee.tax_relief_approvals
        or all(getattr(employee, name, None) for name in relief_evidence_fields)
    ))
    revoking = bool(existing_award and employee.tax_relief_monthly and "tax_relief" in form and values["tax_relief_monthly"] == 0)
    if values["tax_relief_monthly"] > 0 or revoking:
        for name in relief_evidence_fields:
            if not values[name]:
                errors[name] = "A relief approval or withdrawal needs its GRA reference, category and validity dates."
    return values, errors


def save_deduction_metadata(employee, values):
    """Keep each changed relief approval so past payroll periods remain reproducible."""
    for name, value in values.items():
        setattr(employee, name, value)
    approval = dict(
        monthly_amount=values["tax_relief_monthly"],
        reference=values["tax_relief_approval_ref"],
        category=values["tax_relief_category"],
        valid_from=values["tax_relief_valid_from"],
        valid_to=values["tax_relief_valid_to"],
    )
    if approval["reference"] and approval["category"] and approval["valid_from"] and approval["valid_to"]:
        latest = employee.tax_relief_approvals[-1] if employee.tax_relief_approvals else None
        if latest is None or any(getattr(latest, name) != value for name, value in approval.items()):
            employee.tax_relief_approvals.append(EmployeeTaxReliefApproval(**approval))


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
        deduction_values, deduction_errors = parse_deduction_metadata(request.form)
        errors.update(deduction_errors)
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
            # seed guards. See the B6 decision in the early investigation notes.
            ssnit_number=request.form.get("ssnit_number", "").strip() or None,
            ghana_card_number=request.form.get("ghana_card_number", "").strip() or None,
            tin=request.form.get("tin", "").strip() or None,
            momo_number=request.form.get("momo_number", "").strip() or None,
            bank_name=request.form.get("bank_name", "").strip() or None,
            bank_branch=request.form.get("bank_branch", "").strip() or None,
            bank_account_number=request.form.get("bank_account", "").strip() or None,
            # Previous amount-only assignment is retained in parse_deduction_metadata.
            status=ACTIVE,
        )
        db.session.add(emp)
        save_deduction_metadata(emp, deduction_values)
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
        deduction_values, deduction_errors = parse_deduction_metadata(request.form, emp)
        if deduction_errors:
            for error in dict.fromkeys(deduction_errors.values()):
                flash(error, "warning")
            return render_template(
                "employees/edit.html", client=client, emp=emp,
                submitted=request.form, pay_type_guard=None,
            )
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
        # Previous: emp.tax_relief_monthly = _parse_tax_relief(request.form.get("tax_relief"), emp.tax_relief_monthly)
        save_deduction_metadata(emp, deduction_values)
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
@role_required(*ROSTER_DELETE_ROLES)
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


# ── Bulk import via Excel (upload -> preview -> confirm) ───────────────────────
# Thin wrappers over app.roster_import, which the client portal shares. The
# client comes from the URL here because operators work across companies; the
# staged file is checked against it again on confirm.

_SESSION_KEY = "emp_import_token"


@employees_bp.route("/clients/<int:client_id>/roster-template")
@role_required(*REP_ROLES)
def roster_template(client_id):
    """The Excel template, so reps know exactly which columns are read."""
    db.get_or_404(ClientCompany, client_id)
    return send_file(
        roster_import.build_template(),
        download_name=roster_import.TEMPLATE_FILENAME,
        as_attachment=True,
        mimetype=roster_import.TEMPLATE_MIMETYPE,
    )


@employees_bp.route("/clients/<int:client_id>/bulk-import", methods=["GET", "POST"])
@role_required(*REP_ROLES)
def bulk_import(client_id):
    client = db.get_or_404(ClientCompany, client_id)

    if request.method == "GET":
        return render_template(
            "employees/bulk_import.html", client=client, columns=roster_import.ROSTER_COLUMNS
        )

    file = request.files.get("file")
    try:
        safe_name, _ext, _size = validate_spreadsheet_upload(
            file, allowed=ZIP_WORKBOOK_EXTENSIONS | LEGACY_WORKBOOK_EXTENSIONS
        )
        staged_path = save_temporary_upload(file)
    except SpreadsheetValidationError as exc:
        flash(str(exc), "danger")
        return redirect(request.url)
    try:
        existing = roster_import.existing_by_staff_id(
            Employee.query.filter_by(client_company_id=client_id)
        )
        parsed = roster_import.parse_roster(staged_path, existing)
    except roster_import.RosterImportError as exc:
        flash(str(exc), "danger")
        return redirect(request.url)
    finally:
        try:
            os.unlink(staged_path)
        except OSError:
            pass

    previous = session.pop(_SESSION_KEY, None)
    if previous:
        roster_import.discard_staged(previous)
    token = roster_import.stage_roster(client_id, parsed, safe_name)
    session[_SESSION_KEY] = token

    return render_template(
        "employees/bulk_import_preview.html",
        client=client,
        to_create=parsed["to_create"],
        to_update=parsed["to_update"],
        errors=parsed["errors"],
        warnings=parsed["warnings"],
        unchanged=parsed["unchanged"],
        token=token,
    )


@employees_bp.route("/clients/<int:client_id>/bulk-import/confirm", methods=["POST"])
@role_required(*REP_ROLES)
def bulk_import_confirm(client_id):
    client = db.get_or_404(ClientCompany, client_id)  # 404 guard for an unknown client
    token = session.get(_SESSION_KEY)
    if not token or request.form.get("token") not in (None, "", token):
        flash("Session mismatch — please re-upload the file.", "danger")
        return redirect(url_for("employees.bulk_import", client_id=client_id))
    staged = roster_import.load_staged(token, client_id)
    if staged is None:
        flash("Import session expired — please re-upload the file.", "danger")
        return redirect(url_for("employees.bulk_import", client_id=client_id))

    existing = roster_import.existing_by_staff_id(
        Employee.query.filter_by(client_company_id=client_id)
    )
    created, updated = roster_import.apply_roster(staged, client, existing)
    db.session.commit()

    session.pop(_SESSION_KEY, None)
    roster_import.discard_staged(token)
    flash(roster_import.result_message(created, updated), "success")
    return redirect(url_for("employees.roster", client_id=client_id))
