"""Client (tenant) plane — the self-service interface a client company uses to
see and manage ONLY its own data.

Every route is `@tenant_required` (a platform user is redirected to the oversight
console) and reads/writes through the tenancy choke point — `tenant_query()` for
lists and `tenant_get_or_404()` for objects — so a client user can never touch
another tenant's row. Templates are a standalone client shell (no operator base),
so cross-company / operator-only controls simply do not exist here.

Full self-service (Sina, 2026-07-16): client_admin/client_preparer manage their
own employees AND upload a standard payroll workbook to prepare a run. The upload
(``run_upload``) reuses the operator import pipeline with client_company_id forced
to the tenant, then routes the new run through the Phase 5 risk gate
(Submitted -> Held/Auto-Accepted). Raw-hours runs stay a Chrisnat operator flow.
Statutory rates are global and view-only for clients.
"""

import io
import os
import uuid
import zipfile
from datetime import date, datetime, timezone

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
from app.events import platform_admins, record_event
from app.distribution.idempotency import replay_or_run
from app.distribution.service import distribute_run, resolve_channel
from app.excel_utils import allowed_excel_file, mapping_conflicts
from app.models import (
    CHANNEL_AUTO,
    DELIVERY_CHANNELS,
    DELIVERY_FAILED,
    DELIVERY_SENT,
    AuditTrail,
    ClientCompany,
    Employee,
    Expense,
    PayrollItem,
    PayrollRun,
    PayslipDelivery,
    StatutoryRate,
    User,
)
from app.payroll import (
    build_single_payload,
    create_payroll_run_from_payload,
    has_duplicate_payroll,
    save_temporary_upload,
)
from app.payroll_status import AUTO_ACCEPTED, HELD, SENDABLE_STATUSES, SUBMITTED
from app.pdf_service import generate_payslip_pdf, payslip_filename
from app.raw_engine.detection import looks_like_raw_hours
from app.raw_import import normalise_emp_id
from app.risk import apply_risk_gate
from app.roles import CLIENT_ADMIN, CLIENT_PREPARER
from app.tenancy import (
    active_tenant_id,
    tenant_get_or_404,
    tenant_query,
    tenant_required,
    tenant_role_required,
)
from app.validators import collect_blocking_errors

_VALID_SEND_CHANNELS = set(DELIVERY_CHANNELS) | {CHANNEL_AUTO}

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


# --- Run upload (self-service run preparation) ------------------------------
# A client prepares a payroll run by uploading a STANDARD payroll workbook for
# their own company. It reuses the operator import pipeline (build_single_payload
# + create_payroll_run_from_payload, which runs the frozen statutory engine
# identically) but with client_company_id forced to the tenant — never detected
# from the file. The new run enters the Phase 5 lifecycle: Submitted -> risk gate
# -> Held (Chrisnat review) or Auto-Accepted. Raw-hours workbooks are refused;
# those remain a Chrisnat operator flow.
@client_bp.route("/runs/upload", methods=["GET", "POST"])
@tenant_role_required(CLIENT_ADMIN, CLIENT_PREPARER)
def run_upload():
    company = _company()
    now = datetime.now()
    if request.method == "GET":
        return render_template(
            "client/run_upload.html",
            company=company,
            current_month=now.strftime("%B"),
            current_year=now.year,
        )

    file_storage = request.files.get("payroll_file")
    if not file_storage or not file_storage.filename:
        flash("Choose an Excel file to upload.", "warning")
        return redirect(url_for("client.run_upload"))
    if not allowed_excel_file(file_storage.filename):
        flash("Only .xlsx, .xls, or .csv files are supported.", "warning")
        return redirect(url_for("client.run_upload"))

    month = (request.form.get("month") or now.strftime("%B")).strip()
    try:
        year = int(request.form.get("year") or now.year)
    except (TypeError, ValueError):
        year = now.year
    source_filename = file_storage.filename
    file_path = save_temporary_upload(file_storage)
    try:
        if looks_like_raw_hours(file_path):
            flash(
                "That looks like a raw-hours workbook. Raw-hours runs are prepared "
                "by Chrisnat — please upload a standard payroll workbook.",
                "warning",
            )
            return redirect(url_for("client.run_upload"))
        # client_company_id is forced to the tenant here — the file is never
        # allowed to decide which company it lands in.
        payload, error = build_single_payload(file_path, source_filename, company, month, year)
    finally:
        try:
            os.remove(file_path)
        except OSError:
            pass

    if error:
        flash(error, "danger")
        return redirect(url_for("client.run_upload"))

    blocking = mapping_conflicts(payload.get("mapping") or {}) + collect_blocking_errors(
        payload["mapped_rows"], payload.get("detected_company_name", "")
    )
    if blocking:
        for message in blocking[:10]:
            flash(message, "danger")
        flash("Upload blocked — fix the highlighted issues and try again.", "danger")
        return redirect(url_for("client.run_upload"))

    if has_duplicate_payroll(company.id, payload["month"], payload["year"]):
        flash(
            f"A {payload['month']} {payload['year']} payroll already exists for your "
            "company. Contact Chrisnat to replace it.",
            "warning",
        )
        return redirect(url_for("client.run_upload"))

    run = create_payroll_run_from_payload(payload, company, payload["validation"], "single_client")
    # Phase 5 lifecycle: a client submission is risk-gated, not auto-approved.
    run.status = SUBMITTED
    db.session.flush()
    verdict = apply_risk_gate(run, when=datetime.now(timezone.utc))
    run.status = HELD if verdict.held else AUTO_ACCEPTED
    reasons = verdict.reasons_text() or "no rule tripped"
    record_audit(
        "Client run uploaded",
        run,
        f"{run.month} {run.year} uploaded by client from {source_filename}. "
        f"Risk: {verdict.status} ({reasons}).",
    )
    record_event(
        "run.risk_held" if verdict.held else "run.risk_accepted",
        summary=f"{company.name} submitted {run.month} {run.year}: {reasons}.",
        subject=run,
        client_company_id=company.id,
        level="warning" if verdict.held else "info",
        payload={"status": verdict.status, "reasons": verdict.reasons},
        recipients=platform_admins(),
    )
    db.session.commit()
    if verdict.held:
        flash(
            f"{run.month} {run.year} payroll uploaded and sent to Chrisnat for review.",
            "success",
        )
    else:
        flash(f"{run.month} {run.year} payroll uploaded and auto-accepted.", "success")
    return redirect(url_for("client.run_detail", run_id=run.id))


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


# --- Audit trail (read, tenant-scoped) -------------------------------------
@client_bp.route("/audit")
@tenant_required
def audit():
    # AuditTrail has no client_company_id, so scope by the acting user's tenant
    # (§4): entries recorded by users belonging to this company. Never leaks
    # another tenant's activity.
    user_ids = [u.id for u in User.query.filter_by(client_company_id=active_tenant_id()).all()]
    entries = (
        AuditTrail.query.filter(AuditTrail.user_id.in_(user_ids))
        .order_by(AuditTrail.created_at.desc())
        .limit(200)
        .all()
        if user_ids
        else []
    )
    return render_template("client/audit.html", company=_company(), entries=entries)


# --- Payslip distribution ---------------------------------------------------
# The client's own distribution surface. v1 primary channel is a payslip
# download (single PDF or a run-wide ZIP); SMS / WhatsApp / email reuse the
# Chrisnat distribution service and stay console-backed until real backends are
# configured. Sending is client_admin-only; viewing/downloading is any tenant
# user. A run is fetched via tenant_get_or_404 so a client can only ever
# distribute their own run.
def _latest_delivery(item_id):
    return (
        PayslipDelivery.query.filter_by(payroll_item_id=item_id)
        .order_by(PayslipDelivery.created_at.desc())
        .first()
    )


@client_bp.route("/runs/<int:run_id>/distribute")
@tenant_required
def distribute(run_id):
    run = tenant_get_or_404(PayrollRun, run_id)  # 404 if another tenant's run
    rows = [
        {"item": it, "delivery": _latest_delivery(it.id), "suggested": resolve_channel(it)}
        for it in run.items
    ]
    sent = sum(1 for r in rows if r["delivery"] and r["delivery"].status == DELIVERY_SENT)
    failed = sum(1 for r in rows if r["delivery"] and r["delivery"].status == DELIVERY_FAILED)
    return render_template(
        "client/distribute.html",
        company=_company(),
        run=run,
        rows=rows,
        channels=DELIVERY_CHANNELS,
        sendable=run.status in SENDABLE_STATUSES,
        can_send=active_tenant_id() is not None
        and (current_user.role or "").strip().lower() == CLIENT_ADMIN,
        nonce=uuid.uuid4().hex,
        sent_count=sent,
        failed_count=failed,
    )


def _do_client_send(run, only_failed):
    if run.status not in SENDABLE_STATUSES:
        flash("Payslips can only be sent after the payroll run is approved.", "warning")
        return redirect(url_for("client.distribute", run_id=run.id))
    channel = request.form.get("channel", CHANNEL_AUTO)
    if channel not in _VALID_SEND_CHANNELS:
        flash(f"Unknown channel: {channel}", "warning")
        return redirect(url_for("client.distribute", run_id=run.id))
    nonce = request.form.get("nonce")
    action = "resend-failed" if only_failed else "send"
    key = f"client-distribute:{run.id}:{action}:{channel}:{nonce}" if nonce else None
    summary, replayed = replay_or_run(
        key, lambda: distribute_run(run, channel=channel, only_failed=only_failed)
    )
    if not replayed:
        # Notify Chrisnat oversight that a client distributed payslips
        # (tenant -> platform direction). distribute_run already committed;
        # this event + its notifications are committed here.
        record_event(
            "payslips.distributed",
            summary=(
                f"{run.month} {run.year}: {summary['sent']} sent, "
                f"{summary['failed']} failed (of {summary['total']}) via {channel}."
            ),
            subject=run,
            client_company_id=run.client_company_id,
            level="info",
            payload={k: summary.get(k) for k in ("sent", "failed", "skipped", "total")},
            recipients=platform_admins(),
        )
        db.session.commit()
    note = " (already processed)" if replayed else ""
    failed_workers = summary.get("failed_workers") or []
    followup = ""
    if failed_workers:
        shown = ", ".join(failed_workers[:10])
        more = f" +{len(failed_workers) - 10} more" if len(failed_workers) > 10 else ""
        followup = f" No roster contact for: {shown}{more}."
    flash(
        f"Distribution complete{note}: {summary['sent']} sent, {summary['failed']} failed, "
        f"{summary['skipped']} skipped (of {summary['total']}).{followup}",
        "success" if not summary["failed"] else "warning",
    )
    return redirect(url_for("client.distribute", run_id=run.id))


@client_bp.route("/runs/<int:run_id>/distribute/send", methods=["POST"])
@tenant_role_required(CLIENT_ADMIN)
def distribute_send(run_id):
    run = tenant_get_or_404(PayrollRun, run_id)
    return _do_client_send(run, only_failed=False)


@client_bp.route("/runs/<int:run_id>/distribute/resend-failed", methods=["POST"])
@tenant_role_required(CLIENT_ADMIN)
def distribute_resend(run_id):
    run = tenant_get_or_404(PayrollRun, run_id)
    return _do_client_send(run, only_failed=True)


@client_bp.route("/runs/<int:run_id>/payslips.zip")
@tenant_required
def payslips_zip(run_id):
    """Download every payslip in the run as one ZIP — the v1 primary channel."""
    run = tenant_get_or_404(PayrollRun, run_id)  # 404 if another tenant's run
    export_folder = current_app.config["EXPORT_FOLDER"]
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for item in run.items:
            pdf_path = generate_payslip_pdf(item, export_folder)
            archive.write(pdf_path, arcname=payslip_filename(item))
    buffer.seek(0)
    download_name = f"payslips_{run.month}_{run.year}.zip".replace(" ", "_")
    return send_file(
        buffer, mimetype="application/zip", as_attachment=True, download_name=download_name
    )
