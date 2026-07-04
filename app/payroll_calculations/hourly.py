"""Hourly/shift (upload_type='raw') pay calculation.

Replaces the manual step where an operator computed gross pay by hand after a
raw-hours import: each RawPayEntry (employee, pay code, hours) is multiplied
by the matching WageRateProfile rate, then taxed per the pay code's category —
the same three-component treatment as the salaried path (verified against
DZ's own formulas, AR44/AU47/AY51):

  * 'basic' lines form the basic wage: attract employee/employer SSF and
    ordinary marginal PAYE.
  * 'overtime' lines (weekday + Saturday + Sunday/holiday OT summed) get the
    concessionary flat rates — low rate up to the threshold fraction of the
    basic wage, high rate on the excess — never the marginal bands.
  * 'bonus' lines get the flat bonus rate up to the annual-basic cap; only
    the excess joins ordinary taxable income.
  * 'allowance' lines (afternoon, night, 6-to-6, 4-crew shift allowances)
    are ordinary taxable income with no SSF and no concession.

Regression fixture: Richard Woode (DCL9) — basic 631.35, overtime 978.59,
bonus 120.97, shift allowances 315.68; see tests/test_calculations.py.

Pay codes with no configured rate are never guessed — they are collected on
the result as ``missing_rate_codes`` so the operator can add the rate and
recalculate.
"""
from dataclasses import dataclass, field

from app.models import Employee, RawPayEntry, WageRateProfile
from app.money import money as _r2  # 2dp Decimal ROUND_HALF_UP, stored as float
from app.raw_import import normalise_emp_id


@dataclass
class HourlyResult:
    """One employee's computed pay for a raw run."""

    employee_id_str: str
    employee_id: int | None          # roster Employee.id when resolvable
    tax_relief_monthly: float = 0.0  # GRA relief from the roster record
    lines: list = field(default_factory=list)  # (pay_code, category, hours, rate, amount)
    missing_rate_codes: list = field(default_factory=list)
    basic_wage: float = 0.0
    overtime_pay: float = 0.0
    bonus: float = 0.0
    allowances: float = 0.0
    gross_pay: float = 0.0
    ssnit: float = 0.0               # employee SSF (on basic wage only)
    ssf_employer: float = 0.0
    net_basic_wage: float = 0.0      # basic wage - employee SSF (derived)
    annual_salary: float = 0.0       # basic wage x 12 (derived)
    annual_salary_15pct: float = 0.0 # annual x bonus concession threshold (derived)
    taxable_income: float = 0.0      # ordinary taxable income (includes bonus excess)
    ordinary_paye: float = 0.0
    overtime_tax: float = 0.0
    bonus_tax: float = 0.0
    bonus_excess: float = 0.0        # bonus over the annual cap, joins taxable income
    paye: float = 0.0                # TOTAL tax = ordinary + overtime + bonus
    total_deductions: float = 0.0
    net_pay: float = 0.0

    def as_payroll_item_fields(self):
        return {
            "basic_salary": self.basic_wage,
            "overtime_pay": self.overtime_pay,
            "productivity_bonus": self.bonus,
            "other_allowances": self.allowances,
            "gross_pay": self.gross_pay,
            "paye": self.paye,
            "ssnit": self.ssnit,
            "ssf_employer": self.ssf_employer,
            "net_basic_wage": self.net_basic_wage,
            "annual_salary": self.annual_salary,
            "annual_salary_15pct": self.annual_salary_15pct,
            "taxable_income": self.taxable_income,
            "overtime_tax": self.overtime_tax,
            "bonus_tax": self.bonus_tax,
            "bonus_excess": self.bonus_excess,
            "total_deductions": self.total_deductions,
            "net_pay": self.net_pay,
        }


class HourlyShiftCalculator:
    """Computes gross and net pay for every employee in a raw-hours run."""

    _CATEGORY_FIELDS = {
        WageRateProfile.CATEGORY_BASIC: "basic_wage",
        WageRateProfile.CATEGORY_OVERTIME: "overtime_pay",
        WageRateProfile.CATEGORY_BONUS: "bonus",
        WageRateProfile.CATEGORY_ALLOWANCE: "allowances",
    }

    def __init__(self, payroll_run, statutory_rate):
        self.run = payroll_run
        self.rate = statutory_rate

    def _roster(self):
        """Normalised staff_id -> (Employee.id, tax_relief_monthly) for the
        run's client roster."""
        employees = Employee.query.filter_by(
            client_company_id=self.run.client_company_id
        ).all()
        return {
            normalise_emp_id(e.staff_id): (e.id, float(e.tax_relief_monthly or 0))
            for e in employees
        }

    def calculate_run(self):
        """Returns {employee_id_str: HourlyResult} for every worker with
        imported hours in this run."""
        entries = RawPayEntry.query.filter_by(payroll_run_id=self.run.id).all()
        roster = self._roster()
        results = {}

        for entry in entries:
            emp_key = entry.employee_id_str
            result = results.get(emp_key)
            if result is None:
                employee_id, relief = roster.get(
                    normalise_emp_id(emp_key), (None, 0.0)
                )
                result = HourlyResult(
                    employee_id_str=emp_key,
                    employee_id=employee_id,
                    tax_relief_monthly=relief,
                )
                results[emp_key] = result

            profile = WageRateProfile.profile_for(
                self.run.client_company_id, result.employee_id, entry.pay_code
            )
            if profile is None:
                if entry.pay_code not in result.missing_rate_codes:
                    result.missing_rate_codes.append(entry.pay_code)
                continue

            amount = _r2(float(entry.hours) * profile.hourly_rate)
            result.lines.append(
                (entry.pay_code, profile.category, float(entry.hours),
                 profile.hourly_rate, amount)
            )
            bucket = self._CATEGORY_FIELDS.get(profile.category, "allowances")
            setattr(result, bucket, _r2(getattr(result, bucket) + amount))
            result.gross_pay = _r2(result.gross_pay + amount)

        for result in results.values():
            self._apply_statutory(result)
        return results

    def _apply_statutory(self, result):
        """Same three-component tax chain as the salaried path: SSF on the
        basic wage, concessionary overtime/bonus tax, ordinary PAYE on the
        rest (verified against the Richard Woode DZ fixture)."""
        from app.payroll_calculations import bonus_concession_used_ytd

        result.ssnit = _r2(result.basic_wage * self.rate.ssf_employee_rate)
        result.ssf_employer = _r2(result.basic_wage * self.rate.ssf_employer_rate)
        result.net_basic_wage = _r2(result.basic_wage - result.ssnit)
        result.annual_salary = _r2(result.basic_wage * 12)
        result.annual_salary_15pct = _r2(
            result.annual_salary * self.rate.bonus_annual_basic_threshold
        )

        result.overtime_tax = self.rate.compute_overtime_tax(
            result.overtime_pay, result.basic_wage
        )
        # Annual bonus concession cap, enforced once per tax year: subtract
        # whatever concession this employee's OTHER runs already used.
        used_ytd = bonus_concession_used_ytd(
            result.employee_id, self.run.year, exclude_run_id=self.run.id
        )
        result.bonus_tax, bonus_excess = self.rate.split_bonus(
            result.bonus, result.basic_wage, already_used=used_ytd
        )
        result.bonus_excess = bonus_excess

        result.taxable_income = _r2(
            result.basic_wage
            - result.ssnit
            - result.tax_relief_monthly
            + result.allowances
            + bonus_excess
        )
        result.ordinary_paye = self.rate.compute_paye(result.taxable_income)
        result.paye = _r2(
            result.ordinary_paye + result.overtime_tax + result.bonus_tax
        )
        result.total_deductions = _r2(result.ssnit + result.paye)
        result.net_pay = _r2(result.gross_pay - result.total_deductions)
