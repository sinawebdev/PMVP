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
    COL_ACCOUNT_NO,
    COL_BANK,
    COL_BASIC_WAGE,
    COL_BRANCH,
    COL_DAILY_RATE,
    COL_DEPARTMENT,
    COL_GHANA_CARD,
    COL_ICU_DUES,
    COL_JOB_TITLE,
    COL_NAME,
    COL_SSNIT_NO,
    COL_STAFF_KEY,
    COL_TAX_RELIEF,
    ELEMENTS,
    ELEMENT_SET,
    find_name_header_row,
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


def parse_rich_workbook(path, client_company_id, source_filename=None) -> SeedContext:
    """Parse the RAW DATA sheet at ``path`` into a :class:`SeedContext`.

    Raises :class:`~app.raw_engine.mapping.HeaderError` if the sheet is missing
    or its layout does not match the DZ template.
    """
    ws = open_raw_data_sheet(path)
    name_row = find_name_header_row(ws)
    validate_layout(ws, name_row)

    context = SeedContext(
        client_company_id=client_company_id,
        source_filename=source_filename or _text(path).rsplit("/", 1)[-1],
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
            ghana_card_number=_text(ws.cell(r, COL_GHANA_CARD).value),
            ssnit_number=_text(ws.cell(r, COL_SSNIT_NO).value),
            bank_name=_text(ws.cell(r, COL_BANK).value),
            bank_branch=_text(ws.cell(r, COL_BRANCH).value),
            bank_account_number=_text(ws.cell(r, COL_ACCOUNT_NO).value),
            department=_text(ws.cell(r, COL_DEPARTMENT).value),
            job_title=_text(ws.cell(r, COL_JOB_TITLE).value),
            tax_relief_monthly=coerce_rate(ws.cell(r, COL_TAX_RELIEF).value),
            bonus=coerce_rate(ws.cell(r, COL_PROD_BONUS).value),
            other_allowance=coerce_rate(ws.cell(r, COL_OTHER_ALLOWANCE).value),
            pay_difference=coerce_rate(ws.cell(r, COL_PAY_DIFFERENCE).value),
            provident_fund=coerce_rate(ws.cell(r, COL_PROVIDENT).value),
            loan=coerce_rate(ws.cell(r, COL_LOAN).value),
            donations=coerce_rate(ws.cell(r, COL_DONATIONS).value),
            other_deduction=coerce_rate(ws.cell(r, COL_OTHER_DEDUCTION).value),
            welfare=coerce_rate(ws.cell(r, COL_WELFARE).value),
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
