import json
import os
import uuid
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
    payroll_sheet_candidates,
    save_uploaded_file,
    workbook_sheet_names,
)
from app.finance import create_finance_records_for_payroll
from app.models import ClientCompany, Employee, ImportBatch, PayrollItem, PayrollRun
from app.pdf_service import generate_payslip_pdf
from app.validators import validate_payroll_rows

payroll_bp = Blueprint("payroll", __name__, url_prefix="/payroll")


def import_session_path(import_id):
    return os.path.join(current_app.config["IMPORT_SESSION_FOLDER"], f"{import_id}.json")


def save_import_session(payload):
    import_id = str(uuid.uuid4())
    with open(import_session_path(import_id), "w", encoding="utf-8") as file:
        json.dump(payload, file, indent=2)
    return import_id


def load_import_session(import_id):
    with open(import_session_path(import_id), "r", encoding="utf-8") as file:
        return json.load(file)


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
        client_id = request.form.get("client_company_id")
        if not client_id:
            flash("Select a client company before uploading payroll.", "warning")
            return redirect(url_for("payroll.upload"))

        client = db.get_or_404(ClientCompany, client_id)
        known_names = [company.name for company in ClientCompany.query.all()]
        month = request.form.get("month") or now.strftime("%B")
        year = int(request.form.get("year") or now.year)
        selected_sheet_name = request.form.get("selected_sheet_name")
        existing_file_path = request.form.get("existing_file_path")
        source_filename = request.form.get("source_filename")

        if existing_file_path:
            file_path = os.path.abspath(existing_file_path)
            upload_root = os.path.abspath(current_app.config["UPLOAD_FOLDER"])
            if not file_path.startswith(upload_root) or not os.path.exists(file_path):
                flash("The saved workbook could not be found. Please upload it again.", "warning")
                return redirect(url_for("payroll.upload"))
            source_filename = source_filename or os.path.basename(file_path)
        else:
            file_storage = request.files.get("payroll_file")
            if not file_storage or not file_storage.filename:
                flash("Choose an Excel file to upload.", "warning")
                return redirect(url_for("payroll.upload"))
            if not allowed_excel_file(file_storage.filename):
                flash("Only .xlsx, .xls, or .csv files are supported.", "warning")
                return redirect(url_for("payroll.upload"))
            file_path, source_filename = save_uploaded_file(
                file_storage, current_app.config["UPLOAD_FOLDER"]
            )

        sheet_names = workbook_sheet_names(file_path)
        candidates = payroll_sheet_candidates(file_path)
        current_app.logger.info(
            "Smart Excel Import Engine: workbook sheets=%s selected_client=%s candidates=%s",
            sheet_names,
            client.name,
            candidates,
        )
        if not candidates:
            flash(
                "No valid payroll rows were found. Please check the selected sheet, header row, or Excel format.",
                "danger",
            )
            return redirect(url_for("payroll.upload"))

        matched_sheet_name = selected_sheet_name or match_client_sheet(
            client.name, [candidate["sheet_name"] for candidate in candidates]
        )
        if not matched_sheet_name and len(candidates) == 1:
            matched_sheet_name = candidates[0]["sheet_name"]
        if not matched_sheet_name:
            flash(
                "No matching sheet was found for the selected client. Choose the correct payroll sheet below.",
                "warning",
            )
            return render_template(
                "payroll_upload.html",
                clients=clients,
                current_month=month,
                current_year=year,
                selected_client_id=int(client_id),
                pending_file_path=file_path,
                pending_source_filename=source_filename,
                available_sheets=candidates,
            )

        extraction = extract_payroll_sheet(file_path, matched_sheet_name)
        mapped_rows = extraction["mapped_rows"]
        if not mapped_rows:
            current_app.logger.warning(
                "Smart Excel Import Engine extracted 0 rows from sheet=%s file=%s",
                matched_sheet_name,
                source_filename,
            )
            flash(
                "No valid payroll rows were found. Please check the selected sheet, header row, or Excel format.",
                "danger",
            )
            return redirect(url_for("payroll.upload"))

        detected_company_name = detect_company_name(file_path, known_names, matched_sheet_name)
        worker_stats = extraction["worker_stats"]
        validation = validate_payroll_rows(
            mapped_rows, client, month, year, detected_company_name
        )
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

        batch = ImportBatch(
            client_company_id=client.id,
            payroll_month=month,
            payroll_year=year,
            uploaded_by=current_user.id,
            original_filename=source_filename,
            status="Previewed",
            total_rows=import_summary["total_rows"],
            valid_rows=import_summary["valid_rows"],
            invalid_rows=import_summary["invalid_rows"],
            total_workers=worker_stats["total_unique_workers"],
            gross_total=import_summary["gross_total"],
            net_total=import_summary["net_total"],
            paye_total=import_summary["paye_total"],
            ssnit_total=import_summary["ssnit_total"],
            validation_summary="\n".join(validation["summary_warnings"]),
        )
        db.session.add(batch)
        db.session.flush()
        record_audit("Payroll upload", batch, f"Uploaded {source_filename} for preview.")
        db.session.commit()

        payload = {
            "import_batch_id": batch.id,
            "file_path": file_path,
            "source_filename": source_filename,
            "matched_sheet_name": matched_sheet_name,
            "detected_header_row": extraction["detected_header_row"],
            "client_company_id": client.id,
            "month": month,
            "year": year,
            "columns": extraction["columns"],
            "mapping": extraction["mapping"],
            "preview_rows": extraction["preview_rows"],
            "mapped_rows": mapped_rows,
            "worker_stats": worker_stats,
            "status_breakdown": extraction["status_breakdown"],
            "import_summary": import_summary,
            "detected_company_name": detected_company_name,
            "validation": validation,
        }
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
        import_mode="single_client",
        import_type="Single Company Upload",
        detected_company_name=payload.get("detected_company_name"),
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
            ssnit_number=row.get("ssnit_number"),
            basic_salary=float(row.get("basic_salary") or 0),
            transport_allowance=float(row.get("transport_allowance") or 0),
            housing_allowance=float(row.get("housing_allowance") or 0),
            overtime_pay=float(row.get("overtime_pay") or 0),
            other_allowances=float(row.get("other_allowances") or 0),
            gross_pay=float(row.get("gross_pay") or 0),
            paye=float(row.get("paye") or 0),
            ssnit=float(row.get("ssnit") or 0),
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
