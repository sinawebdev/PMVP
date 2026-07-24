"""Orchestrate the calc layer into a full costed payslip.

``compute_payslip(inputs, rate)`` turns one employee's costed components (basic
wage, overtime, allowances, bonus and the month's adjustments) plus the
effective-dated StatutoryRate into a :class:`Payslip` that matches the DZ
workbook's own columns to the cent:

  gross = basic + overtime + allowances + bonus + pay difference
  SSF employee 5.5% / employer 13% of basic
  taxable = (basic - SSF) + allowances + pay diff - relief - provident + bonus excess
  ordinary PAYE on taxable (marginal bands)
  overtime tax (concessionary) + bonus tax (flat within cap)
  ICU = 3% of basic for members, else 0
  net = gross - total tax - SSF employee - provident - ICU - loan - welfare
        - donations - other

Pure and side-effect free — no DB, no rounding surprises beyond the per-component
2dp discipline shared with the standard engine.
"""
from dataclasses import dataclass

from app.money import money
from app.raw_engine.calc import (
    bonus_split,
    employee_ssnit,
    employer_ssnit,
    icu_dues,
    net_pay,
    ordinary_paye,
    overtime_tax,
)


@dataclass
class PayslipInputs:
    """One employee's costed inputs for a month. ``basic_wage``, ``overtime_pay``
    and ``allowances`` are already money (normal/OT hours x rate + shift
    allowances); ``bonus`` and the deductions are the month's lump adjustments."""

    staff_id: str
    basic_wage: float = 0.0
    overtime_pay: float = 0.0
    allowances: float = 0.0          # shift allowances + other allowance
    bonus: float = 0.0               # prod / bonus lump
    pay_difference: float = 0.0
    tax_relief_monthly: float = 0.0
    provident_fund: float = 0.0      # pre-tax; also a net deduction
    is_icu_member: bool = False
    loan: float = 0.0
    welfare: float = 0.0
    donations: float = 0.0
    other_deductions: float = 0.0
    bonus_concession_used_ytd: float = 0.0
    employee_id: int = None


@dataclass
class Payslip:
    staff_id: str
    employee_id: int = None
    basic_wage: float = 0.0
    employee_ssnit: float = 0.0
    employer_ssnit: float = 0.0
    net_basic_wage: float = 0.0
    annual_salary: float = 0.0
    annual_salary_15pct: float = 0.0
    overtime_pay: float = 0.0
    allowances: float = 0.0
    bonus: float = 0.0
    pay_difference: float = 0.0
    gross_pay: float = 0.0
    taxable_income: float = 0.0
    ordinary_paye: float = 0.0
    overtime_tax: float = 0.0
    bonus_tax: float = 0.0
    bonus_excess: float = 0.0
    total_tax: float = 0.0           # ordinary + overtime + bonus
    icu: float = 0.0
    provident_fund: float = 0.0
    loan: float = 0.0
    welfare: float = 0.0
    donations: float = 0.0
    other_deductions: float = 0.0
    total_deductions: float = 0.0
    net_pay: float = 0.0

    def as_payroll_item_fields(self):
        """Map onto PayrollItem columns (reused store — distribution/payslip
        code stays untouched). Donations have no dedicated column, so they fold
        into other_deductions for storage; the net figure already accounts for
        them separately."""
        return {
            "basic_salary": self.basic_wage,
            "overtime_pay": self.overtime_pay,
            "overtime_source": "computed",
            "productivity_bonus": self.bonus,
            "other_allowances": self.allowances,
            "pay_difference": self.pay_difference,
            "gross_pay": self.gross_pay,
            "paye": self.total_tax,
            "ssnit": self.employee_ssnit,
            "ssf_employer": self.employer_ssnit,
            "net_basic_wage": self.net_basic_wage,
            "annual_salary": self.annual_salary,
            "annual_salary_15pct": self.annual_salary_15pct,
            "taxable_income": self.taxable_income,
            "overtime_tax": self.overtime_tax,
            "bonus_tax": self.bonus_tax,
            "bonus_excess": self.bonus_excess,
            "icu_dues": self.icu,
            "pf_fund_employee": self.provident_fund,
            "loan_deduction": self.loan,
            "welfare_deduction": self.welfare,
            "other_deductions": money(self.other_deductions + self.donations),
            "total_deductions": self.total_deductions,
            "net_pay": self.net_pay,
        }


def compute_payslip(inputs, rate):
    """Full costed payslip for ``inputs`` under StatutoryRate ``rate``."""
    basic = inputs.basic_wage

    ee_ssf = employee_ssnit(rate, basic)
    er_ssf = employer_ssnit(rate, basic)
    net_basic = money(basic - ee_ssf)
    annual = money(basic * 12)
    annual_15 = money(annual * rate.bonus_annual_basic_threshold)

    ot_tax = overtime_tax(rate, inputs.overtime_pay, basic)
    b_tax, b_excess = bonus_split(
        rate, inputs.bonus, basic, already_used=inputs.bonus_concession_used_ytd
    )

    gross = money(
        basic
        + inputs.overtime_pay
        + inputs.allowances
        + inputs.bonus
        + inputs.pay_difference
    )
    taxable = money(
        net_basic
        + inputs.allowances
        + inputs.pay_difference
        - inputs.tax_relief_monthly
        - inputs.provident_fund
        + b_excess
    )
    ord_paye = ordinary_paye(rate, taxable)
    total_tax = money(ord_paye + ot_tax + b_tax)

    icu = icu_dues(rate, basic, inputs.is_icu_member)

    net, total_ded = net_pay(
        gross,
        total_tax,
        ee_ssf,
        icu,
        provident_fund=inputs.provident_fund,
        loan=inputs.loan,
        welfare=inputs.welfare,
        donations=inputs.donations,
        other_deductions=inputs.other_deductions,
    )

    return Payslip(
        staff_id=inputs.staff_id,
        employee_id=inputs.employee_id,
        basic_wage=basic,
        employee_ssnit=ee_ssf,
        employer_ssnit=er_ssf,
        net_basic_wage=net_basic,
        annual_salary=annual,
        annual_salary_15pct=annual_15,
        overtime_pay=money(inputs.overtime_pay),
        allowances=money(inputs.allowances),
        bonus=money(inputs.bonus),
        pay_difference=money(inputs.pay_difference),
        gross_pay=gross,
        taxable_income=taxable,
        ordinary_paye=ord_paye,
        overtime_tax=ot_tax,
        bonus_tax=b_tax,
        bonus_excess=b_excess,
        total_tax=total_tax,
        icu=icu,
        provident_fund=money(inputs.provident_fund),
        loan=money(inputs.loan),
        welfare=money(inputs.welfare),
        donations=money(inputs.donations),
        other_deductions=money(inputs.other_deductions),
        total_deductions=total_ded,
        net_pay=net,
    )
