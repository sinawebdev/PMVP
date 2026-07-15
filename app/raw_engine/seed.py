"""Parse a rich RAW-DATA seed workbook into an in-memory :class:`SeedContext`.

This is the read/preview half of the seed path — it does not touch the
database. :mod:`app.raw_engine.store` persists a confirmed context in one
transaction.

Seeded per employee:
  * master fields (name, staff key, Ghana card, SSNIT, bank/branch/account,
    department, monthly tax relief),
  * basic wage,
  * union (ICU) membership — inferred from ICU dues > 0,
  * per-employee hourly rates (one WageRateProfile spec per non-zero rate
    element), tagged basic / overtime / allowance,
  * the seed month's raw hours (input layer, per element).

Axes are derived from the data, never a hardcoded staff list. Salaried admin
rows (no per-employee rate columns) seed a flat basic wage and no rate rows.
"""
from dataclasses import dataclass, field

from app.raw_engine.cleaning import (
    coerce_hours,
    coerce_rate,
    normalise_emp_id,
)
from app.raw_engine.detection import open_raw_data_sheet
from app.raw_engine.mapping import (
    COL_BASIC_WAGE,
    COL_DAILY_RATE,
    COL_ICU_DUES,
    COL_NAME,
    COL_STAFF_KEY,
    ELEMENTS,
    ELEMENT_SET,
    find_name_header_row,
    resolve_adjustment_columns,
    resolve_master_columns,
    validate_layout,
)

# Monthly lump-adjustment columns (inputs, not derived) — 1-based. Read from the
# rich seed workbook so the seed month can be recomputed exactly; on later thin
# uploads these come from the client's adjustment columns.
COL_PROD_BONUS = 30      # AD  PROD'TY ALLOW (bonus rule)
COL_OTHER_ALLOWANCE = 41  # AO  OTHER ALLOWANCE (ordinary taxable)
COL_PAY_DIFFERENCE = 42   # AP  PAY DIFFERENCE
COL_PROVIDENT = 54        # BB  PROVIDENT FUND (pre-tax)
COL_LOAN = 57             # BE  LOAN ADV
COL_DONATIONS = 58        # BF  DONATIONS
COL_OTHER_DEDUCTION = 59  # BG  OTHER DEDUCTION
COL_WELFARE = 60          # BH  WELFARE


@dataclass
class RateSpec:
    pay_code: str
    category: str
    hourly_rate: float
    description: str


@dataclass
class SeedEmployee:
    staff_id: str
    full_name: str
    basic_salary: float = 0.0
    icu_member: bool = False
    is_hourly: bool = False
    daily_rate: float = 0.0
    ghana_card_number: str = ""
    ssnit_number: str = ""
    bank_name: str = ""
    bank_branch: str = ""
    bank_account_number: str = ""
    department: str = ""
    job_title: str = ""
    tax_relief_monthly: float = 0.0
    rates: list = field(default_factory=list)       # list[RateSpec]
    raw_hours: dict = field(default_factory=dict)   # pay_code -> hours (seed month)
    # Seed-month lump adjustments (inputs) — let the seed month be recomputed.
    bonus: float = 0.0
    other_allowance: float = 0.0
    pay_difference: float = 0.0
    provident_fund: float = 0.0
    loan: float = 0.0
    donations: float = 0.0
    other_deduction: float = 0.0
    welfare: float = 0.0


@dataclass
class SeedContext:
    client_company_id: int
    source_filename: str
    employees: list = field(default_factory=list)   # list[SeedEmployee]
    element_set: list = field(default_factory=lambda: list(ELEMENT_SET))
    warnings: list = field(default_factory=list)

    @property
    def icu_member_count(self):
        return sum(1 for e in self.employees if e.icu_member)

    @property
    def hourly_count(self):
        return sum(1 for e in self.employees if e.is_hourly)


def _text(value):
    if value is None:
        return ""
    return str(value).strip()


def parse_rich_workbook(source, client_company_id, source_filename=None) -> SeedContext:
    """Parse the RAW DATA sheet into a :class:`SeedContext`. ``source`` is a
    filesystem path *or* an already-open Workbook (see
    :func:`app.raw_engine.detection.load_raw_workbook`); passing a Workbook
    reuses a single load instead of re-opening the file.

    Raises :class:`~app.raw_engine.mapping.HeaderError` if the sheet is missing
    or its layout does not match the DZ template.
    """
    ws = open_raw_data_sheet(source)
    name_row = find_name_header_row(ws)
    validate_layout(ws, name_row)
    # Locate the employee master-data columns by header, not fixed position
    # (Book1 and the DZ specimen shift them); raises HeaderError if the
    # payment-critical fields can't be found, so a shifted workbook fails loud
    # instead of seeding a bank name into the SSNIT field (PMVP-05 Issue 3).
    master_cols = resolve_master_columns(ws, name_row)
    # Lump-adjustment columns: header-anchored with a fixed-position fallback, so
    # a future column shift can't silently mis-read pay without changing the
    # (currently-aligned) behaviour for existing workbooks.
    adj_cols = resolve_adjustment_columns(ws, name_row)

    def _master_text(row, field):
        col = master_cols.get(field)
        return _text(ws.cell(row, col).value) if col else ""

    def _master_num(row, field):
        col = master_cols.get(field)
        return coerce_rate(ws.cell(row, col).value) if col else 0.0

    def _adj_num(row, field, default_col):
        return coerce_rate(ws.cell(row, adj_cols.get(field, default_col)).value)

    fallback_name = (
        _text(source).rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
        if isinstance(source, str)
        else "raw_workbook.xlsx"
    )
    context = SeedContext(
        client_company_id=client_company_id,
        source_filename=source_filename or fallback_name,
    )
    seen = {}

    for r in range(name_row + 1, ws.max_row + 1):
        full_name = _text(ws.cell(r, COL_NAME).value)
        if not full_name:
            continue  # blank / spacer / total row

        staff_id = normalise_emp_id(ws.cell(r, COL_STAFF_KEY).value)
        if not staff_id or staff_id == "NAN":
            context.warnings.append(
                f"Row {r} ({full_name}): missing staff key — skipped. "
                "Assign a staff ID via a corrected rich upload."
            )
            continue
        if staff_id in seen:
            context.warnings.append(
                f"Duplicate staff ID {staff_id!r} (rows {seen[staff_id]} and {r}, "
                f"{full_name}) — later row wins."
            )
        seen[staff_id] = r

        daily_rate = coerce_rate(ws.cell(r, COL_DAILY_RATE).value)
        emp = SeedEmployee(
            staff_id=staff_id,
            full_name=full_name,
            basic_salary=coerce_rate(ws.cell(r, COL_BASIC_WAGE).value),
            icu_member=coerce_rate(ws.cell(r, COL_ICU_DUES).value) > 0,
            daily_rate=daily_rate,
            ghana_card_number=_master_text(r, "ghana_card"),
            ssnit_number=_master_text(r, "ssnit"),
            bank_name=_master_text(r, "bank"),
            bank_branch=_master_text(r, "branch"),
            bank_account_number=_master_text(r, "account_no"),
            department=_master_text(r, "department"),
            job_title=_master_text(r, "job_title"),
            tax_relief_monthly=_master_num(r, "tax_relief"),
            bonus=_adj_num(r, "bonus", COL_PROD_BONUS),
            other_allowance=_adj_num(r, "other_allowance", COL_OTHER_ALLOWANCE),
            pay_difference=_adj_num(r, "pay_difference", COL_PAY_DIFFERENCE),
            provident_fund=_adj_num(r, "provident_fund", COL_PROVIDENT),
            loan=_adj_num(r, "loan", COL_LOAN),
            donations=_adj_num(r, "donations", COL_DONATIONS),
            # OTHER DEDUCTION stays a fixed position: the DZ workbook carries two
            # identical 'OTHER DEDUCTION' headers, so it can't be disambiguated
            # by label (see resolve_adjustment_columns).
            other_deduction=coerce_rate(ws.cell(r, COL_OTHER_DEDUCTION).value),
            welfare=_adj_num(r, "welfare", COL_WELFARE),
        )

        for pay_code, label, category, hours_col, rate_col, _expected in ELEMENTS:
            rate = coerce_rate(ws.cell(r, rate_col).value)
            hours = coerce_hours(ws.cell(r, hours_col).value)
            if rate > 0:
                emp.rates.append(
                    RateSpec(pay_code=pay_code, category=category,
                             hourly_rate=rate, description=label)
                )
            if hours > 0:
                emp.raw_hours[pay_code] = hours

        # Hourly if the worker carries a per-employee rate table; otherwise a
        # flat-salary admin row (basic only, no rate rows) — derived from data,
        # not a hardcoded classification.
        emp.is_hourly = daily_rate > 0 or bool(emp.rates)
        context.employees.append(emp)

    return context
