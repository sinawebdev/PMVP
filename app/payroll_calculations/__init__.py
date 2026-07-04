"""Payroll calculation services.

Two calculators mirror the two client shapes that exist today:
  * SalariedCalculator  — PayrollRun.upload_type == 'standard' (monthly salary)
  * HourlyShiftCalculator — PayrollRun.upload_type == 'raw' (imported hours × rates)

Both pull SSF/PAYE from the StatutoryRate version active for the run's period,
never from constants, so past runs stay reproducible when rates change.
"""
import calendar
from datetime import date

from app.models import StatutoryRate

_MONTH_NUMBERS = {name: idx for idx, name in enumerate(calendar.month_name) if name}
_MONTH_NUMBERS.update(
    {name: idx for idx, name in enumerate(calendar.month_abbr) if name}
)


def period_start(month, year):
    """First day of a payroll run's period ("January", 2026) -> date(2026, 1, 1)."""
    month_number = _MONTH_NUMBERS.get(str(month).strip().capitalize())
    if not month_number:
        raise ValueError(f"Unrecognised payroll month name: {month!r}")
    return date(int(year), month_number, 1)


def statutory_rate_for_run(payroll_run):
    """The StatutoryRate version in force for the run's period, or raise."""
    on_date = period_start(payroll_run.month, payroll_run.year)
    rate = StatutoryRate.active_for(on_date)
    if rate is None:
        raise LookupError(
            f"No statutory rate version is effective on {on_date.isoformat()}. "
            "Add one under Statutory Rates before calculating this run."
        )
    return rate


def bonus_concession_used_ytd(employee_id, year, exclude_run_id=None):
    """Bonus-concession cedis this employee already used in OTHER FINALIZED
    payroll runs within ``year``, so the 15%-of-annual-basic cap is enforced
    once per tax year, not once per run. Only Approved/Processed runs count —
    Draft, Pending Approval, and Rejected runs never actually paid the
    employee, so their bonus figures must not eat into the annual cap. Per
    item, the concession cedis actually applied is
    ``productivity_bonus + end_of_year_bonus - bonus_excess`` — the same
    arithmetic ``StatutoryRate.split_bonus`` used to produce that item's
    stored bonus_excess in the first place."""
    from app.models import PayrollItem, PayrollRun
    from app.payroll_status import CLOSED_STATUSES

    if not employee_id:
        return 0.0

    query = (
        PayrollItem.query.join(PayrollRun, PayrollItem.payroll_run_id == PayrollRun.id)
        .filter(
            PayrollItem.employee_id == employee_id,
            PayrollRun.year == year,
            PayrollRun.status.in_(CLOSED_STATUSES),
        )
    )
    if exclude_run_id is not None:
        query = query.filter(PayrollRun.id != exclude_run_id)

    return round(
        sum(
            (item.productivity_bonus or 0)
            + (item.end_of_year_bonus or 0)
            - (item.bonus_excess or 0)
            for item in query.all()
        ),
        2,
    )
