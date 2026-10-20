"""Hourly/shift (upload_type='raw') pay calculation.

Replaces the manual step where an operator computed gross pay by hand after a
raw-hours import: each RawPayEntry (employee, pay code, hours) is multiplied
by the matching WageRateProfile rate, then taxed per the pay code's category —
the same three-component treatment as the salaried path (verified against
DZ's own formulas, AR44/AU47/AY51):

  * 'basic' lines form the basic wage: attract employee/employer SSF and
    ordinary marginal PAYE.
  * 'overtime' lines use the concessionary flat rates only when eligibility
    is evidenced under the selected deduction policy; otherwise they join
    ordinary taxable income.
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
from sqlalchemy.orm import selectinload

from app.models import Employee, PayrollItem, RawPayEntry, WageRateProfile
from app.money import money as _r2  # 2dp Decimal ROUND_HALF_UP, stored as float
from app.payroll_calculations import period_start
from app.payroll_calculations.deductions import (
    DeductionContext, context_for_employee, deduction_adjustments,
)
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
    pf_fund_employee: float = 0.0
    pf_fund_employer: float = 0.0
    pensionable_salary: float = 0.0
    ssnit_remittance: float = 0.0
    tier_2_remittance: float = 0.0
    pf_allowable_employee: float = 0.0
    pf_employer_taxable: float = 0.0
    tax_relief_applied: float = 0.0
    overtime_ordinary_pay: float = 0.0
    deductions_policy_version: int = 0
    context: DeductionContext | None = None
    warnings: list = field(default_factory=list)

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
            "pf_fund_employee": self.pf_fund_employee,
            "pf_fund_employer": self.pf_fund_employer,
            "pensionable_salary": self.pensionable_salary,
            "ssnit_remittance": self.ssnit_remittance,
            "tier_2_remittance": self.tier_2_remittance,
            "pf_allowable_employee": self.pf_allowable_employee,
            "pf_employer_taxable": self.pf_employer_taxable,
            "tax_relief_applied": self.tax_relief_applied,
            "overtime_ordinary_pay": self.overtime_ordinary_pay,
            "deductions_policy_version": self.deductions_policy_version,
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
        """Normalised staff_id -> stored employee and approval metadata."""
        employees = Employee.query.filter_by(
            client_company_id=self.run.client_company_id
        ).options(selectinload(Employee.tax_relief_approvals)).all()
        return {
            normalise_emp_id(e.staff_id): e
            for e in employees
        }

    def calculate_run(self):
        """Returns {employee_id_str: HourlyResult} for every worker with
        imported hours in this run."""
        entries = RawPayEntry.query.filter_by(payroll_run_id=self.run.id).all()
        roster = self._roster()
        on_date = period_start(self.run.month, self.run.year)
        # Cash PF inputs belong to this run; never carry a prior month's cash
        # deduction into a new hourly run or infer approval from its amount.
        items = PayrollItem.query.filter_by(payroll_run_id=self.run.id).all()
        items_by_employee = {item.employee_id: item for item in items if item.employee_id}
        items_by_staff = {normalise_emp_id(item.staff_id): item for item in items if item.staff_id}
        results = {}

        for entry in entries:
            emp_key = entry.employee_id_str
            result = results.get(emp_key)
            if result is None:
                # Previous roster tuple trusted the undated relief amount:
                # employee_id, relief = roster.get(normalise_emp_id(emp_key), (None, 0.0))
                employee = roster.get(normalise_emp_id(emp_key))
                employee_id = employee.id if employee is not None else None
                relief = float(employee.tax_relief_monthly or 0) if employee is not None else 0.0
                period_item = items_by_employee.get(employee_id) or items_by_staff.get(
                    normalise_emp_id(emp_key)
                )
                result = HourlyResult(
                    employee_id_str=emp_key,
                    employee_id=employee_id,
                    tax_relief_monthly=relief,
                    pf_fund_employee=float(period_item.pf_fund_employee or 0) if period_item else 0.0,
                    pf_fund_employer=float(period_item.pf_fund_employer or 0) if period_item else 0.0,
                    context=(context_for_employee(employee, on_date) if employee is not None
                             else DeductionContext(period=on_date)),
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
        """Apply the shared deduction policy before ordinary and flat taxes.
        Historical version 0 retains the original client workbook figures."""
        from app.payroll_calculations import bonus_concession_used_ytd

        # Previous unconditional pension calculation:
        # result.ssnit = _r2(result.basic_wage * self.rate.ssf_employee_rate)
        # result.ssf_employer = _r2(result.basic_wage * self.rate.ssf_employer_rate)
        adjustments = deduction_adjustments(
            self.rate,
            result.basic_wage,
            overtime_pay=result.overtime_pay,
            provident_fund=result.pf_fund_employee,
            employer_provident_fund=result.pf_fund_employer,
            requested_relief=result.tax_relief_monthly,
            context=result.context,
        )
        result.ssnit = adjustments.ssnit
        result.ssf_employer = adjustments.ssf_employer
        result.pensionable_salary = adjustments.pensionable_salary
        result.ssnit_remittance = adjustments.ssnit_remittance
        result.tier_2_remittance = adjustments.tier_2_remittance
        result.pf_allowable_employee = adjustments.pf_allowable_employee
        result.pf_employer_taxable = adjustments.pf_employer_taxable
        result.overtime_ordinary_pay = adjustments.overtime_ordinary_pay
        result.warnings = list(adjustments.warnings)
        result.deductions_policy_version = getattr(self.rate, "deductions_policy_version", 0) or 0
        result.net_basic_wage = _r2(result.basic_wage - result.ssnit)
        result.annual_salary = _r2(result.basic_wage * 12)
        result.annual_salary_15pct = _r2(
            result.annual_salary * self.rate.bonus_annual_basic_threshold
        )

        # Previous logic applied the overtime concession unconditionally:
        # result.overtime_tax = self.rate.compute_overtime_tax(result.overtime_pay, result.basic_wage)
        result.overtime_tax = adjustments.overtime_tax
        # Annual bonus concession cap, enforced once per tax year: subtract
        # whatever concession this employee's OTHER runs already used.
        used_ytd = bonus_concession_used_ytd(
            result.employee_id, self.run.year, exclude_run_id=self.run.id
        )
        result.bonus_tax, bonus_excess = self.rate.split_bonus(
            result.bonus, result.basic_wage, already_used=used_ytd
        )
        result.bonus_excess = bonus_excess

        # Previous taxable base trusted undated relief and excluded all OT:
        # result.taxable_income = _r2(result.basic_wage - result.ssnit
        #     - result.tax_relief_monthly + result.allowances + bonus_excess)
        taxable_before_relief = _r2(
            result.basic_wage
            - result.ssnit
            + result.allowances
            + bonus_excess
            + adjustments.overtime_ordinary_pay
            + adjustments.pf_employer_taxable
            - adjustments.pf_allowable_employee
        )
        result.tax_relief_applied = adjustments.tax_relief_applied
        if result.deductions_policy_version >= 1:
            result.tax_relief_applied = min(
                result.tax_relief_applied, max(0.0, taxable_before_relief)
            )
            result.taxable_income = _r2(max(0.0, taxable_before_relief - result.tax_relief_applied))
        else:
            result.taxable_income = _r2(taxable_before_relief - result.tax_relief_applied)
        result.ordinary_paye = self.rate.compute_paye(result.taxable_income)
        result.paye = _r2(
            result.ordinary_paye + result.overtime_tax + result.bonus_tax
        )
        # Previous net calculation omitted current-run PF cash deductions:
        # result.total_deductions = _r2(result.ssnit + result.paye)
        result.total_deductions = _r2(result.ssnit + result.paye + result.pf_fund_employee)
        result.net_pay = _r2(result.gross_pay - result.total_deductions)
