"""Salaried (upload_type='standard') pay calculation.

Total tax is THREE components summed, never one blended marginal figure —
overtime and bonus get concessionary flat-rate treatment under Ghana PAYE:

  * Ordinary PAYE on taxable income (basic net of SSF, plus allowances, plus
    any bonus excess over the concession cap, minus the pre-tax PF fund).
  * Overtime concession: up to 50% of basic monthly salary at 5% flat,
    the excess at 10% flat (rates live on StatutoryRate, not here).
  * Bonus concession: up to 15% of ANNUAL basic salary at 5% flat; only the
    excess joins ordinary taxable income.

Verified against two real ACS rows (January 2026):

  AC605 (Sampson K. Kluvie) — no overtime:
    basic 2737.37 -> SSF 5.5% = 150.56 -> net basic 2586.81
    + transport 323.93 - PF fund 100.00 -> taxable 2810.74
    -> PAYE 382.63 -> net pay 2428.11

  AC636 (David Kwame Tetteh) — heavy overtime:
    basic 1675.14, overtime 3125.30, transport 323.93, PF fund 100.00
    ordinary PAYE 206.96 + overtime tax 270.65 (41.88 @5% + 228.77 @10%)
    = total tax 477.61 -> net pay 4454.63

"PF FUND / EMPLOYEE" (ACS RAW DATA column AA) is deducted before PAYE.
"""
from dataclasses import dataclass

from app.money import money as _r2  # 2dp Decimal ROUND_HALF_UP, stored as float


@dataclass
class SalariedResult:
    """PayrollItem-shaped output of one salaried calculation."""

    basic_salary: float
    transport_allowance: float
    housing_allowance: float
    medical_allowance: float
    productivity_bonus: float
    other_allowances: float
    overtime_pay: float
    gross_pay: float
    ssnit: float                # employee SSF (5.5% of basic)
    ssf_employer: float         # employer SSF (13% of basic) — not a payslip deduction
    taxable_income: float       # ordinary taxable income (includes bonus excess)
    ordinary_paye: float        # marginal-band tax on taxable_income
    overtime_tax: float         # concessionary flat-rate overtime tax
    bonus_tax: float            # concessionary flat-rate bonus tax
    paye: float                 # TOTAL tax = ordinary + overtime + bonus
    pf_fund_employee: float     # pre-tax provident fund contribution
    tax_relief_monthly: float   # GRA relief subtracted from ordinary taxable income
    loan_deduction: float
    other_deductions: float
    total_deductions: float
    net_pay: float

    def as_payroll_item_fields(self):
        """Kwargs for the PayrollItem fixed columns (employer SSF and taxable
        income are calculation intermediates, not item columns)."""
        return {
            "basic_salary": self.basic_salary,
            "transport_allowance": self.transport_allowance,
            "housing_allowance": self.housing_allowance,
            "medical_allowance": self.medical_allowance,
            "productivity_bonus": self.productivity_bonus,
            "other_allowances": self.other_allowances,
            "overtime_pay": self.overtime_pay,
            "gross_pay": self.gross_pay,
            "paye": self.paye,
            "ssnit": self.ssnit,
            "pf_fund_employee": self.pf_fund_employee,
            "loan_deduction": self.loan_deduction,
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
        productivity_bonus=0,
        other_allowances=0,
        overtime_pay=0,
        pf_fund_employee=0,
        tax_relief_monthly=0,
        loan_deduction=0,
        other_deductions=0,
    ):
        """``pf_fund_employee`` ("PF FUND / EMPLOYEE", ACS RAW DATA column AA)
        is deducted before PAYE is applied — it reduces both taxable income and
        net pay. ``tax_relief_monthly`` (GRA marriage/dependents/disability/age
        relief, standing employee data) reduces ordinary taxable income only —
        it never touches the overtime/bonus concessionary tax and is not a
        cash deduction, so it does not appear in total_deductions.
        ``loan_deduction``/``other_deductions`` are post-tax."""
        basic_salary = _r2(basic_salary)
        transport_allowance = _r2(transport_allowance)
        housing_allowance = _r2(housing_allowance)
        medical_allowance = _r2(medical_allowance)
        productivity_bonus = _r2(productivity_bonus)
        other_allowances = _r2(other_allowances)
        overtime_pay = _r2(overtime_pay)
        pf_fund_employee = _r2(pf_fund_employee)
        tax_relief_monthly = _r2(tax_relief_monthly)
        loan_deduction = _r2(loan_deduction)
        other_deductions = _r2(other_deductions)

        ssf_employee = _r2(basic_salary * self.rate.ssf_employee_rate)
        ssf_employer = _r2(basic_salary * self.rate.ssf_employer_rate)
        net_basic = _r2(basic_salary - ssf_employee)

        # Concessionary components stay OUT of the marginal bands: overtime is
        # taxed flat entirely; only the bonus excess over the annual-basic cap
        # joins ordinary taxable income.
        overtime_tax = self.rate.compute_overtime_tax(overtime_pay, basic_salary)
        bonus_tax, bonus_excess = self.rate.split_bonus(productivity_bonus, basic_salary)

        taxable_income = _r2(
            net_basic
            - tax_relief_monthly
            + transport_allowance
            + housing_allowance
            + medical_allowance
            + other_allowances
            + bonus_excess
            - pf_fund_employee
        )
        ordinary_paye = self.rate.compute_paye(taxable_income)
        paye = _r2(ordinary_paye + overtime_tax + bonus_tax)

        gross_pay = _r2(
            basic_salary
            + transport_allowance
            + housing_allowance
            + medical_allowance
            + productivity_bonus
            + other_allowances
            + overtime_pay
        )
        total_deductions = _r2(
            ssf_employee + paye + pf_fund_employee + loan_deduction + other_deductions
        )
        net_pay = _r2(gross_pay - total_deductions)

        return SalariedResult(
            basic_salary=basic_salary,
            transport_allowance=transport_allowance,
            housing_allowance=housing_allowance,
            medical_allowance=medical_allowance,
            productivity_bonus=productivity_bonus,
            other_allowances=other_allowances,
            overtime_pay=overtime_pay,
            gross_pay=gross_pay,
            ssnit=ssf_employee,
            ssf_employer=ssf_employer,
            taxable_income=taxable_income,
            ordinary_paye=ordinary_paye,
            overtime_tax=overtime_tax,
            bonus_tax=bonus_tax,
            paye=paye,
            pf_fund_employee=pf_fund_employee,
            tax_relief_monthly=tax_relief_monthly,
            loan_deduction=loan_deduction,
            other_deductions=other_deductions,
            total_deductions=total_deductions,
            net_pay=net_pay,
        )

    def calculate_for_employee(self, employee, **period_inputs):
        """Convenience wrapper: basic salary from the roster record, variable
        inputs (allowances, overtime, bonus, deductions) per period."""
        return self.calculate(employee.basic_salary or 0, **period_inputs)
