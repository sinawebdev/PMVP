"""Salaried (upload_type='standard') pay calculation.

The effective-dated deduction policy separates cash deductions from allowable
tax deductions and checks evidence for overtime concessions and tax relief:

  * Ordinary PAYE on taxable income (basic net of SSF, plus allowances, plus
    any bonus excess, and overtime that does not qualify for a concession,
    minus allowable PF and approved relief).
  * Eligible overtime: up to 50% of basic monthly salary at 5% flat,
    the excess at 10% flat, only when the selected policy verifies eligibility.
  * Bonus concession: up to 15% of ANNUAL basic salary at 5% flat; only the
    excess joins ordinary taxable income.

Historical policy version 0 reproduces two ACS rows (January 2026):

  AC605 (Sampson K. Kluvie) — no overtime (also verified against the
  "WAGE SHT" / "GRA PAYE" tabs for the derived fields):
    basic 2737.37 -> SSF 5.5% = 150.56 -> SSF 13% = 355.86
    -> net basic wage 2586.81 -> + transport 323.93 -> gross 3061.30
    - PF fund 100.00 -> taxable 2810.74 -> PAYE 382.63 -> net pay 2428.11
    annual salary 32848.44 (2737.37 x 12) -> 15% of annual 4927.27

  AC636 (David Kwame Tetteh) — heavy overtime:
    basic 1675.14, overtime 3125.30, transport 323.93, PF fund 100.00
    ordinary PAYE 206.96 + overtime tax 270.65 (41.88 @5% + 228.77 @10%)
    = total tax 477.61 -> net pay 4454.63

"PF FUND / EMPLOYEE" is always a cash deduction; its allowable tax deduction
is stored separately under policy version 1.
"""
from dataclasses import dataclass, field

from app.money import money as _r2  # 2dp Decimal ROUND_HALF_UP, stored as float
from app.payroll_calculations.deductions import deduction_adjustments


@dataclass
class SalariedResult:
    """PayrollItem-shaped output of one salaried calculation."""

    basic_salary: float
    transport_allowance: float
    housing_allowance: float
    medical_allowance: float
    meal_allowance: float       # "MEALS" (ACS column L) — taxable cash allowance
    productivity_bonus: float
    end_of_year_bonus: float    # one-off bonus; shares the annual concession cap
    other_allowances: float
    overtime_pay: float
    pay_difference: float       # arrears — taxed normally in this period
    gross_pay: float
    ssnit: float                # employee SSF (5.5% of basic)
    ssf_employer: float         # employer SSF (13% of basic) — not a payslip deduction
    net_basic_wage: float       # basic - employee SSF (derived, never uploaded)
    annual_salary: float        # basic x 12 (derived)
    annual_salary_15pct: float  # annual x bonus concession threshold (derived)
    taxable_income: float       # ordinary taxable income (includes bonus excess)
    ordinary_paye: float        # marginal-band tax on taxable_income
    overtime_tax: float         # concessionary flat-rate overtime tax
    bonus_tax: float            # concessionary flat-rate bonus tax
    bonus_excess: float         # bonus over the annual cap, joins taxable income
    paye: float                 # TOTAL tax = ordinary + overtime + bonus
    pf_fund_employee: float     # actual employee provident-fund cash deduction
    tax_relief_monthly: float   # requested relief; applied amount is separate
    loan_deduction: float
    loan_advance: float         # cash advanced to the worker — adds to net pay
    welfare_deduction: float    # post-tax (ACS column AC)
    iou_deduction: float        # post-tax (ACS column AE)
    other_deductions: float
    total_deductions: float
    net_pay: float
    pf_fund_employer: float = 0.0
    pensionable_salary: float = 0.0
    ssnit_remittance: float = 0.0
    tier_2_remittance: float = 0.0
    pf_allowable_employee: float = 0.0
    pf_employer_taxable: float = 0.0
    tax_relief_applied: float = 0.0
    overtime_ordinary_pay: float = 0.0
    deductions_policy_version: int = 0
    warnings: list = field(default_factory=list)

    def as_payroll_item_fields(self):
        """Kwargs for the PayrollItem fixed columns."""
        return {
            "basic_salary": self.basic_salary,
            "transport_allowance": self.transport_allowance,
            "housing_allowance": self.housing_allowance,
            "medical_allowance": self.medical_allowance,
            "meal_allowance": self.meal_allowance,
            "productivity_bonus": self.productivity_bonus,
            "end_of_year_bonus": self.end_of_year_bonus,
            "other_allowances": self.other_allowances,
            "overtime_pay": self.overtime_pay,
            "pay_difference": self.pay_difference,
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
            "loan_deduction": self.loan_deduction,
            "loan_advance": self.loan_advance,
            "welfare_deduction": self.welfare_deduction,
            "iou_deduction": self.iou_deduction,
            "other_deductions": self.other_deductions,
            "total_deductions": self.total_deductions,
            "net_pay": self.net_pay,
        }


class SalariedCalculator:
    """Computes one employee's monthly pay from a StatutoryRate version.

    All money inputs are this period's variable figures; the statutory maths
    (SSF split, PAYE bands) always comes from the rate version passed in.
    """

    def __init__(self, statutory_rate):
        self.rate = statutory_rate

    def calculate(
        self,
        basic_salary,
        *,
        transport_allowance=0,
        housing_allowance=0,
        medical_allowance=0,
        meal_allowance=0,
        productivity_bonus=0,
        end_of_year_bonus=0,
        other_allowances=0,
        overtime_pay=0,
        pay_difference=0,
        pf_fund_employee=0,
        pf_fund_employer=0,
        tax_relief_monthly=0,
        loan_deduction=0,
        loan_advance=0,
        welfare_deduction=0,
        iou_deduction=0,
        other_deductions=0,
        bonus_concession_used_ytd=0,
        calculation_context=None,
    ):
        """``pf_fund_employee`` ("PF FUND / EMPLOYEE", ACS RAW DATA column AA)
        is a cash deduction; only the documented allowable portion reduces
        taxable income under the selected deduction policy. Approved, in-date
        tax relief reduces ordinary taxable income only —
        it never touches the overtime/bonus concessionary tax and is not a
        cash deduction, so it does not appear in total_deductions.
        ``pay_difference`` (arrears) joins this period's taxable gross and is
        taxed normally. ``end_of_year_bonus`` shares the ANNUAL concession cap
        with ``productivity_bonus`` — the two are summed before the split.
        ``bonus_concession_used_ytd`` is bonus-concession cedis this employee
        already used in OTHER runs this tax year (see
        ``payroll_calculations.bonus_concession_used_ytd``) — the annual cap
        is enforced once across the year, not once per run.
        ``meal_allowance`` ("MEALS", ACS column L) is a taxable cash allowance
        like transport/medical. ``welfare_deduction`` (AC) and
        ``iou_deduction`` (AE) are post-tax like ``loan_deduction`` and
        ``other_deductions``; ``loan_advance`` is cash paid out with this
        payroll (opposite direction to the loan deduction: it adds to net pay
        and is never taxable income)."""
        basic_salary = _r2(basic_salary)
        transport_allowance = _r2(transport_allowance)
        housing_allowance = _r2(housing_allowance)
        medical_allowance = _r2(medical_allowance)
        meal_allowance = _r2(meal_allowance)
        productivity_bonus = _r2(productivity_bonus)
        end_of_year_bonus = _r2(end_of_year_bonus)
        other_allowances = _r2(other_allowances)
        overtime_pay = _r2(overtime_pay)
        pay_difference = _r2(pay_difference)
        pf_fund_employee = _r2(pf_fund_employee)
        pf_fund_employer = _r2(pf_fund_employer)
        tax_relief_monthly = _r2(tax_relief_monthly)
        loan_deduction = _r2(loan_deduction)
        loan_advance = _r2(loan_advance)
        welfare_deduction = _r2(welfare_deduction)
        iou_deduction = _r2(iou_deduction)
        other_deductions = _r2(other_deductions)

        # Previous unconditional pension calculation, retained for reference:
        # ssf_employee = _r2(basic_salary * self.rate.ssf_employee_rate)
        # ssf_employer = _r2(basic_salary * self.rate.ssf_employer_rate)
        adjustments = deduction_adjustments(
            self.rate,
            basic_salary,
            overtime_pay=overtime_pay,
            provident_fund=pf_fund_employee,
            employer_provident_fund=pf_fund_employer,
            requested_relief=tax_relief_monthly,
            context=calculation_context,
        )
        ssf_employee = adjustments.ssnit
        ssf_employer = adjustments.ssf_employer
        net_basic = _r2(basic_salary - ssf_employee)
        annual_salary = _r2(basic_salary * 12)
        annual_salary_15pct = _r2(
            annual_salary * self.rate.bonus_annual_basic_threshold
        )

        # Qualified concessionary components stay outside the marginal bands;
        # ordinary overtime and bonus excess enter ordinary taxable income.
        # Productivity and end-of-year bonuses share one annual cap — whatever
        # concession earlier runs already used (bonus_concession_used_ytd)
        # narrows what's left for this run.
        # Previous logic applied the overtime concession to every worker:
        # overtime_tax = self.rate.compute_overtime_tax(overtime_pay, basic_salary)
        overtime_tax = adjustments.overtime_tax
        bonus_tax, bonus_excess = self.rate.split_bonus(
            _r2(productivity_bonus + end_of_year_bonus),
            basic_salary,
            already_used=bonus_concession_used_ytd,
        )

        # Previous taxable base deducted the full uploaded PF and relief and
        # excluded all overtime, without approval or eligibility evidence:
        # taxable_income = _r2(net_basic - tax_relief_monthly
        #     + transport_allowance + housing_allowance + medical_allowance
        #     + meal_allowance + other_allowances + pay_difference + bonus_excess
        #     - pf_fund_employee)
        taxable_before_relief = _r2(
            net_basic
            + transport_allowance
            + housing_allowance
            + medical_allowance
            + meal_allowance
            + other_allowances
            + pay_difference
            + bonus_excess
            + adjustments.overtime_ordinary_pay
            + adjustments.pf_employer_taxable
            - adjustments.pf_allowable_employee
        )
        policy_version = getattr(self.rate, "deductions_policy_version", 0) or 0
        relief_applied = adjustments.tax_relief_applied
        if policy_version >= 1:
            relief_applied = min(relief_applied, max(0.0, taxable_before_relief))
            taxable_income = _r2(max(0.0, taxable_before_relief - relief_applied))
        else:
            taxable_income = _r2(taxable_before_relief - relief_applied)
        ordinary_paye = self.rate.compute_paye(taxable_income)
        paye = _r2(ordinary_paye + overtime_tax + bonus_tax)

        gross_pay = _r2(
            basic_salary
            + transport_allowance
            + housing_allowance
            + medical_allowance
            + meal_allowance
            + productivity_bonus
            + end_of_year_bonus
            + other_allowances
            + overtime_pay
            + pay_difference
        )
        total_deductions = _r2(
            ssf_employee
            + paye
            + pf_fund_employee
            + loan_deduction
            + welfare_deduction
            + iou_deduction
            + other_deductions
        )
        net_pay = _r2(gross_pay - total_deductions + loan_advance)

        return SalariedResult(
            basic_salary=basic_salary,
            transport_allowance=transport_allowance,
            housing_allowance=housing_allowance,
            medical_allowance=medical_allowance,
            meal_allowance=meal_allowance,
            productivity_bonus=productivity_bonus,
            end_of_year_bonus=end_of_year_bonus,
            other_allowances=other_allowances,
            overtime_pay=overtime_pay,
            pay_difference=pay_difference,
            gross_pay=gross_pay,
            ssnit=ssf_employee,
            ssf_employer=ssf_employer,
            net_basic_wage=net_basic,
            annual_salary=annual_salary,
            annual_salary_15pct=annual_salary_15pct,
            taxable_income=taxable_income,
            ordinary_paye=ordinary_paye,
            overtime_tax=overtime_tax,
            bonus_tax=bonus_tax,
            bonus_excess=bonus_excess,
            paye=paye,
            pf_fund_employee=pf_fund_employee,
            tax_relief_monthly=tax_relief_monthly,
            loan_deduction=loan_deduction,
            loan_advance=loan_advance,
            welfare_deduction=welfare_deduction,
            iou_deduction=iou_deduction,
            other_deductions=other_deductions,
            total_deductions=total_deductions,
            net_pay=net_pay,
            pf_fund_employer=pf_fund_employer,
            pensionable_salary=adjustments.pensionable_salary,
            ssnit_remittance=adjustments.ssnit_remittance,
            tier_2_remittance=adjustments.tier_2_remittance,
            pf_allowable_employee=adjustments.pf_allowable_employee,
            pf_employer_taxable=adjustments.pf_employer_taxable,
            tax_relief_applied=relief_applied,
            overtime_ordinary_pay=adjustments.overtime_ordinary_pay,
            deductions_policy_version=policy_version,
            warnings=list(adjustments.warnings),
        )

    def calculate_for_employee(self, employee, **period_inputs):
        """Convenience wrapper: basic salary from the roster record, variable
        inputs (allowances, overtime, bonus, deductions) per period."""
        return self.calculate(employee.basic_salary or 0, **period_inputs)
