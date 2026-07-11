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


def validate_layout(ws, name_row):
    """Confirm the fixed positional template by checking sentinel labels in the
    stacked header rows above ``name_row``. Collects every mismatch and raises a
    single HeaderError listing them (fail loud, never guess)."""
    element_row = name_row - 2  # element names (e.g. row 10 when NAMES is row 12)
    problems = []

    def label(row, col):
        return normalise_element(_cell(ws, row, col))

    # Basic-wage and daily-rate anchors bracket the hours/rate blocks.
    basic_composite = " ".join(
        label(name_row - 3 + i, COL_BASIC_WAGE) for i in range(3)
    )
    if "BASIC" not in basic_composite or "WAGE" not in basic_composite:
        problems.append(
            f"column W (basic wage) header {basic_composite!r} lacks BASIC/WAGE"
        )
    if label(element_row, COL_DAILY_RATE) != "RATE":
        problems.append(
            f"column M (daily rate) header {label(element_row, COL_DAILY_RATE)!r} != 'RATE'"
        )
    icu_composite = " ".join(label(name_row - 3 + i, COL_ICU_DUES) for i in range(3))
    if "ICU" not in icu_composite:
        problems.append(f"column BC (ICU dues) header {icu_composite!r} lacks ICU")

    # Each element's hours column and rate column must carry the expected label.
    for pay_code, _label, _cat, hours_col, rate_col, expected in ELEMENTS:
        got_hours = label(element_row, hours_col)
        got_rate = label(element_row, rate_col)
        if got_hours != expected:
            problems.append(
                f"{pay_code}: hours column header {got_hours!r} != {expected!r}"
            )
        if got_rate != expected:
            problems.append(
                f"{pay_code}: rate column header {got_rate!r} != {expected!r}"
            )

    if problems:
        raise HeaderError(
            "RAW DATA layout validation failed — refusing to guess columns:\n  - "
            + "\n  - ".join(problems)
        )
