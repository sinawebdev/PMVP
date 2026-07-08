"""
header_resolver.py — robust column resolution for the ACS-style payroll sheet.

Root cause of the header mismatch (proven against acs 1.xlsx / RAW DATA):
  * The header is THREE stacked rows (8=group, 9=label, 10=sub-label);
    real data starts at the first row whose column A is a staff-ID (row 12).
  * Keyed on ONE row, labels collide. The fatal one: on row 9 "BASIC" matches
    BOTH column C (BASIC SALARY) and column P (BASIC TAX / PAYE). Last-wins binds
    'basic_salary' to the TAX column, so the real salary in C is never read.
  * Fix: build a COMPOSITE header (row9 + row10) so BASIC SALARY != BASIC TAX,
    anchor the data-start on the staff-ID pattern, and FAIL LOUDLY on any field
    that resolves to zero or multiple columns instead of silently last-winning.

Integrate: replace your single-row label lookup with build_column_map(); feed the
returned {field: col_index} to your row reader. Read INPUT columns only — never
the computed columns (gross/net/tax); those are derived by the compute engine.
"""
import re

# Rows that make up the stacked header block (1-based, as in Excel).
HEADER_LABEL_ROWS = (9, 10)
# A staff-ID in column A marks the first data row (AC605, MT035, ...).
STAFF_ID_RE = re.compile(r'^[A-Z]{2,4}\d{2,}$')


class HeaderError(ValueError):
    """Raised when a required column is missing or ambiguous — never last-wins."""


def _norm(*cells):
    """Composite key from stacked header cells: upper, single-spaced, trimmed."""
    parts = [str(c).strip() for c in cells if c not in (None, "")]
    return re.sub(r'\s+', ' ', " ".join(parts)).upper()


def find_data_start(ws, col_a=1, scan_limit=60):
    """First row whose column A matches the staff-ID pattern. Header ends above it."""
    for r in range(1, min(ws.max_row, scan_limit) + 1):
        v = ws.cell(r, col_a).value
        if isinstance(v, str) and STAFF_ID_RE.match(v.strip()):
            return r
    raise HeaderError("No staff-ID found in column A — wrong sheet or header layout.")


def build_composite_headers(ws, label_rows=HEADER_LABEL_ROWS):
    """{col_index: 'COMPOSITE LABEL'} built from the stacked label rows."""
    headers = {}
    for c in range(1, ws.max_column + 1):
        key = _norm(*(ws.cell(r, c).value for r in label_rows))
        if key:
            headers[c] = key
    return headers

# Canonical INPUT field -> the exact composite label(s) in the sheet.
# NOTE: labels include the sheet's real misspellings ("DIFFERECE", "RELEIF").
REQUIRED_INPUTS = {
    "staff_id":            ("IRS/NO.",),
    "full_name":           ("NAMES",),
    "basic_salary":        ("BASIC SALARY",),
    "end_of_year_bonus":   ("END OF YEAR BONUS",),
    "overtime_pay":        ("OVERTIME ALLOW",),
    "transport_allowance": ("TRANSPORT ALLOWANCE",),
    "meal_allowance":      ("MEAL ALLOWANCE",),
    "medical_allowance":   ("MEDICAL ALLOW",),
    "pay_difference":      ("PAY DIFFERECE",),          # sic
    "pf_fund_employee":    ("PF FUND EMPLOYEE",),
    "loan_deduction":      ("LOAN ADV",),
    "welfare_deduction":   ("WELFARE SUPPLIES",),
    "other_deduction":     ("OTHER DEDUCTION",),
    "iou_deduction":       ("I.O.U DEDUCTION",),
    "monthly_tax_relief":  ("MONTHLY TAX RELEIF",),     # sic
}
# basic_salary is non-negotiable: no basic column => refuse the import.
MANDATORY = {"staff_id", "full_name", "basic_salary"}


def build_column_map(ws, field_aliases=REQUIRED_INPUTS, mandatory=MANDATORY):
    """
    Resolve {field: col_index} strictly. Raises HeaderError on any field that
    matches multiple columns (ambiguous) or, if mandatory, zero columns (missing).
    This is what stops 'BASIC' silently binding to the tax column.
    """
    headers = build_composite_headers(ws)          # {col: 'COMPOSITE'}
    by_label = {}
    for col, label in headers.items():
        by_label.setdefault(label, []).append(col)

    resolved, problems = {}, []
    for field, aliases in field_aliases.items():
        hits = [c for alias in aliases for c in by_label.get(_norm(alias), [])]
        hits = sorted(set(hits))
        if len(hits) == 1:
            resolved[field] = hits[0]
        elif len(hits) > 1:
            problems.append(f"'{field}' is AMBIGUOUS -> columns {hits} "
                            f"(alias {aliases}); refusing to guess.")
        elif field in mandatory:
            problems.append(f"'{field}' NOT FOUND (alias {aliases}).")
    if problems:
        raise HeaderError("Header resolution failed:\n  - " + "\n  - ".join(problems))
    return resolved


# --- example wiring ------------------------------------------------------------
# import openpyxl
# ws = openpyxl.load_workbook(path, data_only=True)["RAW DATA"]
# start = find_data_start(ws)                 # -> 12
# cols  = build_column_map(ws)                # -> {'basic_salary': 3 (C), ...}  or HeaderError
# for r in range(start, ws.max_row + 1):
#     staff = ws.cell(r, cols['staff_id']).value
#     if not staff or not STAFF_ID_RE.match(str(staff).strip()):
#         continue                            # skip blank / total rows
#     basic = ws.cell(r, cols['basic_salary']).value or 0
#     ...                                     # feed INPUTS to compute_payroll_item()
