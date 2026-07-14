"""Web entry points for the Raw Hours Engine (Phase 7).

Wires the tested library into the app. All routes are admin-only. The upload
branches on whether the client is already seeded — the standard-vs-raw choice is
the admin's explicit selection; engine flow is never auto-detected from file
content, and a seeded/thin mismatch stops loudly rather than processing.

Preview -> confirm reuses the disk-staged pattern (the original bytes + metadata
are staged in ``IMPORT_SESSION_FOLDER``, keyed by a one-time token; only the
token rides in the cookie).
"""
import io
import json
import os
import uuid
import zipfile

from flask import (
    Blueprint,
    abort,
    current_app,
    jsonify,
    request,
    send_file,
    session,
    url_for,
)
from flask_login import current_user

from app import db
from app.auth import role_required
from app.models import (
    ClientCompany,
    Employee,
    PayrollRun,
    RawUploadArchive,
    WageRateProfile,
)
from app.payroll_calculations import statutory_rate_for_run
from app.raw_engine.cleaning import normalise_emp_id
from app.raw_engine.detection import (
    STANDARD_PAYROLL,
    classify_workbook,
    company_is_seeded,
    is_rich_raw_data,
    load_raw_workbook,
)
from app.raw_engine.exports.service import generate_run_exports
from app.raw_engine.mapping import HeaderError
from app.raw_engine.run import compute_seed_month
from app.raw_engine.seed import parse_rich_workbook
from app.raw_engine.store import archive_upload, persist_seed, write_payroll_items
from app.raw_engine.template import generate_monthly_template
from app.raw_engine.thin import ThinFormatError, join_and_compute, parse_thin_workbook

raw_engine_bp = Blueprint("raw_engine", __name__, url_prefix="/raw")

DRAFT = "Draft"


def _stage_paths(token):
    folder = current_app.config["IMPORT_SESSION_FOLDER"]
    os.makedirs(folder, exist_ok=True)
    base = os.path.join(folder, f"rawweb_{token}")
    # The workbook keeps an .xlsx extension so openpyxl accepts it when opened
    # by path (it rejects unknown extensions like .bin).
    return base + ".xlsx", base + ".json"


def _cleanup(token):
    for path in _stage_paths(token):
        try:
            os.remove(path)
        except OSError:
            pass


# ── Upload: branch seed vs thin, stage a preview ──────────────────────────────


@raw_engine_bp.route("/upload", methods=["POST"])
@role_required("admin")
def upload():
    client_id = request.form.get("client_company_id")
    month = (request.form.get("month") or "").strip()
    year = (request.form.get("year") or "").strip()
    file = request.files.get("file")

    if not client_id:
        return jsonify({"error": "Select a client company first."}), 400
    client = db.session.get(ClientCompany, int(client_id))
    if not client:
        return jsonify({"error": "Unknown client company."}), 404
    if not month or not year:
        return jsonify({"error": "Choose a month and year."}), 400
    if not file or not file.filename:
        return jsonify({"error": "No file provided."}), 400

    content = file.read()
    if not content:
        return jsonify({"error": "The uploaded file is empty."}), 400

    token = uuid.uuid4().hex
    bin_path, json_path = _stage_paths(token)
    with open(bin_path, "wb") as handle:
        handle.write(content)

    # Shape Guard: a Standard Payroll workbook uploaded here belongs to the
    # other importer — stop before the seed/thin branch and point the user back
    # to Standard Upload. A rich RAW DATA or thin workbook classifies as
    # RAW_HOURS (not STANDARD) and passes straight through; an unknown workbook
    # falls through to the existing seed/thin refusals below.
    if classify_workbook(bin_path) == STANDARD_PAYROLL:
        _cleanup(token)
        return jsonify({
            "error": "This workbook appears to be a Standard Payroll workbook. "
                     "Please upload it using Standard Upload.",
            "wrong_tab": "standard",
        }), 422

    seeded = company_is_seeded(client.id)
    wb = None
    try:
        # Open the workbook ONCE and thread it through the whole preview pipeline
        # (is_rich + parse) instead of re-opening it for each step — a rich DZ
        # workbook is large and the repeated loads were the bulk of the upload time.
        try:
            wb = load_raw_workbook(bin_path)
        except Exception:
            _cleanup(token)
            return jsonify({
                "error": "The uploaded file could not be read as an .xlsx workbook."
            }), 422
        rich = is_rich_raw_data(wb)
        if not seeded:
            # Seed path expects the rich RAW DATA workbook.
            if not rich:
                _cleanup(token)
                return jsonify({
                    "error": "This company is not seeded yet, so the first upload "
                             "must be the rich RAW DATA workbook (with the stacked "
                             "header and rate table). Upload that to seed the company."
                }), 422
            context = parse_rich_workbook(wb, client.id, source_filename=file.filename)
            preview = {
                "employees": len(context.employees),
                "icu_members": context.icu_member_count,
                "hourly": context.hourly_count,
                "salaried": len(context.employees) - context.hourly_count,
                "warnings": context.warnings[:50],
            }
            mode = "seed"
        else:
            # Thin path expects a monthly template; a rich workbook is a mismatch.
            if rich:
                _cleanup(token)
                return jsonify({
                    "error": "This is a rich RAW DATA (seed) workbook, but the "
                             "company is already seeded. Upload the monthly thin "
                             "template instead — new hires/raises are a separate "
                             "re-seed action."
                }), 422
            inputs, _warn = parse_thin_workbook(wb)
            roster = {
                normalise_emp_id(e.staff_id)
                for e in Employee.query.filter_by(client_company_id=client.id)
            }
            blocked = [i.staff_id for i in inputs
                       if normalise_emp_id(i.staff_id) not in roster]
            preview = {
                "employees": len(inputs),
                "matched": len(inputs) - len(blocked),
                "blocked": blocked[:50],
            }
            mode = "thin"
    except (HeaderError, ThinFormatError) as exc:
        _cleanup(token)
        return jsonify({"error": str(exc)}), 422
    finally:
        if wb is not None:
            wb.close()

    with open(json_path, "w", encoding="utf-8") as handle:
        json.dump({
            "client_company_id": client.id,
            "month": month,
            "year": int(year),
            "filename": file.filename,
            "mode": mode,
        }, handle)
    session["raw_web_token"] = token

    return jsonify({
        "status": "preview",
        "token": token,
        "mode": mode,
        "client_name": client.name,
        "period": f"{month} {year}",
        "preview": preview,
    })


# ── Confirm: persist (seed) or compute (thin) ─────────────────────────────────


@raw_engine_bp.route("/confirm", methods=["POST"])
@role_required("admin")
def confirm():
    token = request.form.get("token")
    if not token or session.get("raw_web_token") != token:
        return jsonify({"error": "Session mismatch — please re-upload the file."}), 400

    bin_path, json_path = _stage_paths(token)
    if not (os.path.exists(bin_path) and os.path.exists(json_path)):
        return jsonify({"error": "Upload session expired — please re-upload."}), 400
    with open(json_path, "r", encoding="utf-8") as handle:
        meta = json.load(handle)
    with open(bin_path, "rb") as handle:
        content = handle.read()

    client_id = meta["client_company_id"]
    run = PayrollRun(
        client_company_id=client_id,
        month=meta["month"],
        year=int(meta["year"]),
        status=DRAFT,
        upload_type="raw",
        created_by=getattr(current_user, "id", None),
        source_filename=meta.get("filename"),
        import_type="Raw Data Upload",
    )
    db.session.add(run)
    db.session.flush()  # assign run.id for the archive FK
    rate = statutory_rate_for_run(run)

    if meta["mode"] == "seed":
        context = parse_rich_workbook(bin_path, client_id, source_filename=meta.get("filename"))
        # Persist context + archive the workbook bytes in ONE transaction — a
        # preservation failure rolls the whole seed (and this run) back.
        persist_seed(run=run, context=context, source_bytes=content,
                     source_filename=meta.get("filename"))
        payslips = compute_seed_month(context, rate)
        write_payroll_items(run, payslips)
        summary = {"seeded_employees": len(context.employees),
                   "computed_workers": len(payslips)}
    else:  # thin
        inputs, _warn = parse_thin_workbook(bin_path)
        result = join_and_compute(inputs, run, rate)
        write_payroll_items(run, result.payslips)
        archive_upload(run, meta.get("filename"), content, kind="thin")
        db.session.commit()
        summary = {"computed_workers": len(result.payslips),
                   "blocked": result.blocked}

    _cleanup(token)
    session.pop("raw_web_token", None)
    return jsonify({
        "status": "committed",
        "run_id": run.id,
        "mode": meta["mode"],
        "summary": summary,
        "redirect": url_for("payroll.detail", run_id=run.id),
    })


# ── Download monthly template (seeded clients) ────────────────────────────────


@raw_engine_bp.route("/clients/<int:client_id>/template")
@role_required("admin")
def download_template(client_id):
    client = db.get_or_404(ClientCompany, client_id)
    if not company_is_seeded(client_id):
        abort(404, "Company is not seeded — no template to generate yet.")
    month = request.args.get("month") or "Month"
    year = request.args.get("year") or ""
    path = generate_monthly_template(
        client_id, current_app.config["EXPORT_FOLDER"], month, year
    )
    return send_file(path, as_attachment=True, download_name=os.path.basename(path))


# ── Generate the export family for a computed raw run ─────────────────────────


@raw_engine_bp.route("/runs/<int:run_id>/exports")
@role_required("admin")
def run_exports(run_id):
    run = db.get_or_404(PayrollRun, run_id)
    if run.upload_type != "raw":
        abort(404, "Raw-engine exports are only for raw-hours runs.")
    result = generate_run_exports(run, current_app.config["EXPORT_FOLDER"])

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in result["files"].values():
            if isinstance(path, (list, tuple)):
                for p in path:
                    archive.write(p, arcname=os.path.basename(p))
            elif path:
                archive.write(path, arcname=os.path.basename(path))
    buffer.seek(0)
    name = f"raw_exports_run_{run_id}.zip"
    return send_file(buffer, as_attachment=True, download_name=name,
                     mimetype="application/zip")


# ── Download the archived original upload (integrity-verified) ────────────────


@raw_engine_bp.route("/archive/<int:archive_id>")
@role_required("admin")
def download_archive(archive_id):
    archive = db.get_or_404(RawUploadArchive, archive_id)
    import hashlib

    if hashlib.sha256(archive.content).hexdigest() != archive.sha256:
        abort(422, "Archived workbook failed its integrity check (sha256 mismatch).")
    return send_file(
        io.BytesIO(archive.content),
        as_attachment=True,
        download_name=archive.filename or f"raw_upload_{archive_id}.xlsx",
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
