"""Client portal: bulk import of employees from one Excel sheet.

Upload -> preview -> confirm, through ``app.roster_import`` (shared with the
operator console). The company is always the active tenant: it is never read
from the request, and the staged file is checked against it again on confirm.
Endpoint names start with ``client.employee`` so the Employees tab stays lit.
"""
import os

from flask import flash, redirect, render_template, request, send_file, session, url_for

from app import db, roster_import
from app.client import _company, client_bp
from app.models import Employee
from app.payroll import save_temporary_upload
from app.spreadsheet_uploads import (
    LEGACY_WORKBOOK_EXTENSIONS,
    ZIP_WORKBOOK_EXTENSIONS,
    SpreadsheetValidationError,
    validate_spreadsheet_upload,
)
from app.tenancy import tenant_query, tenant_required

_SESSION_KEY = "client_emp_import_token"


def _import_form(company):
    return render_template(
        "client/employee_import.html",
        company=company,
        columns=roster_import.ROSTER_COLUMNS,
        required=roster_import.REQUIRED_FIELDS,
        max_rows=roster_import.MAX_ROWS,
    )


def _staged(company):
    token = session.get(_SESSION_KEY)
    staged = roster_import.load_staged(token, company.id) if token else None
    return token, staged


@client_bp.route("/employees/import", methods=["GET", "POST"])
@tenant_required
def employee_import():
    company = _company()
    if request.method == "GET":
        return _import_form(company)

    file = request.files.get("file")
    try:
        safe_name, _ext, _size = validate_spreadsheet_upload(
            file, allowed=ZIP_WORKBOOK_EXTENSIONS | LEGACY_WORKBOOK_EXTENSIONS
        )
        path = save_temporary_upload(file)
    except SpreadsheetValidationError as exc:
        flash(str(exc), "warning")
        return _import_form(company)
    try:
        existing = roster_import.existing_by_staff_id(tenant_query(Employee))
        parsed = roster_import.parse_roster(path, existing)
    except roster_import.RosterImportError as exc:
        flash(str(exc), "warning")
        return _import_form(company)
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass

    previous = session.pop(_SESSION_KEY, None)
    if previous:
        roster_import.discard_staged(previous)
    session[_SESSION_KEY] = roster_import.stage_roster(company.id, parsed, safe_name)
    return redirect(url_for("client.employee_import_preview"))


@client_bp.route("/employees/import/preview")
@tenant_required
def employee_import_preview():
    company = _company()
    _token, staged = _staged(company)
    if staged is None:
        flash("That import has expired. Upload the sheet again.", "warning")
        return redirect(url_for("client.employee_import"))
    return render_template(
        "client/employee_import_preview.html", company=company, staged=staged
    )


@client_bp.route("/employees/import/confirm", methods=["POST"])
@tenant_required
def employee_import_confirm():
    company = _company()
    token, staged = _staged(company)
    if staged is None:
        flash("That import has expired. Upload the sheet again.", "warning")
        return redirect(url_for("client.employee_import"))
    existing = roster_import.existing_by_staff_id(tenant_query(Employee))
    created, updated = roster_import.apply_roster(staged, company, existing)
    db.session.commit()
    session.pop(_SESSION_KEY, None)
    roster_import.discard_staged(token)
    flash(roster_import.result_message(created, updated), "success")
    return redirect(url_for("client.employees"))


@client_bp.route("/employees/import/discard", methods=["POST"])
@tenant_required
def employee_import_discard():
    token = session.pop(_SESSION_KEY, None)
    if token:
        roster_import.discard_staged(token)
    flash("Import discarded. Nothing was saved.", "info")
    return redirect(url_for("client.employees"))


@client_bp.route("/employees/import/template")
@tenant_required
def employee_import_template():
    return send_file(
        roster_import.build_template(),
        download_name=roster_import.TEMPLATE_FILENAME,
        as_attachment=True,
        mimetype=roster_import.TEMPLATE_MIMETYPE,
    )
