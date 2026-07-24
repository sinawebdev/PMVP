"""Client (tenant) plane — self-service Raw Hours Engine upload.

The tenant equivalent of the operator ``raw_engine.web`` flow (seed → thin),
tenant-scoped and risk-gated. A client_admin/client_preparer uploads a raw-hours
workbook FOR THEIR OWN COMPANY; ``client_company_id`` is forced to the active
tenant here and never read from the request, so a client can only ever seed /
compute their own company. The seed-vs-thin branch is decided by whether the
company is already seeded (not by a file-content guess), exactly like the
operator flow.

Preview → confirm reuses the disk-staged pattern: the original bytes + metadata
are staged in ``IMPORT_SESSION_FOLDER`` keyed by a one-time token; only the token
rides in the session cookie. On confirm the run is created with ``upload_type
"raw"`` and — because it is a client submission — routed through the Phase 5 risk
gate (Submitted → Held/Auto-Accepted), the same lifecycle the standard client
upload uses. These routes live on the ``client`` blueprint (registered from
``app/client/__init__.py``) so every URL stays under ``/company`` and inherits the
tenant guards.
"""
import json
import os
import time
import uuid
from datetime import datetime, timezone

from flask import current_app, jsonify, request, send_file, session, url_for
from flask_login import current_user

from app import db
from app.audit import record_audit
from app.client import _company, client_bp
from app.events import platform_admins, record_event
from app.models import Employee, PayrollRun
from app.payroll_calculations import statutory_rate_for_run
from app.payroll_status import AUTO_ACCEPTED, HELD, SUBMITTED
from app.raw_engine.cleaning import normalise_emp_id
from app.raw_engine.detection import (
    STANDARD_PAYROLL,
    classify_workbook,
    company_is_seeded,
    is_rich_raw_data,
    load_raw_workbook,
)
from app.raw_engine.mapping import HeaderError
from app.raw_engine.run import compute_seed_month
from app.raw_engine.seed import parse_rich_workbook
from app.raw_engine.store import archive_upload, persist_seed, write_payroll_items
from app.raw_engine.template import generate_monthly_template
from app.raw_engine.thin import ThinFormatError, join_and_compute, parse_thin_workbook
from app.risk import apply_risk_gate
from app.roles import CLIENT_ADMIN, CLIENT_PREPARER
from app.tenancy import tenant_role_required

DRAFT = "Draft"
_SESSION_KEY = "client_raw_token"
_STAGE_PREFIX = "clientraw_"
STAGING_MAX_AGE_SECONDS = 24 * 3600


def _stage_paths(token):
    folder = current_app.config["IMPORT_SESSION_FOLDER"]
    os.makedirs(folder, exist_ok=True)
    base = os.path.join(folder, f"{_STAGE_PREFIX}{token}")
    # The workbook keeps an .xlsx extension so openpyxl accepts it when opened by
    # path (it rejects unknown extensions like .bin).
    return base + ".xlsx", base + ".json"


def _cleanup(token):
    for path in _stage_paths(token):
        try:
            os.remove(path)
        except OSError:
            pass


def _sweep_stale_staging(max_age_seconds=STAGING_MAX_AGE_SECONDS):
    """Best-effort removal of abandoned client-raw staging files. Never raises —
    a cleanup failure must never block an upload."""
    folder = current_app.config.get("IMPORT_SESSION_FOLDER")
    if not folder or not os.path.isdir(folder):
        return 0
    cutoff = time.time() - max_age_seconds
    removed = 0
    try:
        for name in os.listdir(folder):
            if not name.startswith(_STAGE_PREFIX):
                continue
            path = os.path.join(folder, name)
            try:
                if os.path.getmtime(path) < cutoff:
                    os.remove(path)
                    removed += 1
            except OSError:
                pass
    except OSError:
        pass
    return removed


# ── Upload: branch seed vs thin, stage a preview ──────────────────────────────


@client_bp.route("/runs/raw/upload", methods=["POST"])
@tenant_role_required(CLIENT_ADMIN, CLIENT_PREPARER)
def raw_upload():
    _sweep_stale_staging()  # opportunistically clear abandoned preview files
    company = _company()  # the active tenant — never taken from the request
    month = (request.form.get("month") or "").strip()
    year = (request.form.get("year") or "").strip()
    file = request.files.get("file")

    if not month or not year:
        return jsonify({"error": "Choose a month and year."}), 400
    try:
        int(year)
    except (TypeError, ValueError):
        return jsonify({"error": "Year must be a number."}), 400
    if not file or not file.filename:
        return jsonify({"error": "No file provided."}), 400

    content = file.read()
    if not content:
        return jsonify({"error": "The uploaded file is empty."}), 400

    token = uuid.uuid4().hex
    bin_path, json_path = _stage_paths(token)
    with open(bin_path, "wb") as handle:
        handle.write(content)

    # Shape Guard: a Standard Payroll workbook uploaded here belongs to the other
    # importer — stop and point the user back to Standard Upload.
    if classify_workbook(bin_path) == STANDARD_PAYROLL:
        _cleanup(token)
        return jsonify({
            "error": "This looks like a Standard Payroll workbook. "
                     "Please use Standard Payroll Upload instead.",
            "wrong_tab": "standard",
        }), 422

    seeded = company_is_seeded(company.id)
    wb = None
    try:
        try:
            wb = load_raw_workbook(bin_path)
        except Exception:  # noqa: BLE001 - any read failure is a bad workbook
            _cleanup(token)
            return jsonify({
                "error": "The uploaded file could not be read as an .xlsx workbook."
            }), 422
        rich = is_rich_raw_data(wb)
        if not seeded:
            # First upload for a not-yet-seeded company must be the rich workbook.
            if not rich:
                _cleanup(token)
                return jsonify({
                    "error": "Your company is not set up for raw-hours yet, so the "
                             "first upload must be the rich RAW DATA workbook (with "
                             "the stacked header and rate table). Upload that to set "
                             "up raw-hours payroll."
                }), 422
            context = parse_rich_workbook(wb, company.id, source_filename=file.filename)
            preview = {
                "employees": len(context.employees),
                "icu_members": context.icu_member_count,
                "hourly": context.hourly_count,
                "salaried": len(context.employees) - context.hourly_count,
                "warnings": context.warnings[:50],
            }
            mode = "seed"
        else:
            # Seeded company expects the monthly thin template; rich = mismatch.
            if rich:
                _cleanup(token)
                return jsonify({
                    "error": "This is a rich RAW DATA (setup) workbook, but your "
                             "company is already set up. Upload the monthly template "
                             "instead — new hires/raises are a separate re-setup step "
                             "handled with your provider."
                }), 422
            inputs, _warn = parse_thin_workbook(wb)
            roster = {
                normalise_emp_id(e.staff_id)
                for e in Employee.query.filter_by(client_company_id=company.id)
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
            "client_company_id": company.id,
            "month": month,
            "year": int(year),
            "filename": file.filename,
            "mode": mode,
        }, handle)
    session[_SESSION_KEY] = token

    return jsonify({
        "status": "preview",
        "token": token,
        "mode": mode,
        "client_name": company.name,
        "period": f"{month} {year}",
        "preview": preview,
    })


# ── Confirm: persist (seed) or compute (thin), then risk-gate ─────────────────


@client_bp.route("/runs/raw/confirm", methods=["POST"])
@tenant_role_required(CLIENT_ADMIN, CLIENT_PREPARER)
def raw_confirm():
    company = _company()
    token = request.form.get("token")
    if not token or session.get(_SESSION_KEY) != token:
        return jsonify({"error": "Session mismatch — please re-upload the file."}), 400

    bin_path, json_path = _stage_paths(token)
    if not (os.path.exists(bin_path) and os.path.exists(json_path)):
        return jsonify({"error": "Upload session expired — please re-upload."}), 400
    with open(json_path, "r", encoding="utf-8") as handle:
        meta = json.load(handle)
    # The staged metadata is trusted only for the file's shape; the company is
    # re-derived from the active tenant so a swapped session can't cross tenants.
    if meta.get("client_company_id") != company.id:
        return jsonify({"error": "Session mismatch — please re-upload the file."}), 400
    with open(bin_path, "rb") as handle:
        content = handle.read()

    # One transaction: the run, its seed/thin context, the archived workbook AND
    # the computed items commit together (commit=False on the store calls, a single
    # commit at the end). A failure anywhere rolls the whole thing back; staged
    # files are kept on failure so the client can retry.
    try:
        run = PayrollRun(
            client_company_id=company.id,
            month=meta["month"],
            year=int(meta["year"]),
            status=DRAFT,  # transient — moved to SUBMITTED before the risk gate
            upload_type="raw",
            created_by=getattr(current_user, "id", None),
            source_filename=meta.get("filename"),
            import_type="Raw Data Upload",
        )
        db.session.add(run)
        db.session.flush()  # assign run.id for the archive/seed FKs
        rate = statutory_rate_for_run(run)

        if meta["mode"] == "seed":
            context = parse_rich_workbook(
                bin_path, company.id, source_filename=meta.get("filename")
            )
            persist_seed(run=run, context=context, source_bytes=content,
                         source_filename=meta.get("filename"), commit=False)
            payslips = compute_seed_month(context, rate)
            write_payroll_items(run, payslips, commit=False)
            summary = {"seeded_employees": len(context.employees),
                       "computed_workers": len(payslips)}
        else:  # thin
            inputs, _warn = parse_thin_workbook(bin_path)
            result = join_and_compute(inputs, run, rate)
            write_payroll_items(run, result.payslips, commit=False)
            archive_upload(run, meta.get("filename"), content, kind="thin")
            summary = {"computed_workers": len(result.payslips),
                       "blocked": result.blocked}

        # Phase 5 lifecycle: a client submission is risk-gated, not operator-DRAFT.
        run.status = SUBMITTED
        db.session.flush()
        verdict = apply_risk_gate(run, when=datetime.now(timezone.utc))
        run.status = HELD if verdict.held else AUTO_ACCEPTED
        reasons = verdict.reasons_text() or "no rule tripped"
        record_audit(
            "Client raw run imported",
            run,
            f"{run.month} {run.year} raw-hours ({meta['mode']}) imported by client "
            f"from {meta.get('filename')}. Risk: {verdict.status} ({reasons}).",
        )
        record_event(
            "run.risk_held" if verdict.held else "run.risk_accepted",
            summary=f"{company.name} submitted raw-hours {run.month} {run.year}: {reasons}.",
            subject=run,
            client_company_id=company.id,
            level="warning" if verdict.held else "info",
            payload={"status": verdict.status, "reasons": verdict.reasons},
            recipients=platform_admins(),
        )
        db.session.commit()
        run_id = run.id
        held = verdict.held
    except Exception:  # noqa: BLE001 - any failure must write nothing
        db.session.rollback()
        current_app.logger.exception("Client raw confirm failed (token %s)", token)
        return jsonify({
            "error": "Could not save this upload — nothing was written. "
                     "Please try again."
        }), 500

    _cleanup(token)
    session.pop(_SESSION_KEY, None)
    return jsonify({
        "status": "committed",
        "run_id": run_id,
        "mode": meta["mode"],
        "summary": summary,
        "held": held,
        "redirect": url_for("client.run_detail", run_id=run_id),
    })


# ── Download the monthly template (already-seeded tenants) ─────────────────────


@client_bp.route("/runs/raw/template")
@tenant_role_required(CLIENT_ADMIN, CLIENT_PREPARER)
def raw_template():
    company = _company()
    if not company_is_seeded(company.id):
        return jsonify({
            "error": "Your company is not set up for raw-hours yet — the monthly "
                     "template is available after the first setup upload."
        }), 404
    month = request.args.get("month") or "Month"
    year = request.args.get("year") or ""
    path = generate_monthly_template(
        company.id, current_app.config["EXPORT_FOLDER"], month, year
    )
    return send_file(path, as_attachment=True, download_name=os.path.basename(path))
