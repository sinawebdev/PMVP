"""Column mapping for the DZ-style rich RAW-DATA workbook.

The RAW DATA sheet is a fixed positional template (the same monthly workbook
for the company), so columns are resolved by position and then *validated*
against sentinel header labels — if the template shifts, we raise loudly rather
than read the wrong column (mirroring the ACS ``mapping_conflicts`` hard-stop).

Layout (1-based columns, verified against ``DZ-PAYROLL JAN 2026.xlsx``):

  A  staff key (staff-ID token, or a bare sequence number for salaried admin)
  B  NAMES
  D..L  input HOURS per element (Normal, Weekday/Sat/Sun-Hol OT, Afternoon,
        Night, 6to6 Day, 6to6 Night, 4crew)
  M     DAILY RATE (per employee)
  N..V  per-employee per-hour RATE per element (parallel to D..L)
  W     BASIC WAGE
  BC    ICU DUES        (>0 => union member)
  BI    JOB TITLE   BK  DEP'T   BN  MONTHLY TAX RELIEF
  BP    GHANA CARD  BQ  SOCIAL SECURITY NO.  BR  A/C NO.  BS  BANK  BT  BRANCH

Only INPUT / context columns are read here — never the computed columns
(gross AQ, PAYE AR, net BL). Pay is derived by the compute engine, never
trusted from the sheet.
"""
from app.models import WageRateProfile
from app.raw_engine.cleaning import normalise_element

RAW_DATA_SHEET = "RAW DATA"
NAME_HEADER_LABEL = "NAMES"

# --- employee-master columns (1-based) -------------------------------------
COL_STAFF_KEY = 1     # A
COL_NAME = 2          # B
COL_DAILY_RATE = 13   # M
COL_BASIC_WAGE = 23   # W
COL_ICU_DUES = 55     # BC
COL_JOB_TITLE = 61    # BI
COL_DEPARTMENT = 63   # BK
COL_TAX_RELIEF = 66   # BN  (MONTHLY TAX RELIEF)
COL_GHANA_CARD = 68   # BP
COL_SSNIT_NO = 69     # BQ
COL_ACCOUNT_NO = 70   # BR
COL_BANK = 71         # BS
COL_BRANCH = 72       # BT

# --- pay-element registry --------------------------------------------------
# Each element pairs an input-hours column with the per-employee rate column,
# a canonical pay_code (the WageRateProfile key), a display label, its
# statutory category, and the expected element-label (in the element-label
# header row) used to validate the positional layout.
#
# fields: (pay_code, label, category, hours_col, rate_col, expected_label)
ELEMENTS = [
    ("NORMAL",     "Normal hours",       WageRateProfile.CATEGORY_BASIC,     4,  14, "NORMAL"),
    ("WEEKDAY_OT", "Weekday overtime",   WageRateProfile.CATEGORY_OVERTIME,  5,  15, "WEEK DAY"),
    ("SAT_OT",     "Saturday overtime",  WageRateProfile.CATEGORY_OVERTIME,  6,  16, "SATURDAY"),
    ("SUNHOL_OT",  "Sun/Holiday overtime", WageRateProfile.CATEGORY_OVERTIME, 7, 17, "SUN / HOL"),
    ("AFTERNOON",  "Afternoon allowance", WageRateProfile.CATEGORY_ALLOWANCE, 8, 18, "AFT'NOON"),
    ("NIGHT",      "Night allowance",     WageRateProfile.CATEGORY_ALLOWANCE, 9, 19, "NIGHT"),
    ("SIXTOSIX_DAY", "6to6 day allowance", WageRateProfile.CATEGORY_ALLOWANCE, 10, 20, "6 TO 6"),
    ("SIXTOSIX_NIGHT", "6to6 night allowance", WageRateProfile.CATEGORY_ALLOWANCE, 11, 21, "6 TO 6"),
    ("FOURCREW",   "4-crew shift allowance", WageRateProfile.CATEGORY_ALLOWANCE, 12, 22, "4CREW"),
]

# The company's canonical element set (pay_code, label, category), in order.
ELEMENT_SET = [(e[0], e[1], e[2]) for e in ELEMENTS]


class HeaderError(ValueError):
    """Raised when the RAW DATA template does not match the expected layout —
    never read the wrong column, refuse the import instead."""


def _cell(ws, row, col):
    return ws.cell(row, col).value


def find_name_header_row(ws, scan_limit=40):
    """Row whose column B is the ``NAMES`` label; data begins the row below.
    Raises HeaderError if not found (wrong sheet / layout)."""
    for r in range(1, min(ws.max_row, scan_limit) + 1):
        if normalise_element(_cell(ws, r, COL_NAME)) == NAME_HEADER_LABEL:
            return r
    raise HeaderError(
        "Could not find the 'NAMES' header in column B — this is not a "
        "DZ-style rich RAW DATA sheet, or its layout has changed."
    )


def _merged_anchor(ws, row, col):
    """``(anchor_row, value)`` for ``(row, col)``. openpyxl stores a merged
    cell's value only in its top-left anchor; the other cells read ``None``. If
    ``(row, col)`` sits inside a merged range this returns the anchor's row and
    value, otherwise the cell itself — so a vertically-merged header label (e.g.
    Book1's ``D10:D11`` 'NORMAL HOURS' spanning onto the NAMES row) resolves to
    its real value instead of a blank."""
    for rng in ws.merged_cells.ranges:
        if rng.min_row <= row <= rng.max_row and rng.min_col <= col <= rng.max_col:
            return rng.min_row, ws.cell(rng.min_row, rng.min_col).value
    return row, ws.cell(row, col).value


def find_element_row(ws, name_row, scan_up=6):
    """The row carrying the element category labels, anchored on the NAMES row
    rather than a hardcoded offset. Scans from the NAMES row upward for the row
    whose first-element column reads the NORMAL label (merge-resolved) and
    returns the merge's *top* row, so the parallel un-merged rate labels on that
    same row stay visible. Handles all three real geometries — the 3-row stacked
    DZ header (categories two rows above NAMES), the merged 2-row header
    (categories merged onto the NAMES row), and a future single header row
    (categories on the NAMES row itself) — and falls back to ``name_row - 2`` if
    nothing matches."""
    first_hours_col = ELEMENTS[0][3]
    normal = normalise_element(ELEMENTS[0][5])
    for r in range(name_row, max(1, name_row - scan_up) - 1, -1):
        anchor_row, value = _merged_anchor(ws, r, first_hours_col)
        if normal and normal in normalise_element(value):
            return anchor_row
    # Fallback to the classic DZ offset, clamped to a real row so a degenerate
    # sheet (NAMES at the very top) fails loud in validate_layout rather than
    # crashing on a non-positive row index.
    return max(1, name_row - 2)


def validate_layout(ws, name_row):
    """Confirm the RAW DATA positional template by checking sentinel labels on
    the element-header row (located via :func:`find_element_row`, not a fixed
    ``name_row - 2``). Two robustness rules vs. the original exact-match:

      * **merge-aware** — a header cell inside a merged range resolves to its
        anchor value, so a vertically-merged 'NORMAL HOURS' validates.
      * **token containment** — the expected label need only be *contained* in
        the (normalised) header, so 'NORMAL HOURS' satisfies 'NORMAL' and
        '6 TO 6 DAY' satisfies '6 TO 6'. A genuinely wrong column (e.g. 'RATE 1')
        still fails, so the fail-loud contract holds.

    Each header is read across the element row *and the row below it*
    (merge-resolved), so a split 'BASIC'/'WAGE' stack (DZ) reads identically to a
    merged 'BASIC WAGE' (Book1). Collects every mismatch and raises one
    HeaderError."""
    element_row = find_element_row(ws, name_row)
    problems = []

    def band(col):
        # element row + the row below (sub-label / merge tail), merge-resolved.
        return normalise_element(
            f"{_merged_anchor(ws, element_row, col)[1] or ''} "
            f"{_merged_anchor(ws, element_row + 1, col)[1] or ''}"
        )

    def require(col, token, where):
        got = band(col)
        if normalise_element(token) not in got:
            problems.append(f"{where} header {got!r} lacks {token!r}")

    # Bracketing anchors: daily rate (M), basic wage (W — split in the DZ
    # stack), ICU dues (BC — likewise split).
    require(COL_DAILY_RATE, "RATE", "column M (daily rate)")
    basic = band(COL_BASIC_WAGE)
    if "BASIC" not in basic or "WAGE" not in basic:
        problems.append(f"column W (basic wage) header {basic!r} lacks BASIC/WAGE")
    require(COL_ICU_DUES, "ICU", "column BC (ICU dues)")

    # Each element's hours column and rate column must carry the expected token.
    for pay_code, _label, _cat, hours_col, rate_col, expected in ELEMENTS:
        require(hours_col, expected, f"{pay_code}: hours column")
        require(rate_col, expected, f"{pay_code}: rate column")

    if problems:
        raise HeaderError(
            "RAW DATA layout validation failed — refusing to guess columns:\n  - "
            + "\n  - ".join(problems)
        )


# ── Master-data columns: resolved by HEADER, not fixed position ──────────────
# The employee master block (Ghana card, SSNIT, account, bank, branch, dept,
# job title, tax relief) sits at different columns in different real workbooks:
# Book1 drops two tax-relief columns the DZ specimen has, shifting everything
# from GHANA CARD onward two columns left. A fixed position that is right for
# one is wrong for the other — and, unlike the hours block, these columns were
# never validated, so a shift silently seeded a bank name into the SSNIT field
# and a branch into the account number (PMVP-05 Issue 3). We now locate each
# field by matching its header label in the element/NAMES header band.


def _compact_header(value):
    """Upper-case and drop every non-alphanumeric character so header labels
    compare regardless of spacing/punctuation: "A/C NO." -> 'ACNO',
    "DEP'T" -> 'DEPT', "SOCIAL SECURITY NO." -> 'SOCIALSECURITYNO'."""
    return "".join(ch for ch in str(value or "").upper() if ch.isalnum())


# field -> predicate(compact_header) -> bool. Exact where a near-label could
# collide (branch must NOT match 'BRANCH CODE'); startswith where the label is
# unambiguous and may be doubled by a merged header.
_MASTER_HEADER_MATCHERS = {
    "ghana_card": lambda h: h.startswith("GHANACARD"),
    "ssnit": lambda h: h.startswith("SOCIALSECURITY") or h in ("SSNIT", "SSNITNO", "SSNITNUMBER"),
    "account_no": lambda h: h in ("ACNO", "ACCNO", "ACCOUNTNO", "ACCOUNTNUMBER", "ACCOUNTSNO"),
    "bank": lambda h: h in ("BANK", "BANKNAME"),
    "branch": lambda h: h == "BRANCH",           # exact — must not match BRANCHCODE
    "department": lambda h: h in ("DEPT", "DEPARTMENT"),
    "job_title": lambda h: h in ("JOBTITLE", "POSITION", "TITLE"),
    "tax_relief": lambda h: "MONTHLY" in h and "TAXREL" in h,
}
# Payment-/statutory-critical fields. If any cannot be located by header, the
# workbook is refused rather than seeded from a guessed column.
_MASTER_REQUIRED = ("ssnit", "account_no", "bank")


def resolve_master_columns(ws, name_row):
    """``{field: 1-based column}`` for the employee master-data columns, located
    by header label (merge-resolved) instead of a fixed position. Raises
    HeaderError if a payment-critical field (SSNIT / account / bank) can't be
    found — fail loud instead of writing wrong PII."""
    element_row = find_element_row(ws, name_row)
    resolved = {}
    max_col = ws.max_column or 100
    for col in range(1, max_col + 1):
        parts = []
        for rr in (element_row, element_row + 1):
            value = _merged_anchor(ws, rr, col)[1]
            # Skip a repeat from a vertically-merged header so a 2-row label
            # isn't doubled ('GHANA CARDGHANA CARD').
            if value is not None and (not parts or parts[-1] != value):
                parts.append(value)
        header = _compact_header(" ".join(str(p) for p in parts))
        if not header:
            continue
        for field, matches in _MASTER_HEADER_MATCHERS.items():
            if field not in resolved and matches(header):
                resolved[field] = col
    missing = [f for f in _MASTER_REQUIRED if f not in resolved]
    if missing:
        raise HeaderError(
            "RAW DATA master columns could not be located by header ("
            + ", ".join(missing)
            + ") — refusing to seed employee bank/SSNIT details from guessed "
            "columns. Check the workbook's header labels."
        )
    return resolved
