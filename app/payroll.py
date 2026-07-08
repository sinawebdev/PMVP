import json
import os
import tempfile
import uuid
from datetime import datetime, timezone

import pandas as pd

from flask import (
    Blueprint,
    current_app,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    session,
    url_for,
)
from flask_login import current_user, login_required

from app import db
from app.audit import record_audit
from app.auth import role_required
from app.excel_utils import (
    allowed_excel_file,
    calculate_status_breakdown,
    calculate_worker_stats,
    detect_company_name,
    extract_payroll_sheet,
    export_bank_listing,
    export_gra_paye_schedule,
    export_payroll_run,
    export_wages_sheet,
    export_import_error_report,
    match_client_sheet,
    normalize_company_key,
    payroll_sheet_candidates,
    workbook_sheet_names,
)
from app.models import (
    DELIVERY_SENT,
    ClientCompany,
    Employee,
    Expense,
    ImportBatch,
    PayrollItem,
    PayrollRun,
    PayslipDelivery,
    RawPayEntry,
    Remittance,
    WageRateProfile,
)
from app.payroll_status import (
    APPROVED,
    DRAFT,
    PENDING_APPROVAL,
    PENDING_STATUSES,
    PROCESSED,
    REJECTED,
)
from app.pdf_service import generate_payslip_pdf
from app.raw_import import normalise_emp_id
from app.validators import collect_blocking_errors, validate_payroll_rows

payroll_bp = Blueprint("payroll", __name__, url_prefix="/payroll")


def crossref_employee_records(client_id, mapped_rows):
    """Compare a payroll file's staff IDs against the client's active employee roster.

    Returns ``(unregistered, no_contact)`` — both non-blocking warnings:
      * unregistered: normalised staff IDs in the file with no roster record yet.
        They import fine but cannot receive payslips until their record is created.
      * no_contact: active roster employees present in the file that have neither
        email nor phone, so distribution is unavailable for them.
    Contact details for distribution always come from the roster, never the upload.
    """
    file_ids = {
        normalise_emp_id(row.get("staff_id"))
        for row in mapped_rows
        if row.get("staff_id")
    }
    file_ids.discard("")
    db_employees = {
        e.staff_id: e
        for e in Employee.query.filter_by(
            client_company_id=client_id, status="Active"
        ).all()
    }
    unregistered = sorted(file_ids - set(db_employees.keys()))
    no_contact = sorted(
        (
            {"staff_id": e.staff_id, "name": e.full_name}
            for e in db_employees.values()
            if e.staff_id in file_ids and not e.email and not e.phone
        ),
        key=lambda r: r["name"] or r["staff_id"],
    )
    return unregistered, no_contact


def save_import_session(payload):
    batch = db.session.get(ImportBatch, int(payload["import_batch_id"]))
    batch.payload_json = json.dumps(payload, indent=2)
    db.session.commit()
    return str(batch.id)


def parse_import_id_or_404(import_id):
    """The <import_id> URL segment as an int, or a 404 — a non-numeric id is
    a bad URL, not a server error (bare int() raised ValueError -> 500)."""
    from flask import abort

    try:
        return int(import_id)
    except (TypeError, ValueError):
        abort(404)


def load_import_session(import_id):
    batch = db.get_or_404(ImportBatch, parse_import_id_or_404(import_id))
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


def replace_existing_runs(client_id, month, year):
    """Hard-delete every existing run for (client, month, year) ahead of a
    confirmed replacement import. All-or-nothing: if any existing run is not
    deletable, nothing is deleted and the reason is returned. Does not commit
    (the surrounding confirm flow owns the transaction, so a failed import
    also rolls the deletions back)."""
    existing_runs = PayrollRun.query.filter_by(
        client_company_id=client_id, month=month, year=int(year)
    ).all()
    for run in existing_runs:
        blockers = payroll_run_delete_blockers(run)
        if blockers:
            return False, f"run #{run.id} ({run.status}) is not deletable: " + "; ".join(blockers)
    for run in existing_runs:
        ok, reason = hard_delete_payroll_run(run)
        if not ok:  # pragma: no cover - blockers already checked above
            return False, reason
    return True, None


def save_temporary_upload(file_storage):
    """Render files are ephemeral; uploaded workbooks are saved only long enough to parse."""
    suffix = os.path.splitext(file_storage.filename or "")[1]
    handle = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
    handle.close()
    file_storage.save(handle.name)
    return handle.name


def match_client_for_sheet(sheet_name, clients):
    for client in clients:
        if match_client_sheet(client.name, [sheet_name]):
            return client
    return None


def is_consolidated_sheet(sheet_name):
    normalized = normalize_company_key(sheet_name, strip_suffix=False)
    return normalized.startswith("consolidated") or normalized.startswith("mixed")


def build_run_payload_from_extraction(
    extraction,
    mapped_rows,
    file_path,
    source_filename,
    client,
    month,
    year,
    known_names,
    detected_company_name=None,
):
    detected_company_name = detected_company_name or detect_company_name(
        file_path, known_names, extraction["sheet_name"]
    )
    validation = validate_payroll_rows(mapped_rows, client, month, year, detected_company_name)
    import_summary = summarize_import(mapped_rows, validation)
    worker_stats = calculate_worker_stats(mapped_rows)
    status_breakdown = calculate_status_breakdown(mapped_rows)
    return {
        "mode": "single_client",
        "source_filename": os.path.basename(source_filename),
        "matched_sheet_name": extraction["sheet_name"],
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
        "worker_stats": worker_stats,
        "status_breakdown": status_breakdown,
        "import_summary": import_summary,
        "detected_company_name": detected_company_name,
        "validation": validation,
    }


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
        return None, f"No valid worker rows extracted from sheet '{matched_sheet_name}'. Check that the header row contains recognizable payroll column names."

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
        if is_consolidated_sheet(candidate["sheet_name"]):
            extraction = extract_payroll_sheet(file_path, candidate["sheet_name"])
            if not extraction["mapped_rows"]:
                return None, f"No valid worker rows extracted from sheet '{candidate['sheet_name']}'. Check that the header row contains recognizable payroll column names."

            grouped_rows = {}
            for row in extraction["mapped_rows"]:
                group_name = str(row.get("client_company") or "").strip()
                if group_name:
                    grouped_rows.setdefault(group_name, []).append(row)

            if not grouped_rows:
                unmatched_sheets.append(
                    {
                        "sheet_name": candidate["sheet_name"],
                        "reason": "Consolidated sheet has no Client or Company column.",
                    }
                )
                continue

            for group_name, group_rows in grouped_rows.items():
                client = match_client_for_sheet(group_name, clients)
                if not client:
                    unmatched_sheets.append(
                        {
                            "sheet_name": f"{candidate['sheet_name']} - {group_name}",
                            "reason": "Client group could not be matched.",
                        }
                    )
                    continue
                runs.append(
                    build_run_payload_from_extraction(
                        extraction,
                        group_rows,
                        file_path,
                        source_filename,
                        client,
                        month,
                        year,
                        known_names,
                        detected_company_name=group_name,
                    )
                )
            continue

        client = match_client_for_sheet(candidate["sheet_name"], clients)
        if not client:
            unmatched_sheets.append(candidate)
            continue
        extraction = extract_payroll_sheet(file_path, candidate["sheet_name"])
        if not extraction["mapped_rows"]:
            return None, f"No valid worker rows extracted from sheet '{candidate['sheet_name']}'. Check that the header row contains recognizable payroll column names."
        runs.append(
            build_run_payload_from_extraction(
                extraction,
                extraction["mapped_rows"],
                file_path,
                source_filename,
                client,
                month,
                year,
                known_names,
            )
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


def handle_payroll_upload(now):
    import_mode = request.form.get("import_mode") or "single_client"
    client_id = request.form.get("client_company_id")
    if import_mode == "single_client" and not client_id:
        flash("Select a client company before uploading payroll.", "warning")
        return redirect(url_for("payroll.runs"))

    month = request.form.get("month") or now.strftime("%B")
    year = int(request.form.get("year") or now.year)
    file_storage = request.files.get("payroll_file")
    if not file_storage or not file_storage.filename:
        flash("Choose an Excel file to upload.", "warning")
        return redirect(url_for("payroll.runs"))
    if not allowed_excel_file(file_storage.filename):
        flash("Only .xlsx, .xls, or .csv files are supported.", "warning")
        return redirect(url_for("payroll.runs"))

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
        return redirect(url_for("payroll.runs"))

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


@payroll_bp.route("/runs", methods=["GET", "POST"])
@login_required
def runs():
    now = datetime.now()
    if request.method == "POST":
        if current_user.role != "admin":
            flash("Only admins can create payroll runs.", "danger")
            return redirect(url_for("payroll.runs"))
        return handle_payroll_upload(now)

    selected_client = None
    query = PayrollRun.query
    client_id = request.args.get("client_id")
    status_filter = request.args.get("status", "")
    if client_id:
        selected_client = db.get_or_404(ClientCompany, client_id)
        query = query.filter(PayrollRun.client_company_id == selected_client.id)
    if status_filter == "needs_approval":
        query = query.filter(PayrollRun.status.in_(PENDING_STATUSES))
    elif status_filter:
        query = query.filter(PayrollRun.status == status_filter)
    payroll_runs = query.order_by(PayrollRun.created_at.desc()).all()
    clients = ClientCompany.query.filter_by(status="Active").order_by(ClientCompany.name).all()
    return render_template(
        "payroll_runs.html",
        payroll_runs=payroll_runs,
        selected_client=selected_client,
        status_filter=status_filter,
        clients=clients,
        current_month=now.strftime("%B"),
        current_year=now.year,
    )


@payroll_bp.route("/preview/<import_id>")
@role_required("admin")
def preview(import_id):
    payload = load_import_session(import_id)
    client = None
    unregistered, no_contact = [], []
    if payload.get("mode") != "multi_client":
        client = db.get_or_404(ClientCompany, payload["client_company_id"])
        unregistered, no_contact = crossref_employee_records(
            client.id, payload.get("mapped_rows", [])
        )
    return render_template(
        "payroll_preview.html",
        import_id=import_id,
        payload=payload,
        client=client,
        unregistered=unregistered,
        no_contact=no_contact,
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
        return redirect(url_for("payroll.runs"))
    client = db.get_or_404(ClientCompany, payload["client_company_id"])
    # Spec §8 hard-stops: a corrupted upload (wrong header row, zero-basic
    # active workers) is refused outright — warn-and-proceed is what let
    # production run 9 persist net > gross rows. Cheap, no DB round trips.
    blocking = collect_blocking_errors(
        payload["mapped_rows"], payload.get("detected_company_name", "")
    )
    if blocking:
        for error in blocking:
            flash(error, "danger")
        flash("Import blocked — no payroll run was created.", "danger")
        return redirect(url_for("payroll.preview", import_id=import_id))
    # Reuse the validation computed at upload time (stored in the payload)
    # instead of re-running the whole row sweep — the rows can't have changed
    # between preview and confirm, and the recompute doubled the confirm
    # request's query load. Recompute only for payloads persisted before
    # validation was stored.
    validation = payload.get("validation") or validate_payroll_rows(
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
    if duplicate_exists:
        # "Replace" must actually replace: previously this checkbox only
        # suppressed the warning, leaving two live runs for the same
        # client/month double-counting on the dashboard and in the YTD bonus
        # cap. Hard-delete the old run(s) first, subject to the same
        # restrictions as manual deletion — if the old run doesn't qualify
        # (approved, voucher, sent payslips...), refuse loudly instead of
        # silently importing a duplicate.
        ok, reason = replace_existing_runs(client.id, payload["month"], payload["year"])
        if not ok:
            flash(f"Cannot replace the existing payroll: {reason}.", "danger")
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
        return redirect(url_for("payroll.runs"))

    # Same §8 hard-stops as the single-client confirm, checked across every
    # sheet BEFORE anything is created — one corrupted sheet blocks the whole
    # multi-client import rather than leaving a partial batch.
    blocking = []
    for run_payload in runs:
        for error in collect_blocking_errors(
            run_payload["mapped_rows"], run_payload.get("detected_company_name", "")
        ):
            blocking.append(f"{run_payload.get('client_company_name', 'Sheet')}: {error}")
    if blocking:
        for error in blocking:
            flash(error, "danger")
        flash("Import blocked — no payroll runs were created.", "danger")
        return redirect(url_for("payroll.preview", import_id=import_id))

    duplicates = []
    for run_payload in runs:
        if has_duplicate_payroll(run_payload["client_company_id"], run_payload["month"], run_payload["year"]):
            duplicates.append(run_payload)
    if duplicates and request.form.get("replace_existing") != "1":
        duplicate_names = ", ".join(d["client_company_name"] for d in duplicates)
        flash(
            f"Duplicate payroll found for: {duplicate_names}. Tick confirm replacement before importing again.",
            "warning",
        )
        return redirect(url_for("payroll.preview", import_id=import_id))
    for run_payload in duplicates:
        # Replacement means the old run really goes away (see the single-client
        # confirm). Refuse the whole import if any old run isn't deletable.
        ok, reason = replace_existing_runs(
            run_payload["client_company_id"], run_payload["month"], run_payload["year"]
        )
        if not ok:
            db.session.rollback()
            flash(
                f"Cannot replace the existing payroll for {run_payload['client_company_name']}: {reason}.",
                "danger",
            )
            return redirect(url_for("payroll.preview", import_id=import_id))

    created_runs = []
    for run_payload in runs:
        client = db.get_or_404(ClientCompany, run_payload["client_company_id"])
        # Same reuse as the single-client confirm: the per-run validation was
        # computed and stored at upload time.
        validation = run_payload.get("validation") or validate_payroll_rows(
            run_payload["mapped_rows"],
            client,
            run_payload["month"],
            run_payload["year"],
            run_payload.get("detected_company_name", ""),
        )
        payroll_run = create_payroll_run_from_payload(run_payload, client, validation, "multi_client")
        created_runs.append(payroll_run)
        record_audit("Payroll import confirmed", payroll_run, f"Created multi-client payroll run from {run_payload['source_filename']}.")

    batch = db.session.get(ImportBatch, parse_import_id_or_404(import_id))
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
        status=DRAFT,
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
    # The client's whole roster in ONE query. The previous per-row
    # Employee.query + db.session.flush() pair was 2 round trips per row and
    # a large share of why big confirms hit the worker timeout. New employees
    # created during this import are added to the map so duplicate staff IDs
    # within one file reuse the same record instead of re-querying.
    # Keyed by the NORMALISED staff id ("DCL 9" == "DCL9") so an upload with
    # spacing/case variants resolves to the roster record instead of forking
    # a duplicate Employee.
    employees_by_staff_id = {
        normalise_emp_id(e.staff_id): e
        for e in Employee.query.filter_by(client_company_id=client.id).all()
    }
    for index, row in enumerate(payload["mapped_rows"], start=1):
        if not str(row.get("staff_id") or "").strip() and not str(row.get("full_name") or "").strip():
            continue

        employee = create_or_update_employee_from_import(
            row, client, payroll_run, index, employees_by_staff_id
        )

        warnings = validation["per_row_warnings"].get(str(index)) or validation[
            "per_row_warnings"
        ].get(index, [])
        item = PayrollItem(
            payroll_run_id=payroll_run.id,
            employee=employee,
            staff_id=row.get("staff_id"),
            full_name=row.get("full_name"),
            status=row.get("status"),
            service_line=row.get("service_line"),
            job_role=row.get("job_role"),
            payroll_month=row.get("payroll_month"),
            ssnit_number=row.get("ssnit_number"),
            ghana_card_number=row.get("ghana_card_number"),
            bank_name=row.get("bank_name"),
            bank_branch=row.get("bank_branch"),
            bank_account_number=row.get("bank_account_number"),
            momo_number=row.get("momo_number"),
            email=row.get("email"),
            basic_salary=float(row.get("basic_salary") or 0),
            transport_allowance=float(row.get("transport_allowance") or 0),
            housing_allowance=float(row.get("housing_allowance") or 0),
            medical_allowance=float(row.get("medical_allowance") or 0),
            meal_allowance=float(row.get("meal_allowance") or 0),
            productivity_bonus=float(row.get("productivity_bonus") or 0),
            end_of_year_bonus=float(row.get("end_of_year_bonus") or 0),
            overtime_hours=float(row.get("overtime_hours") or 0),
            overtime_pay=float(row.get("overtime_pay") or 0),
            # Imported overtime is a hand-keyed lump sum (the ACS model);
            # 'computed' is reserved for the raw hours x rate path.
            overtime_source="manual",
            other_allowances=float(row.get("other_allowances") or 0),
            pay_difference=float(row.get("pay_difference") or 0),
            gross_pay=float(row.get("gross_pay") or 0),
            paye=float(row.get("paye") or 0),
            ssnit=float(row.get("ssnit") or 0),
            tier_2_pension=float(row.get("tier_2_pension") or 0),
            pf_fund_employee=float(row.get("pf_fund_employee") or 0),
            loan_deduction=float(row.get("loan_deduction") or 0),
            loan_advance=float(row.get("loan_advance") or 0),
            welfare_deduction=float(row.get("welfare_deduction") or 0),
            iou_deduction=float(row.get("iou_deduction") or 0),
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

    # Statutory figures are computed the moment the import is confirmed — any
    # SSF/PAYE numbers from the uploaded file are preview-only and never
    # survive as the official values. The manual "Calculate Pay" button stays
    # for recomputing after grid edits.
    auto_calculate_on_confirm(payroll_run)
    return payroll_run


def auto_calculate_on_confirm(payroll_run):
    """Run the salaried statutory calculation on a freshly confirmed run.

    If no statutory rate version covers the run's period the run is left in
    Draft with the uploaded figures and a visible warning — the import itself
    must not fail, but the numbers are not trustworthy until Calculate Pay
    succeeds."""
    from app.payroll_calculations import statutory_rate_for_run

    db.session.flush()
    try:
        statutory_rate = statutory_rate_for_run(payroll_run)
    except (LookupError, ValueError) as exc:
        record_audit(
            "Payroll auto-calculation skipped",
            payroll_run,
            f"Imported figures kept unverified: {exc}",
        )
        flash(
            f"Payroll imported but statutory figures could NOT be computed: {exc}",
            "warning",
        )
        return

    # The math itself (recalculate_salaried_items) used to run unprotected —
    # any exception there (bad row data, an edge case in the calculator)
    # propagated as a raw 500 with zero diagnostic trail: no Postgres error,
    # nothing in the audit log, just "Internal Server Error." Catch broadly
    # here so a bad row degrades the run (kept in Draft, uploaded figures
    # intact, visible warning) instead of taking down the whole request —
    # matching the "import itself must not fail" contract above.
    try:
        recalculate_salaried_items(payroll_run, statutory_rate)
        db.session.flush()
        refresh_run_totals(payroll_run)
    except Exception as exc:
        # No rollback here: the PayrollRun/PayrollItem rows from
        # create_payroll_run_from_payload are already flushed in this same
        # (uncommitted) transaction. A rollback would silently discard the
        # entire import, contradicting "uploaded figures were kept" below.
        # Items processed before the failure keep their recalculated values;
        # items from the failure point on keep their raw uploaded figures —
        # an inconsistent but non-destructive partial state, flagged by the
        # warning and fixable via Calculate Pay once the underlying issue
        # (see traceback) is resolved.
        current_app.logger.exception(
            "auto_calculate_on_confirm: calculation failed for run %s", payroll_run.id
        )
        record_audit(
            "Payroll auto-calculation failed",
            payroll_run,
            f"{type(exc).__name__}: {exc}",
        )
        flash(
            f"Payroll imported but statutory calculation failed ({type(exc).__name__}: {exc}). "
            "Uploaded figures were kept (some items may be partially recalculated); check "
            "Render logs for the full traceback, fix the underlying data/rate issue, then "
            "use Calculate Pay to retry.",
            "danger",
        )
        return

    record_audit(
        "Payroll auto-calculated on import",
        payroll_run,
        f"Statutory SSF/PAYE computed for {len(payroll_run.items)} workers on "
        f"confirm. Statutory rates effective {statutory_rate.effective_from.isoformat()}.",
    )


def recalculate_salaried_items(payroll_run, statutory_rate):
    """Recompute every item's statutory figures from its raw earnings inputs."""
    from app.payroll_calculations import bonus_concession_used_ytd_bulk
    from app.payroll_calculations.salaried import SalariedCalculator

    calculator = SalariedCalculator(statutory_rate)
    # Annual bonus concession cap, enforced once per tax year: subtract
    # whatever concession each employee's OTHER runs already used. Fetched
    # for the whole run in ONE query — the per-item version was a join query
    # per worker and helped push large confirms past the worker timeout.
    try:
        used_ytd_by_employee = bonus_concession_used_ytd_bulk(
            [item.employee_id for item in payroll_run.items],
            payroll_run.year,
            exclude_run_id=payroll_run.id,
        )
    except Exception as exc:
        raise RuntimeError(
            f"bonus_concession_used_ytd_bulk failed for run {payroll_run.id}: {exc}"
        ) from exc
    # Tax relief comes off the roster record; loading it via item.employee
    # inside the loop would be one lazy-load query per item, so prefetch the
    # run's employees in one query instead.
    employee_ids = [item.employee_id for item in payroll_run.items if item.employee_id]
    relief_by_employee = (
        {
            e.id: (e.tax_relief_monthly or 0)
            for e in Employee.query.filter(Employee.id.in_(set(employee_ids))).all()
        }
        if employee_ids
        else {}
    )
    for item in payroll_run.items:
        used_ytd = used_ytd_by_employee.get(item.employee_id, 0.0)
        tax_relief = relief_by_employee.get(item.employee_id, 0)
        try:
            result = _calculate_item(calculator, item, used_ytd, tax_relief)
        except Exception as exc:
            raise RuntimeError(
                f"SalariedCalculator.calculate failed for staff_id={item.staff_id!r} "
                f"(item id={item.id}, employee_id={item.employee_id}): {exc}"
            ) from exc
        for field, value in result.as_payroll_item_fields().items():
            setattr(item, field, value)
        apply_overtime_concession_warning(item, statutory_rate)
    verify_statutory_invariants(payroll_run, statutory_rate)


# Substring marker that identifies OUR junior-staff note inside warning_notes,
# so recalculation can replace a stale copy instead of stacking duplicates.
_JUNIOR_OT_MARKER = "junior-staff qualifying threshold"


def apply_overtime_concession_warning(item, statutory_rate):
    """Spec §7.1: the 5%/10% overtime concession legally applies only to a
    qualifying junior employee (qualifying income <= the monthly threshold on
    the rate version, GHS 1,500 under GRA 2026 rules). We match the client
    sheet and apply it to everyone, but flag every overtime earner above the
    threshold so the exposure is visible and the gate can be tightened later."""
    existing = [
        note.strip()
        for note in (item.warning_notes or "").split(";")
        if note.strip() and _JUNIOR_OT_MARKER not in note
    ]
    threshold = float(statutory_rate.overtime_junior_monthly_threshold or 0)
    if (
        threshold > 0
        and float(item.overtime_pay or 0) > 0
        and float(item.basic_salary or 0) > threshold
    ):
        existing.append(
            "Overtime taxed at the concessionary flat rates, but basic salary "
            f"(GHS {item.basic_salary:,.2f}) is above the GRA "
            f"{_JUNIOR_OT_MARKER} (GHS {threshold:,.2f}/month) — strictly, "
            "this worker's overtime belongs in the marginal PAYE bands."
        )
    item.warning_notes = "; ".join(existing)
    if existing:
        item.validation_status = "Warning"
    elif item.validation_status == "Warning":
        item.validation_status = "OK"


def verify_statutory_invariants(payroll_run, statutory_rate):
    """Spec §8 hard-stops on DERIVED figures. Since net pay and SSNIT are
    computed, a violation can only mean a compute or import-mapping bug — the
    kind that shipped net > gross in production run 9. Raise loudly instead of
    persisting a bad run; callers keep the run in Draft with the failure
    visible."""
    from app.money import D, money

    problems = []
    for item in payroll_run.items:
        gross = float(item.gross_pay or 0)
        net = float(item.net_pay or 0)
        advance = float(item.loan_advance or 0)
        who = item.staff_id or item.full_name or f"item {item.id}"
        # Net can legitimately exceed gross only by the cash advance.
        if net > gross + advance + 0.01:
            problems.append(f"{who}: net {net:.2f} exceeds gross {gross:.2f}")
        expected_ssnit = money(
            D(item.basic_salary or 0) * D(statutory_rate.ssf_employee_rate)
        )
        if abs(float(item.ssnit or 0) - expected_ssnit) > 0.01:
            problems.append(
                f"{who}: SSNIT {float(item.ssnit or 0):.2f} != "
                f"{expected_ssnit:.2f} (basic x {statutory_rate.ssf_employee_rate})"
            )
    if problems:
        shown = "; ".join(problems[:5])
        more = f" (+{len(problems) - 5} more)" if len(problems) > 5 else ""
        raise RuntimeError(f"Statutory invariant violation: {shown}{more}")


def _calculate_item(calculator, item, used_ytd, tax_relief_monthly):
    return calculator.calculate(
            item.basic_salary,
            transport_allowance=item.transport_allowance,
            housing_allowance=item.housing_allowance,
            medical_allowance=item.medical_allowance,
            meal_allowance=item.meal_allowance,
            productivity_bonus=item.productivity_bonus,
            end_of_year_bonus=item.end_of_year_bonus,
            other_allowances=item.other_allowances,
            overtime_pay=item.overtime_pay,
            pay_difference=item.pay_difference,
            pf_fund_employee=item.pf_fund_employee,
            tax_relief_monthly=tax_relief_monthly,
            loan_deduction=item.loan_deduction,
            loan_advance=item.loan_advance,
            welfare_deduction=item.welfare_deduction,
            iou_deduction=item.iou_deduction,
            other_deductions=item.other_deductions,
            bonus_concession_used_ytd=used_ytd,
        )


def refresh_run_totals(payroll_run):
    """Re-derive the run's persisted totals from its items."""
    items = PayrollItem.query.filter_by(payroll_run_id=payroll_run.id).all()
    payroll_run.total_gross_pay = round(sum(i.gross_pay or 0 for i in items), 2)
    payroll_run.total_deductions = round(sum(i.total_deductions or 0 for i in items), 2)
    payroll_run.total_net_pay = round(sum(i.net_pay or 0 for i in items), 2)
    payroll_run.total_paye = round(sum(i.paye or 0 for i in items), 2)
    payroll_run.total_ssnit = round(sum(i.ssnit or 0 for i in items), 2)
    payroll_run.total_ssnit_employer = round(
        sum(i.ssf_employer or 0 for i in items), 2
    )
    payroll_run.total_workers = len(items)


def create_or_update_employee_from_import(
    row, client, payroll_run, row_index, employees_by_staff_id
):
    """``employees_by_staff_id`` is the client's prefetched roster keyed by
    NORMALISED staff id ({normalise_emp_id(staff_id): Employee}); newly
    created employees are inserted into it so later rows (and later imports
    in the same request) reuse them without another query."""
    # Normalise the join key exactly like the roster side
    # (Employee.normalise_staff_id): "DCL 9" -> "DCL9". Without this, an
    # upload with a spacing variant created a second Employee for the same
    # worker, splitting their history and breaking payslip distribution's
    # contact lookup (which normalises before matching).
    staff_id = normalise_emp_id(row.get("staff_id") or "")
    full_name = str(row.get("full_name") or "").strip()
    if not staff_id:
        staff_id = f"IMPORT-{client.id}-{payroll_run.id}-{row_index}"
    if not full_name:
        full_name = f"Imported Worker {staff_id}"

    employee = employees_by_staff_id.get(staff_id)
    if employee is None:
        employee = Employee(
            staff_id=staff_id,
            client_company_id=client.id,
            status="Active",
            employment_type="Imported Payroll Worker",
            service_line="Personnel Outsourcing",
        )
        db.session.add(employee)
        employees_by_staff_id[staff_id] = employee

    employee.full_name = full_name
    employee.ssnit_number = row.get("ssnit_number") or employee.ssnit_number
    employee.ghana_card_number = row.get("ghana_card_number") or employee.ghana_card_number
    employee.bank_name = row.get("bank_name") or employee.bank_name
    employee.bank_branch = row.get("bank_branch") or employee.bank_branch
    employee.bank_account_number = row.get("bank_account_number") or employee.bank_account_number
    employee.momo_number = row.get("momo_number") or employee.momo_number
    employee.email = row.get("email") or employee.email
    employee.status = row.get("status") or employee.status
    employee.service_line = row.get("service_line") or employee.service_line
    employee.basic_salary = float(row.get("basic_salary") or employee.basic_salary or 0)
    employee.assigned_client = client.name
    return employee


@payroll_bp.route("/runs/<int:run_id>")
@login_required
def detail(run_id):
    payroll_run = db.get_or_404(PayrollRun, run_id)

    return render_template(
        "payroll_detail.html",
        payroll_run=payroll_run,
        # Drive the delete button off the same set the backend gate uses, so the
        # two can't silently drift apart again (a hardcoded "Draft" in the
        # template is exactly how the button went missing for Rejected runs).
        deletable_statuses=DELETABLE_STATUSES,
    )


def _raw_import_session_path(run_id):
    """Parsed raw-hours preview is staged on disk (not the cookie session) keyed
    by run, mirroring the standard import's IMPORT_SESSION_FOLDER pattern, so a
    large workbook never overflows the session cookie."""
    folder = current_app.config["IMPORT_SESSION_FOLDER"]
    return os.path.join(folder, f"raw_import_{run_id}.json")


@payroll_bp.route("/runs/<int:run_id>/raw-upload", methods=["POST"])
@role_required("admin")
def raw_upload(run_id):
    """Accept a raw-data Excel file, parse it, cross-validate, and return a JSON
    preview for user confirmation. Does NOT write any payroll data to the DB."""
    from app.raw_import import (
        build_import_preview,
        cross_validate,
        detect_sheet_layout,
        normalise_emp_id,
        parse_master_tab,
        parse_qtarpay,
    )

    run = db.get_or_404(PayrollRun, run_id)

    file = request.files.get("file")
    if not file or not file.filename:
        return jsonify({"error": "No file provided"}), 400

    try:
        xl = pd.ExcelFile(file)
    except Exception:
        return jsonify({"error": "Could not read the uploaded file as an Excel workbook."}), 422

    layout = detect_sheet_layout(xl)
    if "qtarpay" not in layout:
        return jsonify({
            "error": "Could not find a pay-code sheet (qtarpay format) in this file. "
                     "Expected a sheet with Column1/Column2/Column3/Column4 headers."
        }), 422

    df_qtarpay = xl.parse(layout["qtarpay"], header=None)
    employees, warnings = parse_qtarpay(df_qtarpay)

    discrepancies = []
    if "master" in layout:
        df_master = xl.parse(layout["master"], header=None)
        master_data = parse_master_tab(df_master)
        discrepancies = cross_validate(employees, master_data)

    db_emps_raw = Employee.query.filter_by(
        client_company_id=run.client_company_id
    ).all()
    db_employees = {
        normalise_emp_id(e.staff_id): {"name": e.full_name, "id": e.id}
        for e in db_emps_raw
    }

    preview = build_import_preview(employees, db_employees)

    # Stage parsed hours on disk and keep only the run id in the cookie session.
    with open(_raw_import_session_path(run_id), "w", encoding="utf-8") as handle:
        json.dump(employees, handle)
    session["raw_import_run_id"] = run_id

    record_audit("Raw payroll upload", run, "Raw hours uploaded for preview (billable add-on).")

    return jsonify({
        "status": "preview",
        "preview": preview,
        "warnings": warnings,
        "discrepancies": discrepancies,
        "employee_count": len(employees),
    })


@payroll_bp.route("/runs/<int:run_id>/raw-confirm", methods=["POST"])
@role_required("admin")
def raw_confirm(run_id):
    """User confirmed the preview. Commit raw hours and mark the run
    upload_type='raw'. Does NOT calculate pay — that is a later operator step."""
    if session.get("raw_import_run_id") != run_id:
        return jsonify({"error": "Session mismatch — please re-upload the file."}), 400

    session_path = _raw_import_session_path(run_id)
    employees = {}
    if os.path.exists(session_path):
        with open(session_path, "r", encoding="utf-8") as handle:
            employees = json.load(handle)
    if not employees:
        return jsonify({"error": "No import data in session."}), 400

    run = db.get_or_404(PayrollRun, run_id)

    # Replace, never append: a re-import of the same (or a corrected) file for
    # this run must supersede the previous hours. Appending on top doubled
    # every worker's hours — and therefore pay — on the next Calculate.
    RawPayEntry.query.filter_by(payroll_run_id=run_id).delete()

    for emp_id, pay_codes in employees.items():
        for pay_code, hours in pay_codes.items():
            db.session.add(
                RawPayEntry(
                    payroll_run_id=run_id,
                    employee_id_str=emp_id,
                    pay_code=pay_code,
                    hours=hours,
                )
            )

    run.upload_type = "raw"
    record_audit("Raw payroll confirm", run, "Raw hours committed; awaiting pay calculation.")
    db.session.commit()

    session.pop("raw_import_run_id", None)
    try:
        os.remove(session_path)
    except OSError:
        pass

    return jsonify({
        "status": "committed",
        "run_id": run_id,
        "imported": sum(len(v) for v in employees.values()),
    })


def _raw_new_path(token):
    """Disk staging for an upload-page raw import keyed by a one-time token, so a
    large workbook never rides in the cookie session."""
    return os.path.join(current_app.config["IMPORT_SESSION_FOLDER"], f"raw_new_{token}.json")


@payroll_bp.route("/runs/raw-upload", methods=["POST"])
@role_required("admin")
def raw_upload_new():
    """Upload-page raw-data flow: parse a raw-hours workbook for the chosen client
    and return a JSON preview. Creates NO payroll run yet — that happens on confirm,
    so abandoning the preview leaves no orphan Draft run behind."""
    from app.raw_import import (
        build_import_preview,
        cross_validate,
        detect_sheet_layout,
        normalise_emp_id,
        parse_master_tab,
        parse_qtarpay,
    )

    client_id = request.form.get("client_company_id")
    if not client_id:
        return jsonify({"error": "Select a client company first."}), 400
    client = db.session.get(ClientCompany, int(client_id))
    if not client:
        return jsonify({"error": "Unknown client company."}), 404
    month = request.form.get("month")
    year = request.form.get("year")
    if not month or not year:
        return jsonify({"error": "Choose a month and year."}), 400

    file = request.files.get("file")
    if not file or not file.filename:
        return jsonify({"error": "No file provided"}), 400
    try:
        xl = pd.ExcelFile(file)
    except Exception:
        return jsonify({"error": "Could not read the uploaded file as an Excel workbook."}), 422

    layout = detect_sheet_layout(xl)
    if "qtarpay" not in layout:
        return jsonify({
            "error": "Could not find a pay-code sheet (qtarpay format) in this file. "
                     "Expected a sheet with Column1/Column2/Column3/Column4 headers."
        }), 422

    employees, warnings = parse_qtarpay(xl.parse(layout["qtarpay"], header=None))
    discrepancies = []
    if "master" in layout:
        master_data = parse_master_tab(xl.parse(layout["master"], header=None))
        discrepancies = cross_validate(employees, master_data)

    db_employees = {
        normalise_emp_id(e.staff_id): {"name": e.full_name, "id": e.id}
        for e in Employee.query.filter_by(client_company_id=client.id).all()
    }
    preview = build_import_preview(employees, db_employees)

    token = uuid.uuid4().hex
    with open(_raw_new_path(token), "w", encoding="utf-8") as handle:
        json.dump(
            {
                "client_company_id": client.id,
                "month": month,
                "year": int(year),
                "source_filename": file.filename,
                "employees": employees,
            },
            handle,
        )
    session["raw_new_token"] = token

    return jsonify({
        "status": "preview",
        "token": token,
        "preview": preview,
        "warnings": warnings,
        "discrepancies": discrepancies,
        "employee_count": len(employees),
        "client_name": client.name,
        "period": f"{month} {year}",
    })


@payroll_bp.route("/runs/raw-confirm", methods=["POST"])
@role_required("admin")
def raw_confirm_new():
    """Commit a previewed upload-page raw import: create a Draft payroll run marked
    upload_type='raw', store the raw hours, and hand back the run detail URL. Pay is
    calculated later by a Chrisnat operator — this stores hours only."""
    token = request.form.get("token")
    if not token or session.get("raw_new_token") != token:
        return jsonify({"error": "Session mismatch — please re-upload the file."}), 400

    path = _raw_new_path(token)
    staged = {}
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as handle:
            staged = json.load(handle)
    employees = staged.get("employees", {})
    if not employees:
        return jsonify({"error": "No import data in session."}), 400

    run = PayrollRun(
        client_company_id=staged["client_company_id"],
        month=staged["month"],
        year=int(staged["year"]),
        status=DRAFT,
        upload_type="raw",
        created_by=current_user.id,
        source_filename=staged.get("source_filename"),
        import_type="Raw Data Upload",
        total_rows_imported=sum(len(v) for v in employees.values()),
        total_workers=len(employees),
        total_unique_workers=len(employees),
    )
    db.session.add(run)
    db.session.flush()  # assign run.id before writing child rows

    for emp_id, pay_codes in employees.items():
        for pay_code, hours in pay_codes.items():
            db.session.add(
                RawPayEntry(
                    payroll_run_id=run.id,
                    employee_id_str=emp_id,
                    pay_code=pay_code,
                    hours=hours,
                )
            )

    record_audit("Raw payroll upload", run, "Raw hours imported (billable add-on); awaiting pay calculation.")
    db.session.commit()

    session.pop("raw_new_token", None)
    try:
        os.remove(path)
    except OSError:
        pass

    return jsonify({
        "status": "committed",
        "run_id": run.id,
        "imported": sum(len(v) for v in employees.values()),
        "redirect": url_for("payroll.detail", run_id=run.id),
    })


@payroll_bp.route("/runs/<int:run_id>/calculate", methods=["POST"])
@role_required("admin")
def calculate(run_id):
    """Compute statutory pay for a run in code — no rep ever types a tax or
    SSF figure. Dispatches on upload_type: 'raw' builds PayrollItems from the
    imported hours × WageRateProfile rates; otherwise each existing item's
    SSNIT/PAYE/net is recomputed from its earnings columns. Uses the
    StatutoryRate version in force for the run's period."""
    from app.payroll_calculations import statutory_rate_for_run
    from app.payroll_calculations.hourly import HourlyShiftCalculator

    payroll_run = db.get_or_404(PayrollRun, run_id)
    if payroll_run.status not in PENDING_STATUSES:
        flash("Only Draft or Pending Approval runs can be recalculated.", "warning")
        return redirect(url_for("payroll.detail", run_id=run_id))
    try:
        statutory_rate = statutory_rate_for_run(payroll_run)
    except (LookupError, ValueError) as exc:
        flash(str(exc), "danger")
        return redirect(url_for("payroll.detail", run_id=run_id))

    if payroll_run.upload_type == "raw":
        calculator = HourlyShiftCalculator(payroll_run, statutory_rate)
        results = calculator.calculate_run()
        missing = sorted(
            {code for r in results.values() for code in r.missing_rate_codes}
        )
        if missing:
            flash(
                "No wage rate configured for pay code(s): "
                f"{', '.join(missing)}. Add them under Wage Rates, then recalculate.",
                "danger",
            )
            return redirect(url_for("payroll.detail", run_id=run_id))

        PayrollItem.query.filter_by(payroll_run_id=payroll_run.id).delete()
        employees_by_id = {
            e.id: e
            for e in Employee.query.filter_by(
                client_company_id=payroll_run.client_company_id
            ).all()
        }
        for emp_key, result in results.items():
            employee = employees_by_id.get(result.employee_id)
            item = PayrollItem(
                payroll_run_id=payroll_run.id,
                employee_id=result.employee_id,
                staff_id=emp_key,
                full_name=employee.full_name if employee else emp_key,
                status=employee.status if employee else None,
                ssnit_number=employee.ssnit_number if employee else None,
                ghana_card_number=employee.ghana_card_number if employee else None,
                bank_name=employee.bank_name if employee else None,
                bank_branch=employee.bank_branch if employee else None,
                bank_account_number=employee.bank_account_number if employee else None,
                momo_number=employee.momo_number if employee else None,
                email=employee.email if employee else None,
                payroll_month=f"{payroll_run.month} {payroll_run.year}",
                # Raw-hours overtime IS hours x rate — the computed path
                # of the hybrid overtime model.
                overtime_source="computed",
                validation_status="OK" if result.employee_id else "Warning",
                warning_notes=(
                    "" if result.employee_id else "No matching roster employee."
                ),
                **result.as_payroll_item_fields(),
            )
            apply_overtime_concession_warning(item, statutory_rate)
            db.session.add(item)
        summary = f"Calculated {len(results)} workers from imported hours."
    else:
        try:
            recalculate_salaried_items(payroll_run, statutory_rate)
        except RuntimeError as exc:
            # Invariant violation (§8 hard-stop) or a row the calculator
            # rejected: keep the stored figures untouched and surface the
            # reason instead of a 500.
            db.session.rollback()
            flash(f"Calculation blocked: {exc}", "danger")
            return redirect(url_for("payroll.detail", run_id=run_id))
        summary = f"Recalculated statutory pay for {len(payroll_run.items)} workers."

    db.session.flush()
    refresh_run_totals(payroll_run)

    record_audit(
        "Payroll calculated",
        payroll_run,
        f"{summary} Statutory rates effective {statutory_rate.effective_from.isoformat()}.",
    )
    db.session.commit()
    flash(summary, "success")
    return redirect(url_for("payroll.detail", run_id=run_id))


# Raw-input fields a rep may edit in the grid. Computed figures (gross_pay,
# paye, ssnit, total_deductions, net_pay) are NEVER writable here — they only
# ever come from the calculator, so the grid cannot bypass the statutory math.
EDITABLE_ITEM_FIELDS = (
    "basic_salary",
    "transport_allowance",
    "medical_allowance",
    "meal_allowance",
    "productivity_bonus",
    "end_of_year_bonus",
    "overtime_pay",
    "pay_difference",
    "pf_fund_employee",
    "loan_deduction",
    "loan_advance",
    "welfare_deduction",
    "iou_deduction",
    "other_deductions",
)


@payroll_bp.route("/runs/<int:run_id>/items/edit", methods=["GET", "POST"])
@role_required("admin", "payroll_officer")
def edit_items(run_id):
    """Grid editing of a run's raw input figures without a full re-upload.

    Editable only while the run is Draft — read-only afterwards, so the
    approval flow cannot be bypassed through a back door. Saving does NOT
    recalculate: 'Calculate Pay' stays a separate, separately-audited action
    tied to the statutory rate version, and a rep can batch several cell
    corrections before recomputing once. Every changed field is audit-logged
    with its old and new value.
    """
    payroll_run = db.get_or_404(PayrollRun, run_id)
    editable = payroll_run.status == DRAFT
    is_raw = payroll_run.upload_type == "raw"

    if request.method == "POST":
        if not editable:
            flash("This run is no longer in Draft — figures are read-only.", "warning")
            return redirect(url_for("payroll.edit_items", run_id=run_id))

        changes = 0
        rejected = 0
        if is_raw:
            entries = RawPayEntry.query.filter_by(payroll_run_id=run_id).all()
            for entry in entries:
                raw_value = request.form.get(f"entry-{entry.id}-hours")
                if raw_value is None or not str(raw_value).strip():
                    continue
                try:
                    new_value = float(raw_value)
                except ValueError:
                    rejected += 1
                    continue
                if new_value < 0:
                    rejected += 1
                    continue
                old_value = float(entry.hours or 0)
                if abs(new_value - old_value) < 0.005:
                    continue
                entry.hours = new_value
                record_audit(
                    "Payroll figures edited",
                    payroll_run,
                    f"Raw hours {entry.employee_id_str}/{entry.pay_code}: "
                    f"{old_value:g} -> {new_value:g}",
                )
                changes += 1
        else:
            for item in payroll_run.items:
                for field in EDITABLE_ITEM_FIELDS:
                    raw_value = request.form.get(f"item-{item.id}-{field}")
                    if raw_value is None or not str(raw_value).strip():
                        continue
                    try:
                        new_value = float(raw_value)
                    except ValueError:
                        rejected += 1
                        continue
                    if new_value < 0:
                        rejected += 1
                        continue
                    old_value = float(getattr(item, field) or 0)
                    if abs(new_value - old_value) < 0.005:
                        continue
                    setattr(item, field, new_value)
                    record_audit(
                        "Payroll figures edited",
                        payroll_run,
                        f"{item.staff_id or item.full_name} {field}: "
                        f"{old_value:.2f} -> {new_value:.2f}",
                    )
                    changes += 1

                # bank_branch is free text, not a money field — handled outside
                # the numeric loop so the float() guard doesn't reject it.
                raw_branch = request.form.get(f"item-{item.id}-bank_branch")
                if raw_branch is not None:
                    new_branch = raw_branch.strip() or None
                    old_branch = item.bank_branch or None
                    if new_branch != old_branch:
                        item.bank_branch = new_branch
                        record_audit(
                            "Payroll figures edited",
                            payroll_run,
                            f"{item.staff_id or item.full_name} bank_branch: "
                            f"{old_branch or '—'} -> {new_branch or '—'}",
                        )
                        changes += 1
        db.session.commit()
        if rejected:
            flash(f"{rejected} invalid value(s) were ignored (must be numbers >= 0).", "warning")
        if changes:
            flash(
                f"Saved {changes} change(s). Computed figures are now stale — "
                "run Calculate Pay to recompute PAYE/SSNIT/net.",
                "success",
            )
        else:
            flash("No changes to save.", "info")
        return redirect(url_for("payroll.edit_items", run_id=run_id))

    raw_entries = (
        RawPayEntry.query.filter_by(payroll_run_id=run_id)
        .order_by(RawPayEntry.employee_id_str, RawPayEntry.pay_code)
        .all()
        if is_raw
        else []
    )
    return render_template(
        "payroll_items_edit.html",
        payroll_run=payroll_run,
        editable=editable,
        is_raw=is_raw,
        raw_entries=raw_entries,
        editable_fields=EDITABLE_ITEM_FIELDS,
    )


@payroll_bp.route("/clients/<int:client_id>/wage-rates", methods=["GET", "POST"])
@role_required("admin")
def wage_rates(client_id):
    """Admin-managed hourly rates per pay code for a raw/hourly client —
    client-wide defaults plus optional per-employee overrides."""
    client = db.get_or_404(ClientCompany, client_id)
    if request.method == "POST":
        pay_code = (request.form.get("pay_code") or "").strip().upper()
        try:
            hourly_rate = float(request.form["hourly_rate"])
        except (KeyError, ValueError):
            flash("Enter a numeric hourly rate.", "warning")
            return redirect(url_for("payroll.wage_rates", client_id=client_id))
        if not pay_code or hourly_rate <= 0:
            flash("Pay code and a positive hourly rate are required.", "warning")
            return redirect(url_for("payroll.wage_rates", client_id=client_id))
        category = (request.form.get("category") or "").strip().lower()
        if category not in WageRateProfile.CATEGORIES:
            flash("Choose a pay category (basic / overtime / bonus / allowance).", "warning")
            return redirect(url_for("payroll.wage_rates", client_id=client_id))
        employee_id = request.form.get("employee_id") or None
        profile = WageRateProfile.query.filter_by(
            client_company_id=client.id,
            employee_id=int(employee_id) if employee_id else None,
            pay_code=pay_code,
        ).first()
        if profile is None:
            profile = WageRateProfile(
                client_company_id=client.id,
                employee_id=int(employee_id) if employee_id else None,
                pay_code=pay_code,
            )
            db.session.add(profile)
        profile.hourly_rate = hourly_rate
        profile.category = category
        profile.description = request.form.get("description") or profile.description
        record_audit(
            "Wage rate saved",
            profile,
            f"{client.name} {pay_code} = {hourly_rate} [{category}] "
            f"({'employee override' if employee_id else 'client default'})",
        )
        db.session.commit()
        flash(f"Rate saved for {pay_code}.", "success")
        return redirect(url_for("payroll.wage_rates", client_id=client_id))

    profiles = (
        WageRateProfile.query.filter_by(client_company_id=client.id)
        .order_by(WageRateProfile.pay_code, WageRateProfile.employee_id)
        .all()
    )
    employees = (
        Employee.query.filter_by(client_company_id=client.id, status="Active")
        .order_by(Employee.staff_id)
        .all()
    )
    # Pay codes seen in this client's raw imports but with no configured rate.
    imported_codes = {
        row[0]
        for row in db.session.query(RawPayEntry.pay_code)
        .join(PayrollRun, RawPayEntry.payroll_run_id == PayrollRun.id)
        .filter(PayrollRun.client_company_id == client.id)
        .distinct()
        .all()
    }
    configured_codes = {p.pay_code for p in profiles if p.employee_id is None}
    missing_codes = sorted(imported_codes - configured_codes)
    return render_template(
        "wage_rates.html",
        client=client,
        profiles=profiles,
        employees=employees,
        missing_codes=missing_codes,
    )


# Single-stage approval: Draft -> Pending Approval -> Approved/Rejected.
# The legacy two-stage review flow (mark-reviewed / submit-review /
# submit-md-approval writing reviewed_by/reviewed_at) is retired; the columns
# remain in the schema but are no longer written.
@payroll_bp.route("/runs/<int:run_id>/submit-for-approval", methods=["POST"])
@role_required("admin", "accounts_officer")
def submit_for_approval(run_id):
    payroll_run = db.get_or_404(PayrollRun, run_id)
    payroll_run.status = PENDING_APPROVAL
    record_audit("Payroll edit", payroll_run, "Payroll moved to Pending Approval.")
    db.session.commit()
    flash("Payroll submitted for approval.", "success")
    return redirect(url_for("payroll.detail", run_id=run_id))


@payroll_bp.route("/runs/<int:run_id>/approve", methods=["POST"])
@role_required("admin", "md")
def approve(run_id):
    payroll_run = db.get_or_404(PayrollRun, run_id)
    payroll_run.status = APPROVED
    payroll_run.approved_by = current_user.id
    payroll_run.approved_at = datetime.now(timezone.utc)
    record_audit("Payroll approval", payroll_run, "Payroll approved (single-stage approval).")
    db.session.commit()
    flash("Payroll approved.", "success")
    return redirect(url_for("payroll.detail", run_id=run_id))


@payroll_bp.route("/runs/<int:run_id>/reject", methods=["POST"])
@role_required("admin", "md")
def reject(run_id):
    payroll_run = db.get_or_404(PayrollRun, run_id)
    payroll_run.status = REJECTED
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
    payroll_run.status = PROCESSED
    record_audit("Payroll processed", payroll_run, "Payroll marked as processed.")
    db.session.commit()
    flash("Payroll marked as processed.", "success")
    return redirect(url_for("payroll.detail", run_id=run_id))


# Hard delete is deliberately narrow: only runs that never moved money or
# reached a worker qualify. Draft/Previewed are pre-approval; Rejected is a
# terminal dead-end that (per the approval workflow) can never have produced a
# voucher, remittance, or sent payslip — so it is exactly as safe to delete as
# a Draft, and reuploading over it should replace it.
DELETABLE_STATUSES = {DRAFT, "Previewed", REJECTED}


def payroll_run_delete_blockers(payroll_run):
    """Why this run may NOT be hard-deleted, as human-readable reasons.
    Empty list means deletion is allowed."""
    blockers = []
    if payroll_run.status not in DELETABLE_STATUSES:
        blockers.append(
            f"run status is {payroll_run.status} "
            "(only Draft or Rejected runs can be deleted)"
        )
    if payroll_run.voucher:
        blockers.append(
            f"a payment voucher exists ({payroll_run.voucher.voucher_number})"
        )
    if payroll_run.remittances:
        blockers.append(f"{len(payroll_run.remittances)} remittance record(s) exist")
    sent_deliveries = PayslipDelivery.query.filter_by(
        payroll_run_id=payroll_run.id, status=DELIVERY_SENT
    ).count()
    if sent_deliveries:
        blockers.append(
            f"{sent_deliveries} payslip(s) were already sent to workers"
        )
    # Not in the original spec but required by the schema: Expense rows hold a
    # plain FK to payroll_run with no cascade, so deleting past them would
    # raise IntegrityError — and they are money records in their own right.
    linked_expenses = Expense.query.filter_by(payroll_run_id=payroll_run.id).count()
    if linked_expenses:
        blockers.append(f"{linked_expenses} expense record(s) are linked to this run")
    return blockers


def hard_delete_payroll_run(payroll_run):
    """Irreversibly delete a payroll run and its dependent rows.

    Returns ``(True, None)`` or ``(False, reason)``. Does NOT commit — the
    caller owns the transaction so callers like the replace-existing flow can
    delete-and-recreate atomically.

    Order matters: PayslipDelivery and RawPayEntry carry non-nullable FKs to
    payroll_run with no DB-side cascade, so they go first; ImportBatch rows
    for the run are removed too (these are the "Previewed" leftovers this
    feature exists to clean up). PayrollItem rows are handled by the
    relationship's own delete-orphan cascade — no extra code."""
    blockers = payroll_run_delete_blockers(payroll_run)
    if blockers:
        return False, "; ".join(blockers)

    client = payroll_run.client_company
    record_audit(
        "Payroll run hard-deleted",
        payroll_run,
        f"Deleted run #{payroll_run.id} {client.name if client else 'no client'} "
        f"{payroll_run.month} {payroll_run.year} (status {payroll_run.status}): "
        f"workers={payroll_run.total_workers} gross={payroll_run.total_gross_pay} "
        f"net={payroll_run.total_net_pay} paye={payroll_run.total_paye} "
        f"ssnit={payroll_run.total_ssnit}. Source file: "
        f"{payroll_run.source_filename or 'n/a'}.",
    )

    PayslipDelivery.query.filter_by(payroll_run_id=payroll_run.id).delete()
    RawPayEntry.query.filter_by(payroll_run_id=payroll_run.id).delete()
    ImportBatch.query.filter_by(payroll_run_id=payroll_run.id).delete()
    db.session.delete(payroll_run)  # PayrollItems cascade via the relationship
    return True, None


# Deletion is more sensitive than export: it destroys payroll history, so it
# is restricted to admin and MD. accounts_officer/payroll_officer keep their
# export access but cannot erase runs.
@payroll_bp.route("/runs/<int:run_id>/delete", methods=["POST"])
@role_required("admin", "md")
def delete_run(run_id):
    payroll_run = db.get_or_404(PayrollRun, run_id)
    label = (
        f"{payroll_run.client_company.name if payroll_run.client_company else 'Run'} "
        f"{payroll_run.month} {payroll_run.year}"
    )
    ok, reason = hard_delete_payroll_run(payroll_run)
    if not ok:
        flash(f"Cannot delete {label}: {reason}.", "danger")
        return redirect(url_for("payroll.detail", run_id=run_id))
    db.session.commit()
    flash(f"Payroll run {label} permanently deleted.", "success")
    return redirect(url_for("payroll.runs"))


@payroll_bp.route("/runs/<int:run_id>/export")
@role_required("admin", "md", "accounts_officer", "payroll_officer")
def export(run_id):
    payroll_run = db.get_or_404(PayrollRun, run_id)
    file_path = export_payroll_run(payroll_run, current_app.config["EXPORT_FOLDER"])
    payroll_run.status = PROCESSED
    record_audit("Payroll export", payroll_run, "Payroll exported and marked as processed.")
    db.session.commit()
    return send_file(file_path, as_attachment=True)


@payroll_bp.route("/runs/<int:run_id>/export/bank-listing")
@role_required("admin", "md", "accounts_officer", "payroll_officer")
def export_bank_listing_route(run_id):
    """Bank transfer batch listing grouped by bank_name — generated from the
    run's items, not a hand-maintained sheet."""
    payroll_run = db.get_or_404(PayrollRun, run_id)
    file_path = export_bank_listing(payroll_run, current_app.config["EXPORT_FOLDER"])
    record_audit("Bank listing export", payroll_run, "Bank transfer listing generated.")
    db.session.commit()
    return send_file(file_path, as_attachment=True)


@payroll_bp.route("/runs/<int:run_id>/export/wages-sheet")
@role_required("admin", "md", "accounts_officer", "payroll_officer")
def export_wages_sheet_route(run_id):
    """Wages Sheet (ACS "WAGE SHT" layout) for the run — 17 columns plus
    totals, generated from the run's items."""
    payroll_run = db.get_or_404(PayrollRun, run_id)
    file_path = export_wages_sheet(payroll_run, current_app.config["EXPORT_FOLDER"])
    record_audit("Wages sheet export", payroll_run, "Wages sheet generated.")
    db.session.commit()
    return send_file(file_path, as_attachment=True)


@payroll_bp.route("/runs/<int:run_id>/export/gra-paye")
@role_required("admin", "md", "accounts_officer", "payroll_officer")
def export_gra_paye_route(run_id):
    """GRA Employer's Monthly Tax Deductions Schedule (P.A.Y.E.) for the run."""
    payroll_run = db.get_or_404(PayrollRun, run_id)
    file_path = export_gra_paye_schedule(
        payroll_run,
        current_app.config["EXPORT_FOLDER"],
        employer_tin=os.getenv("CHRISNAT_EMPLOYER_TIN", ""),
        tax_office=os.getenv("CHRISNAT_TAX_OFFICE", ""),
    )
    record_audit("GRA PAYE schedule export", payroll_run, "GRA PAYE schedule generated.")
    db.session.commit()
    return send_file(file_path, as_attachment=True)


@payroll_bp.route("/items/<int:item_id>/payslip")
@role_required("admin", "md", "accounts_officer", "payroll_officer")
def payslip(item_id):
    payroll_item = db.get_or_404(PayrollItem, item_id)
    file_path = generate_payslip_pdf(payroll_item, current_app.config["EXPORT_FOLDER"])
    return send_file(
        file_path,
        mimetype="application/pdf",
        as_attachment=True,
        download_name=os.path.basename(file_path),
    )
