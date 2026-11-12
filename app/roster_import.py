"""Employee roster import from Excel: one parser for both portals.

The operator console (``employees.bulk_import``) and the client portal
(``client.employee_import``) both load a whole roster from one sheet. They share
everything below: the template, the header matching, the row checks, the staged
preview and the apply step. Only the routes differ, and each route decides which
company the import belongs to. Nothing here reads a company from the request.

Flow: upload -> ``parse_roster`` -> ``stage_roster`` (a JSON file in
IMPORT_SESSION_FOLDER, keyed by a random token kept in the user's session) ->
preview -> ``apply_roster``. Staged files are checked against the company again
on confirm and swept after a day.

Rules:
- Staff ID and full name are required. Staff IDs go through ``normalise_emp_id``,
  so "DM 001" and "dm001" are the same worker.
- A staff ID already on the roster is an update. Only the cells that are filled
  in change anything, so a sheet with just names and phones never blanks a bank
  account.
- A row with a problem is skipped and listed; the other rows still import.
  Phone and MoMo numbers that are not Ghana mobiles are kept as typed (roster
  data is never rewritten) but flagged, because SMS cannot reach them.
"""
import io
import json
import os
import re
import time
import uuid
from math import isfinite

import pandas as pd
from flask import current_app
from openpyxl import Workbook
from openpyxl.styles import Font

from app import db
from app.audit import record_audit
from app.distribution.channels import is_valid_email
from app.distribution.phones import normalise_gh_mobile
from app.models import Employee
from app.raw_import import normalise_emp_id

ACTIVE = "Active"
MAX_ROWS = 2000
STAGE_PREFIX = "emp_import_"
STAGING_MAX_AGE_SECONDS = 24 * 3600

# (field, template header, aliases). The template header is what we write; the
# aliases are what we also accept, compared after ``_header_key``.
ROSTER_COLUMNS = (
    ("staff_id", "Staff ID", ("employee id", "emp id", "staff id", "staff no", "staff number",
                              "employee no", "employee number", "emp no", "clk no", "id")),
    ("full_name", "Full name", ("full name", "name", "employee name", "emp name", "staff name",
                                "worker name")),
    ("job_title", "Job title", ("job title", "title", "position", "designation", "job role", "role")),
    ("department", "Department", ("department", "dept", "division", "unit")),
    ("phone", "Phone", ("phone", "phone number", "phone no", "mobile", "mobile number",
                        "whatsapp", "contact", "tel", "telephone")),
    ("momo_number", "MoMo number", ("momo", "momo number", "momo no", "mobile money",
                                    "mobile money number", "momo wallet")),
    ("email", "Email", ("email", "email address", "e-mail", "mail")),
    ("ssnit_number", "SSNIT number", ("ssnit", "ssnit number", "ssnit no", "ssf number",
                                      "social security number")),
    ("ghana_card_number", "Ghana Card number", ("ghana card", "ghana card number", "ghana card no",
                                                "ghana card id", "national id", "nia number")),
    ("tin", "TIN", ("tin", "tin number", "tin no", "tax id", "taxpayer id")),
    ("bank_name", "Bank name", ("bank", "bank name")),
    ("bank_branch", "Bank branch", ("bank branch", "branch")),
    ("bank_account_number", "Bank account number", ("bank account number", "bank account",
                                                    "account number", "account no", "acct no",
                                                    "bank acct", "account", "a/c number", "a/c no")),
    ("basic_salary", "Basic salary (GHS)", ("basic salary", "basic", "basic pay", "salary",
                                            "monthly basic", "monthly salary")),
)
REQUIRED_FIELDS = ("staff_id", "full_name")
FIELD_LABELS = {field: header for field, header, _ in ROSTER_COLUMNS}
TEXT_FIELDS = tuple(f for f, _, _ in ROSTER_COLUMNS if f not in ("staff_id", "full_name", "basic_salary"))


class RosterImportError(Exception):
    """The file as a whole can't be used (unreadable, no required columns, too big)."""


# --- Template ------------------------------------------------------------------


def build_template():
    """An .xlsx with the headers on sheet one and the instructions on sheet two.

    The first sheet holds no example row: an example that is forgotten in place
    would be imported as a real worker. Every column except the salary is
    formatted as text, so Excel keeps the leading 0 of a phone number.
    """
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Employees"
    headers = [f"{header} *" if field in REQUIRED_FIELDS else header
               for field, header, _ in ROSTER_COLUMNS]
    sheet.append(headers)
    for cell in sheet[1]:
        cell.font = Font(bold=True)
    sheet.freeze_panes = "A2"
    for index, (field, header, _) in enumerate(ROSTER_COLUMNS, start=1):
        letter = sheet.cell(row=1, column=index).column_letter
        sheet.column_dimensions[letter].width = max(14, len(header) + 4)
        if field != "basic_salary":
            for row in range(2, MAX_ROWS + 2):
                sheet.cell(row=row, column=index).number_format = "@"

    notes = workbook.create_sheet("How to fill this in")
    notes.column_dimensions["A"].width = 24
    notes.column_dimensions["B"].width = 70
    lines = [
        ("Employees sheet", "One row per employee. Leave the header row as it is."),
        ("Required", "Staff ID and Full name. Every other column is optional."),
        ("Existing staff", "A Staff ID already on your roster updates that person. "
                           "Empty cells leave what is saved untouched."),
        ("Phone and MoMo", "Ghana mobile numbers, e.g. 024 123 4567. Payslips are texted to them."),
        ("Basic salary", "Monthly, in cedis, numbers only (e.g. 2500 or 2,500.00)."),
        ("Limit", f"Up to {MAX_ROWS:,} rows per file."),
        ("", ""),
        ("Example row", "Staff ID: EX001 | Full name: Ama Owusu | Job title: Cashier | "
                        "Phone: 0241234567 | Basic salary (GHS): 2500"),
    ]
    for label, text in lines:
        notes.append([label, text])
        notes.cell(row=notes.max_row, column=1).font = Font(bold=True)

    stream = io.BytesIO()
    workbook.save(stream)
    stream.seek(0)
    return stream


TEMPLATE_FILENAME = "payrolla_employee_import_template.xlsx"
TEMPLATE_MIMETYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


# --- Parsing -------------------------------------------------------------------

_PARENS = re.compile(r"\(.*?\)")
_SPACES = re.compile(r"\s+")


def _header_key(value):
    """'Basic salary (GHS) *' -> 'basic salary'; 'staff_id' -> 'staff id'."""
    text = str(value or "").lower().replace("_", " ").replace("*", " ")
    text = _PARENS.sub(" ", text)
    return _SPACES.sub(" ", text).strip()


_ALIASES = {}
for _field, _header, _aliases in ROSTER_COLUMNS:
    for _alias in (_header, *_aliases):
        _ALIASES.setdefault(_header_key(_alias), _field)


def _clean(value):
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() in ("nan", "none", "nat"):
        return None
    return text


_MONEY_NOISE = re.compile(r"(ghs|gh₵|ghc|₵|,|\s)", re.I)


def parse_amount(text):
    """'2,500.00' / 'GHS 2500' -> 2500.0. None for blank. Raises ValueError."""
    if text is None:
        return None
    value = float(_MONEY_NOISE.sub("", str(text)))
    if not isfinite(value) or value < 0:
        raise ValueError(text)
    return round(value, 2)


def _read_frame(path):
    try:
        frame = pd.read_excel(path, sheet_name=0, dtype=str)
    except Exception as exc:  # noqa: BLE001 - surfaced to the user as a file problem
        raise RosterImportError(f"We couldn't read that file ({exc.__class__.__name__}). "
                                "Save it as .xlsx and try again.") from exc
    return frame


def map_columns(frame):
    """{field: column name in the file} for every header we recognise."""
    mapping = {}
    for column in frame.columns:
        field = _ALIASES.get(_header_key(column))
        if field and field not in mapping:
            mapping[field] = column
    return mapping


def parse_roster(path, existing):
    """Read the sheet at ``path`` and sort its rows.

    ``existing`` maps normalised staff ID -> Employee for the target company.
    Returns ``{"to_create", "to_update", "unchanged", "errors", "warnings",
    "columns"}``; rows are plain dicts so they can be staged as JSON.
    """
    frame = _read_frame(path)
    mapping = map_columns(frame)
    missing = [FIELD_LABELS[f] for f in REQUIRED_FIELDS if f not in mapping]
    if missing:
        raise RosterImportError(
            "The sheet needs a " + " and a ".join(f"'{m}'" for m in missing)
            + " column. Download the template to see the headers we read."
        )

    to_create, to_update, errors, warnings = [], [], [], []
    unchanged = 0
    seen = set()
    rows_read = 0
    for index, row in frame.iterrows():
        values = {field: _clean(row.get(column)) for field, column in mapping.items()}
        if not any(values.values()):
            continue  # a blank line in the sheet
        rows_read += 1
        if rows_read > MAX_ROWS:
            raise RosterImportError(
                f"That sheet has more than {MAX_ROWS:,} employees. Split it into smaller files."
            )
        line = index + 2  # header is row 1
        staff_id = normalise_emp_id(values.get("staff_id") or "")
        name = values.get("full_name")
        if not staff_id:
            errors.append({"row": line, "staff_id": "", "name": name or "",
                           "issue": "Staff ID is blank"})
            continue
        if not name:
            errors.append({"row": line, "staff_id": staff_id, "name": "",
                           "issue": "Full name is blank"})
            continue
        if staff_id in seen:
            errors.append({"row": line, "staff_id": staff_id, "name": name,
                           "issue": "This Staff ID appears more than once in the sheet"})
            continue
        too_long = _too_long({**values, "staff_id": staff_id})
        if too_long:
            errors.append({"row": line, "staff_id": staff_id[:60], "name": name[:160],
                           "issue": too_long})
            continue
        email = values.get("email")
        if email and not is_valid_email(email):
            errors.append({"row": line, "staff_id": staff_id, "name": name,
                           "issue": f"'{email}' is not an email address"})
            continue
        try:
            salary = parse_amount(values.get("basic_salary"))
        except ValueError:
            errors.append({"row": line, "staff_id": staff_id, "name": name,
                           "issue": f"Basic salary '{values.get('basic_salary')}' is not an amount"})
            continue
        seen.add(staff_id)

        for field in ("phone", "momo_number"):
            number = values.get(field)
            if number and normalise_gh_mobile(number) is None:
                warnings.append({"row": line, "staff_id": staff_id, "name": name,
                                 "issue": f"{FIELD_LABELS[field]} '{number}' is not a Ghana mobile "
                                          "number, so payslip texts can't reach it"})

        record = {"row": line, "staff_id": staff_id, "full_name": name, "basic_salary": salary}
        for field in TEXT_FIELDS:
            record[field] = values.get(field)

        current = existing.get(staff_id)
        if current is None:
            to_create.append(record)
            continue
        changes = _changes(current, record)
        if changes:
            record["changes"] = changes
            to_update.append(record)
        else:
            unchanged += 1

    return {
        "to_create": to_create,
        "to_update": to_update,
        "unchanged": unchanged,
        "errors": errors,
        "warnings": warnings,
        "columns": [FIELD_LABELS[f] for f, _, _ in ROSTER_COLUMNS if f in mapping],
    }


def _too_long(values):
    """An error message if any value is longer than its database column, else None.

    Postgres refuses an over-long string outright (SQLite does not), so without
    this one long cell would fail the whole confirm on the live site.
    """
    columns = Employee.__table__.columns
    for field, value in values.items():
        if field == "basic_salary" or value is None or field not in columns:
            continue
        limit = getattr(columns[field].type, "length", None)
        if limit and len(value) > limit:
            return f"{FIELD_LABELS[field]} is longer than {limit} characters"
    return None


def _changes(employee, record):
    """Labels of the fields this row would change on ``employee``."""
    changed = []
    if record["full_name"] != (employee.full_name or ""):
        changed.append(FIELD_LABELS["full_name"])
    for field in TEXT_FIELDS:
        new = record.get(field)
        if new is not None and new != (getattr(employee, field) or None):
            changed.append(FIELD_LABELS[field])
    salary = record.get("basic_salary")
    if salary is not None and round(float(employee.basic_salary or 0), 2) != salary:
        changed.append("Basic salary")
    return changed


def existing_by_staff_id(query):
    """{normalised staff ID: Employee} over ``query`` (one company's employees)."""
    found = {}
    for employee in query.all():
        found.setdefault(normalise_emp_id(employee.staff_id or ""), employee)
    return found


# --- Staging -------------------------------------------------------------------


def _stage_path(token):
    if not re.fullmatch(r"[0-9a-f]{32}", token or ""):
        raise ValueError("bad staging token")
    folder = current_app.config["IMPORT_SESSION_FOLDER"]
    os.makedirs(folder, exist_ok=True)
    return os.path.join(folder, f"{STAGE_PREFIX}{token}.json")


def stage_roster(client_id, parsed, filename):
    """Write the parsed rows to a staging file; return its token."""
    sweep_stale_staging()
    token = uuid.uuid4().hex
    payload = dict(parsed, client_id=client_id, filename=filename)
    with open(_stage_path(token), "w", encoding="utf-8") as handle:
        json.dump(payload, handle)
    return token


def load_staged(token, client_id):
    """The staged import for ``client_id``, or None if missing, expired or not theirs."""
    try:
        path = _stage_path(token)
    except ValueError:
        return None
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as handle:
        staged = json.load(handle)
    if staged.get("client_id") != client_id:
        return None
    return staged


def discard_staged(token):
    try:
        os.remove(_stage_path(token))
    except (OSError, ValueError):
        pass


def sweep_stale_staging(max_age_seconds=STAGING_MAX_AGE_SECONDS):
    """Best-effort removal of abandoned previews. Never raises."""
    folder = current_app.config.get("IMPORT_SESSION_FOLDER")
    if not folder or not os.path.isdir(folder):
        return 0
    cutoff = time.time() - max_age_seconds
    removed = 0
    try:
        for name in os.listdir(folder):
            if not name.startswith(STAGE_PREFIX):
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


# --- Apply ---------------------------------------------------------------------


def apply_roster(staged, company, existing):
    """Create and update employees for ``company`` from a staged import.

    ``existing`` is re-read by the caller at confirm time, so a worker added by
    hand between preview and confirm is updated rather than duplicated. The
    caller commits.
    """
    created = updated = 0
    for record in staged.get("to_create", []) + staged.get("to_update", []):
        employee = existing.get(record["staff_id"])
        if employee is None:
            employee = Employee(
                client_company_id=company.id,
                assigned_client=company.name,
                staff_id=record["staff_id"],
                status=ACTIVE,
                basic_salary=0,
            )
            db.session.add(employee)
            existing[record["staff_id"]] = employee
            created += 1
        else:
            updated += 1
        employee.full_name = record["full_name"]
        for field in TEXT_FIELDS:
            if record.get(field) is not None:
                setattr(employee, field, record[field])
        if record.get("basic_salary") is not None:
            employee.basic_salary = record["basic_salary"]
        # The company binding is the caller's, never the file's.
        employee.client_company_id = company.id

    record_audit(
        "Employees imported",
        company,
        f"{created} added and {updated} updated from {staged.get('filename') or 'a spreadsheet'}.",
    )
    return created, updated


def result_message(created, updated):
    """'3 employees added and 2 updated.' for the flash after a confirm."""
    parts = []
    if created:
        parts.append(f"{created} employee{'' if created == 1 else 's'} added")
    if updated:
        parts.append(f"{updated} updated")
    return (" and ".join(parts) or "Nothing needed changing") + "."
