import json
import os
import tempfile
from datetime import datetime, timezone

from flask import Blueprint, current_app, flash, redirect, render_template, request, send_file, url_for
from flask_login import current_user, login_required

from app import db
from app.audit import record_audit
from app.auth import role_required
from app.excel_utils import (
    allowed_excel_file,
    detect_company_name,
    extract_payroll_sheet,
    export_payroll_run,
    export_import_error_report,
    match_client_sheet,
    normalize_company_key,
    payroll_sheet_candidates,
    workbook_sheet_names,
)
from app.finance import create_finance_records_for_payroll
from app.models import ClientCompany, Employee, ImportBatch, PayrollItem, PayrollRun
from app.pdf_service import generate_payslip_pdf
from app.validators import validate_payroll_rows

payroll_bp = Blueprint("payroll", __name__, url_prefix="/payroll")


def save_import_session(payload):
    batch = db.session.get(ImportBatch, int(payload["import_batch_id"]))
    batch.payload_json = json.dumps(payload, indent=2)
    db.session.commit()
    return str(batch.id)


def load_import_session(import_id):
    batch = db.get_or_404(ImportBatch, int(import_id))
    return json.loads(batch.payload_json or "{}")


def summarize_import(mapped_rows, validation):
    invalid_rows = len(validation["per_row_warnings"])
    return {
        "total_rows": len(mapped_rows),
        "valid_rows": max(len(mapped_rows) - invalid_rows, 0),
        "invalid_rows": invalid_rows,
        "gross_total": sum(float(row.get("gross_pay") or 0) for row in mapped_rows),
        "net_total": sum(float(row.get("net_pay") or 0) for row in mapped_rows),
        "paye_total": sum(float(row.get("paye") or 0) for row in mapped_rows),
        "ssnit_total": sum(float(row.get("ssnit") or 0) for row in mapped_rows),
        "deductions_total": sum(float(row.get("total_deductions") or 0) for row in mapped_rows),
    }


def has_duplicate_payroll(client_id, month, year):
    return (
        PayrollRun.query.filter_by(
            client_company_id=client_id,
            month=month,
            year=int(year),
        ).first()
        is not None
    )


def save_temporary_upload(file_storage):
    """Render files are ephemeral; uploaded workbooks are saved only long enough to parse."""
    suffix = os.path.splitext(file_storage.filename or "")[1]
    handle = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
    handle.close()
    file_storage.save(handle.name)
    return handle.name


def match_client_for_sheet(sheet_name, clients):
    sheet_key = normalize_company_key(sheet_name)
    for client in clients:
        client_key = normalize_company_key(client.name)
        if client_key == sheet_key or client_key in sheet_key or sheet_key in client_key:
            return client
    return None


def build_single_payload(file_path, source_filename, client, month, year, selected_sheet_name=None):
    known_names = [company.name for company in ClientCompany.query.all()]
    sheet_names = workbook_sheet_names(file_path)
    candidates = payroll_sheet_candidates(file_path)
    current_app.logger.info(
        "Smart Excel Import Engine: workbook sheets=%s selected_client=%s candidates=%s",
        sheet_names,
        client.name,
        candidates,
    )
    if not candidates:
        return None, "No valid payroll rows were found. Please check the selected sheet, header row, or Excel format."

    matched_sheet_name = selected_sheet_name or match_client_sheet(
        client.name, [candidate["sheet_name"] for candidate in candidates]
    )
    if not matched_sheet_name and len(candidates) == 1:
        matched_sheet_name = candidates[0]["sheet_name"]
    if not matched_sheet_name:
        available = ", ".join(candidate["sheet_name"] for candidate in candidates)
        return None, f"No matching payroll sheet found for selected client company. Available payroll-looking sheets are: {available}"

    extraction = extract_payroll_sheet(file_path, matched_sheet_name)
    mapped_rows = extraction["mapped_rows"]
    if not mapped_rows:
        current_app.logger.warning(
            "Smart Excel Import Engine extracted 0 rows from sheet=%s file=%s",
            matched_sheet_name,
            source_filename,
        )
        return None, "No valid payroll rows were found. Please check the selected sheet, header row, or Excel format."

    detected_company_name = detect_company_name(file_path, known_names, matched_sheet_name)
    validation = validate_payroll_rows(mapped_rows, client, month, year, detected_company_name)
    import_summary = summarize_import(mapped_rows, validation)
    current_app.logger.info(
        "Smart Excel Import Engine: matched_sheet=%s header_row=%s columns=%s mapping=%s rows=%s ignored=%s warnings=%s",
        matched_sheet_name,
        extraction["detected_header_row"],
        extraction["columns"],
        extraction["mapping"],
        len(mapped_rows),
        extraction["ignored_rows"],
        len(validation["per_row_warnings"]) + len(validation["summary_warnings"]),
    )

    return {
        "mode": "single_client",
        "source_filename": source_filename,
        "matched_sheet_name": matched_sheet_name,
        "detected_header_row": extraction["detected_header_row"],
        "client_company_id": client.id,
        "client_company_name": client.name,
        "month": month,
        "year": year,
        "columns": extraction["columns"],
        "mapping": extraction["mapping"],
        "unmapped_columns": [column for column, field in extraction["mapping"].items() if field == "unmapped"],
        "preview_rows": extraction["preview_rows"],
        "mapped_rows": mapped_rows,
        "worker_stats": extraction["worker_stats"],
        "status_breakdown": extraction["status_breakdown"],
        "import_summary": import_summary,
        "detected_company_name": detected_company_name,
        "validation": validation,
    }, None


def build_multi_client_payload(file_path, source_filename, month, year):
    clients = ClientCompany.query.filter_by(status="Active").all()
    candidates = payroll_sheet_candidates(file_path)
    current_app.logger.info(
        "Smart Excel Import Engine multi-client: workbook sheets=%s candidates=%s",
        workbook_sheet_names(file_path),
        candidates,
    )
    if not candidates:
        return None, "No valid payroll rows were found. Please check the selected sheet, header row, or Excel format."

    runs = []
    unmatched_sheets = []
    known_names = [company.name for company in ClientCompany.query.all()]
    for candidate in candidates:
        client = match_client_for_sheet(candidate["sheet_name"], clients)
        if not client:
            unmatched_sheets.append(candidate)
            continue
        extraction = extract_payroll_sheet(file_path, candidate["sheet_name"])
        if not extraction["mapped_rows"]:
            continue
        detected_company_name = detect_company_name(file_path, known_names, candidate["sheet_name"])
        validation = validate_payroll_rows(
            extraction["mapped_rows"], client, month, year, detected_company_name
        )
        import_summary = summarize_import(extraction["mapped_rows"], validation)
        runs.append(
            {
                "mode": "single_client",
                "source_filename": source_filename,
                "matched_sheet_name": candidate["sheet_name"],
                "detected_header_row": extraction["detected_header_row"],
                "client_company_id": client.id,
                "client_company_name": client.name,
                "month": month,
                "year": year,
                "columns": extraction["columns"],
                "mapping": extraction["mapping"],
                "unmapped_columns": [column for column, field in extraction["mapping"].items() if field == "unmapped"],
                "preview_rows": extraction["preview_rows"],
                "mapped_rows": extraction["mapped_rows"],
                "worker_stats": extraction["worker_stats"],
                "status_breakdown": extraction["status_breakdown"],
                "import_summary": import_summary,
                "detected_company_name": detected_company_name,
                "validation": validation,
            }
        )

    if not runs:
        return None, "No valid matched client payroll rows were found. Please check client sheet names and Excel format."

    return {
        "mode": "multi_client",
        "source_filename": source_filename,
        "month": month,
        "year": year,
        "runs": runs,
        "unmatched_sheets": unmatched_sheets,
    }, None


@payroll_bp.route("/runs")
@login_required
def runs():
    selected_client = None
    query = PayrollRun.query
    client_id = request.args.get("client_id")
    status_filter = request.args.get("status", "")
    if client_id:
        selected_client = db.get_or_404(ClientCompany, client_id)
        query = query.filter(PayrollRun.client_company_id == selected_client.id)
    if status_filter == "needs_approval":
        query = query.filter(PayrollRun.status.in_(["Draft", "Pending Review", "Pending MD Approval"]))
    elif status_filter:
        query = query.filter(PayrollRun.status == status_filter)
    payroll_runs = query.order_by(PayrollRun.created_at.desc()).all()
    return render_template(
        "payroll_runs.html",
        payroll_runs=payroll_runs,
        selected_client=selected_client,
        status_filter=status_filter,
    )


@payroll_bp.route("/upload", methods=["GET", "POST"])
@role_required("admin")
def upload():
    clients = ClientCompany.query.filter_by(status="Active").order_by(ClientCompany.name).all()
    now = datetime.now()
    if request.method == "POST":
        import_mode = request.form.get("import_mode") or "single_client"
        client_id = request.form.get("client_company_id")
        if import_mode == "single_client" and not client_id:
            flash("Select a client company before uploading payroll.", "warning")
            return redirect(url_for("payroll.upload"))

        month = request.form.get("month") or now.strftime("%B")
        year = int(request.form.get("year") or now.year)
        file_storage = request.files.get("payroll_file")
        if not file_storage or not file_storage.filename:
            flash("Choose an Excel file to upload.", "warning")
            return redirect(url_for("payroll.upload"))
        if not allowed_excel_file(file_storage.filename):
            flash("Only .xlsx, .xls, or .csv files are supported.", "warning")
            return redirect(url_for("payroll.upload"))

        source_filename = file_storage.filename
        file_path = save_temporary_upload(file_storage)
        try:
            if import_mode == "multi_client":
                payload, error = build_multi_client_payload(file_path, source_filename, month, year)
                client_for_batch = None
            else:
                client = db.get_or_404(ClientCompany, client_id)
                payload, error = build_single_payload(file_path, source_filename, client, month, year)
                client_for_batch = client
        finally:
            try:
                os.remove(file_path)
            except OSError:
                pass

        if error:
            flash(error, "danger")
            return redirect(url_for("payroll.upload"))

        batch_client_id = (
            client_for_batch.id
            if client_for_batch
            else payload["runs"][0]["client_company_id"]
        )

        batch = ImportBatch(
            client_company_id=batch_client_id,
            payroll_month=month,
            payroll_year=year,
            uploaded_by=current_user.id,
            original_filename=source_filename,
            import_mode=payload["mode"],
            source_sheet_name=payload.get("matched_sheet_name"),
            status="Previewed",
            total_rows=(payload["import_summary"]["total_rows"] if payload["mode"] == "single_client" else sum(run["import_summary"]["total_rows"] for run in payload["runs"])),
            valid_rows=(payload["import_summary"]["valid_rows"] if payload["mode"] == "single_client" else sum(run["import_summary"]["valid_rows"] for run in payload["runs"])),
            invalid_rows=(payload["import_summary"]["invalid_rows"] if payload["mode"] == "single_client" else sum(run["import_summary"]["invalid_rows"] for run in payload["runs"])),
            total_workers=(payload["worker_stats"]["total_unique_workers"] if payload["mode"] == "single_client" else sum(run["worker_stats"]["total_unique_workers"] for run in payload["runs"])),
            gross_total=(payload["import_summary"]["gross_total"] if payload["mode"] == "single_client" else sum(run["import_summary"]["gross_total"] for run in payload["runs"])),
            net_total=(payload["import_summary"]["net_total"] if payload["mode"] == "single_client" else sum(run["import_summary"]["net_total"] for run in payload["runs"])),
            paye_total=(payload["import_summary"]["paye_total"] if payload["mode"] == "single_client" else sum(run["import_summary"]["paye_total"] for run in payload["runs"])),
            ssnit_total=(payload["import_summary"]["ssnit_total"] if payload["mode"] == "single_client" else sum(run["import_summary"]["ssnit_total"] for run in payload["runs"])),
            validation_summary=(
                "\n".join(payload["validation"]["summary_warnings"])
                if payload["mode"] == "single_client"
                else "\n".join(warning for run in payload["runs"] for warning in run["validation"]["summary_warnings"])
            ),
        )
        db.session.add(batch)
        db.session.flush()
        record_audit("Payroll upload", batch, f"Uploaded {source_filename} for preview.")
        payload["import_batch_id"] = batch.id
        import_id = save_import_session(payload)
        return redirect(url_for("payroll.preview", import_id=import_id))

    return render_template(
        "payroll_upload.html",
        clients=clients,
        current_month=now.strftime("%B"),
        current_year=now.year,
    )


@payroll_bp.route("/preview/<import_id>")
@role_required("admin")
def preview(import_id):
    payload = load_import_session(import_id)
    client = None
    if payload.get("mode") != "multi_client":
        client = db.get_or_404(ClientCompany, payload["client_company_id"])
    return render_template(
        "payroll_preview.html",
        import_id=import_id,
        payload=payload,
        client=client,
    )


@payroll_bp.route("/preview/<import_id>/errors")
@role_required("admin")
def error_report(import_id):
    payload = load_import_session(import_id)
    file_path = export_import_error_report(payload, current_app.config["EXPORT_FOLDER"])
    return send_file(file_path, as_attachment=True)


@payroll_bp.route("/confirm/<import_id>", methods=["POST"])
@role_required("admin")
def confirm(import_id):
    payload = load_import_session(import_id)
    if payload.get("mode") == "multi_client":
        return confirm_multi_client_import(import_id, payload)
    if not payload.get("mapped_rows"):
        flash(
            "No valid payroll rows were found. Please check the selected sheet, header row, or Excel format.",
            "danger",
        )
        return redirect(url_for("payroll.upload"))
    client = db.get_or_404(ClientCompany, payload["client_company_id"])
    validation = validate_payroll_rows(
        payload["mapped_rows"],
        client,
        payload["month"],
        payload["year"],
        payload.get("detected_company_name", ""),
    )
    duplicate_exists = has_duplicate_payroll(client.id, payload["month"], payload["year"])
    if duplicate_exists and request.form.get("replace_existing") != "1":
        flash("Duplicate payroll found. Tick confirm replacement before importing this client/month again.", "warning")
        return redirect(url_for("payroll.preview", import_id=import_id))

    payroll_run = create_payroll_run_from_payload(payload, client, validation, "single_client")
    batch_id = payload.get("import_batch_id")
    if batch_id:
        batch = db.session.get(ImportBatch, batch_id)
        if batch:
            batch.status = "Imported"
            batch.payroll_run_id = payroll_run.id
    record_audit("Payroll import confirmed", payroll_run, f"Created payroll run from {payload['source_filename']}.")
    db.session.commit()

    flash("Company-specific payroll run created in Draft status.", "success")
    return redirect(url_for("payroll.detail", run_id=payroll_run.id))


def confirm_multi_client_import(import_id, payload):
    runs = payload.get("runs", [])
    if not runs:
        flash(
            "No valid payroll rows were found. Please check the selected sheet, header row, or Excel format.",
            "danger",
        )
        return redirect(url_for("payroll.upload"))

    duplicates = []
    for run_payload in runs:
        if has_duplicate_payroll(run_payload["client_company_id"], run_payload["month"], run_payload["year"]):
            duplicates.append(run_payload["client_company_name"])
    if duplicates and request.form.get("replace_existing") != "1":
        flash(
            f"Duplicate payroll found for: {', '.join(duplicates)}. Tick confirm replacement before importing again.",
            "warning",
        )
        return redirect(url_for("payroll.preview", import_id=import_id))

    created_runs = []
    for run_payload in runs:
        client = db.get_or_404(ClientCompany, run_payload["client_company_id"])
        validation = validate_payroll_rows(
            run_payload["mapped_rows"],
            client,
            run_payload["month"],
            run_payload["year"],
            run_payload.get("detected_company_name", ""),
        )
        payroll_run = create_payroll_run_from_payload(run_payload, client, validation, "multi_client")
        created_runs.append(payroll_run)
        record_audit("Payroll import confirmed", payroll_run, f"Created multi-client payroll run from {run_payload['source_filename']}.")

    batch = db.session.get(ImportBatch, int(import_id))
    batch.status = "Imported"
    if created_runs:
        batch.payroll_run_id = created_runs[0].id
    db.session.commit()
    flash(f"{len(created_runs)} client payroll runs created in Draft status.", "success")
    return redirect(url_for("payroll.runs"))


def create_payroll_run_from_payload(payload, client, validation, import_mode):
    status_breakdown = payload.get("status_breakdown") or {}
    payroll_run = PayrollRun(
        month=payload["month"],
        year=int(payload["year"]),
        status="Draft",
        created_by=current_user.id,
        client_company_id=client.id,
        total_workers=payload["worker_stats"]["total_unique_workers"],
        total_unique_workers=payload["worker_stats"]["total_unique_workers"],
        total_rows_imported=payload["worker_stats"]["total_rows"],
        duplicate_workers_found=payload["worker_stats"]["duplicate_count"],
        source_filename=payload["source_filename"],
        source_sheet_name=payload.get("matched_sheet_name"),
        detected_header_row=payload.get("detected_header_row") or 0,
        import_mode=import_mode,
        import_type="Multi-Sheet Upload" if import_mode == "multi_client" else "Single Company Upload",
        detected_company_name=payload.get("detected_company_name"),
        active_workers=status_breakdown.get("active", 0),
        inactive_workers=status_breakdown.get("inactive", 0),
        terminated_workers=status_breakdown.get("terminated", 0),
        on_leave_workers=status_breakdown.get("on_leave", 0),
        unknown_status_workers=status_breakdown.get("unknown", 0),
        notes="\n".join(validation["summary_warnings"]),
    )
    db.session.add(payroll_run)
    db.session.flush()

    totals = {
        "gross": 0,
        "deductions": 0,
        "net": 0,
        "paye": 0,
        "ssnit": 0,
    }
    for index, row in enumerate(payload["mapped_rows"], start=1):
        if not str(row.get("staff_id") or "").strip() and not str(row.get("full_name") or "").strip():
            continue

        employee = create_or_update_employee_from_import(row, client, payroll_run, index)
        db.session.flush()

        warnings = validation["per_row_warnings"].get(str(index)) or validation[
            "per_row_warnings"
        ].get(index, [])
        item = PayrollItem(
            payroll_run_id=payroll_run.id,
            employee_id=employee.id if employee else None,
            staff_id=row.get("staff_id"),
            full_name=row.get("full_name"),
            status=row.get("status"),
            service_line=row.get("service_line"),
            job_role=row.get("job_role"),
            payroll_month=row.get("payroll_month"),
            ssnit_number=row.get("ssnit_number"),
            ghana_card_number=row.get("ghana_card_number"),
            bank_name=row.get("bank_name"),
            bank_account_number=row.get("bank_account_number"),
            momo_number=row.get("momo_number"),
            basic_salary=float(row.get("basic_salary") or 0),
            transport_allowance=float(row.get("transport_allowance") or 0),
            housing_allowance=float(row.get("housing_allowance") or 0),
            overtime_hours=float(row.get("overtime_hours") or 0),
            overtime_pay=float(row.get("overtime_pay") or 0),
            other_allowances=float(row.get("other_allowances") or 0),
            gross_pay=float(row.get("gross_pay") or 0),
            paye=float(row.get("paye") or 0),
            ssnit=float(row.get("ssnit") or 0),
            tier_2_pension=float(row.get("tier_2_pension") or 0),
            loan_deduction=float(row.get("loan_deduction") or 0),
            other_deductions=float(row.get("other_deductions") or 0),
            total_deductions=float(row.get("total_deductions") or 0),
            net_pay=float(row.get("net_pay") or 0),
            validation_status="Warning" if warnings else "OK",
            warning_notes="; ".join(warnings),
        )
        db.session.add(item)
        totals["gross"] += item.gross_pay
        totals["deductions"] += item.total_deductions
        totals["net"] += item.net_pay
        totals["paye"] += item.paye
        totals["ssnit"] += item.ssnit

    payroll_run.total_gross_pay = totals["gross"]
    payroll_run.total_deductions = totals["deductions"]
    payroll_run.total_net_pay = totals["net"]
    payroll_run.total_paye = totals["paye"]
    payroll_run.total_ssnit = totals["ssnit"]
    return payroll_run


def create_or_update_employee_from_import(row, client, payroll_run, row_index):
    staff_id = str(row.get("staff_id") or "").strip()
    full_name = str(row.get("full_name") or "").strip()
    if not staff_id:
        staff_id = f"IMPORT-{client.id}-{payroll_run.id}-{row_index}"
    if not full_name:
        full_name = f"Imported Worker {staff_id}"

    employee = Employee.query.filter_by(
        staff_id=staff_id,
        client_company_id=client.id,
    ).first()
    if employee is None:
        employee = Employee(
            staff_id=staff_id,
            client_company_id=client.id,
            status="Active",
            employment_type="Imported Payroll Worker",
            service_line="Personnel Outsourcing",
        )
        db.session.add(employee)

    employee.full_name = full_name
    employee.ssnit_number = row.get("ssnit_number") or employee.ssnit_number
    employee.ghana_card_number = row.get("ghana_card_number") or employee.ghana_card_number
    employee.bank_name = row.get("bank_name") or employee.bank_name
    employee.bank_account_number = row.get("bank_account_number") or employee.bank_account_number
    employee.momo_number = row.get("momo_number") or employee.momo_number
    employee.status = row.get("status") or employee.status
    employee.service_line = row.get("service_line") or employee.service_line
    employee.basic_salary = float(row.get("basic_salary") or employee.basic_salary or 0)
    employee.assigned_client = client.name
    return employee


@payroll_bp.route("/runs/<int:run_id>")
@login_required
def detail(run_id):
    payroll_run = db.get_or_404(PayrollRun, run_id)
    return render_template("payroll_detail.html", payroll_run=payroll_run)


@payroll_bp.route("/runs/<int:run_id>/mark-reviewed", methods=["POST"])
@role_required("admin")
def mark_reviewed(run_id):
    payroll_run = db.get_or_404(PayrollRun, run_id)
    payroll_run.status = "Pending Review"
    record_audit("Payroll edit", payroll_run, "Payroll moved to Pending Review.")
    db.session.commit()
    flash("Payroll submitted for accounts review.", "success")
    return redirect(url_for("payroll.detail", run_id=run_id))


@payroll_bp.route("/runs/<int:run_id>/submit-review", methods=["POST"])
@role_required("admin")
def submit_review(run_id):
    return mark_reviewed(run_id)


@payroll_bp.route("/runs/<int:run_id>/submit-md-approval", methods=["POST"])
@role_required("admin", "accounts_officer")
def submit_md_approval(run_id):
    payroll_run = db.get_or_404(PayrollRun, run_id)
    payroll_run.status = "Pending MD Approval"
    payroll_run.reviewed_by = current_user.id
    payroll_run.reviewed_at = datetime.now(timezone.utc)
    record_audit("Payroll review", payroll_run, "Accounts submitted payroll for MD approval.")
    db.session.commit()
    flash("Payroll sent for MD approval.", "success")
    return redirect(url_for("payroll.detail", run_id=run_id))


@payroll_bp.route("/runs/<int:run_id>/approve", methods=["POST"])
@role_required("admin", "md")
def approve(run_id):
    payroll_run = db.get_or_404(PayrollRun, run_id)
    payroll_run.status = "Approved"
    payroll_run.approved_by = current_user.id
    payroll_run.approved_at = datetime.now(timezone.utc)
    create_finance_records_for_payroll(payroll_run, current_user.id)
    record_audit("Payroll approval", payroll_run, "MD/Admin approved payroll.")
    db.session.commit()
    flash("Payroll approved. Voucher and statutory remittance records were prepared.", "success")
    return redirect(url_for("payroll.detail", run_id=run_id))


@payroll_bp.route("/runs/<int:run_id>/reject", methods=["POST"])
@role_required("admin", "md")
def reject(run_id):
    payroll_run = db.get_or_404(PayrollRun, run_id)
    payroll_run.status = "Rejected"
    payroll_run.rejected_at = datetime.now(timezone.utc)
    payroll_run.notes = request.form.get("notes") or payroll_run.notes
    record_audit("Payroll rejection", payroll_run, payroll_run.notes or "Payroll rejected.")
    db.session.commit()
    flash("Payroll rejected.", "warning")
    return redirect(url_for("payroll.detail", run_id=run_id))


@payroll_bp.route("/runs/<int:run_id>/mark-paid", methods=["POST"])
@role_required("admin", "accounts_officer")
def mark_paid(run_id):
    payroll_run = db.get_or_404(PayrollRun, run_id)
    payroll_run.status = "Paid"
    if payroll_run.voucher:
        payroll_run.voucher.status = "Paid"
        payroll_run.voucher.date_paid = datetime.now(timezone.utc)
    record_audit("Payment marked as paid", payroll_run, "Accounts marked payroll voucher as paid.")
    db.session.commit()
    flash("Payroll marked as paid.", "success")
    return redirect(url_for("payroll.detail", run_id=run_id))


@payroll_bp.route("/runs/<int:run_id>/export")
@login_required
def export(run_id):
    payroll_run = db.get_or_404(PayrollRun, run_id)
    file_path = export_payroll_run(payroll_run, current_app.config["EXPORT_FOLDER"])
    payroll_run.status = "Exported"
    db.session.commit()
    return send_file(file_path, as_attachment=True)


@payroll_bp.route("/items/<int:item_id>/payslip")
@login_required
def payslip(item_id):
    payroll_item = db.get_or_404(PayrollItem, item_id)
    file_path = generate_payslip_pdf(payroll_item, current_app.config["EXPORT_FOLDER"])
    return send_file(
        file_path,
        mimetype="application/pdf",
        as_attachment=True,
        download_name=os.path.basename(file_path),
    )
