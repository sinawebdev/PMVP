"""Orchestrate the calc layer into a full costed payslip.

``compute_payslip(inputs, rate)`` turns one employee's costed components (basic
wage, overtime, allowances, bonus and the month's adjustments) plus the
effective-dated StatutoryRate into a :class:`Payslip` that matches the DZ
workbook's own columns to the cent:

  gross = basic + overtime + allowances + bonus + pay difference
  SSF employee 5.5% / employer 13% of basic
  taxable = (basic - SSF) + allowances + pay diff + ordinary overtime
            + bonus excess + taxable employer PF - approved relief - allowable PF
  ordinary PAYE on taxable (marginal bands)
  eligible overtime tax (concessionary) + bonus tax (flat within cap)
  ICU = 3% of basic for members, else 0
  net = gross - total tax - SSF employee - provident - ICU - loan - welfare
        - donations - other

Pure and side-effect free — no DB, no rounding surprises beyond the per-component
2dp discipline shared with the standard engine.
"""
from dataclasses import dataclass, field

from app.money import money
from app.payroll_calculations.deductions import DeductionContext, deduction_adjustments
from app.raw_engine.calc import (
    bonus_split,
    icu_dues,
    net_pay,
    ordinary_paye,
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
    provident_fund: float = 0.0      # cash deduction; tax allowance is separate
    is_icu_member: bool = False
    loan: float = 0.0
    welfare: float = 0.0
    donations: float = 0.0
    other_deductions: float = 0.0
    bonus_concession_used_ytd: float = 0.0
    employee_id: int = None
    context: DeductionContext | None = None
    pf_fund_employer: float = 0.0


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
            "pf_fund_employer": self.pf_fund_employer,
            "pensionable_salary": self.pensionable_salary,
            "ssnit_remittance": self.ssnit_remittance,
            "tier_2_remittance": self.tier_2_remittance,
            "pf_allowable_employee": self.pf_allowable_employee,
            "pf_employer_taxable": self.pf_employer_taxable,
            "tax_relief_applied": self.tax_relief_applied,
            "overtime_ordinary_pay": self.overtime_ordinary_pay,
            "deductions_policy_version": self.deductions_policy_version,
            "loan_deduction": self.loan,
            "welfare_deduction": self.welfare,
            "other_deductions": money(self.other_deductions + self.donations),
            "total_deductions": self.total_deductions,
            "net_pay": self.net_pay,
        }


def compute_payslip(inputs, rate):
    """Full costed payslip for ``inputs`` under StatutoryRate ``rate``."""
    basic = inputs.basic_wage

    # Previous unconditional pension calculation, retained for reference:
    # ee_ssf = employee_ssnit(rate, basic)
    # er_ssf = employer_ssnit(rate, basic)
    adjustments = deduction_adjustments(
        rate,
        basic,
        overtime_pay=inputs.overtime_pay,
        provident_fund=inputs.provident_fund,
        employer_provident_fund=inputs.pf_fund_employer,
        requested_relief=inputs.tax_relief_monthly,
        context=inputs.context,
    )
    ee_ssf = adjustments.ssnit
    er_ssf = adjustments.ssf_employer
    net_basic = money(basic - ee_ssf)
    annual = money(basic * 12)
    annual_15 = money(annual * rate.bonus_annual_basic_threshold)

    # Previous logic gave every worker the overtime concession:
    # ot_tax = overtime_tax(rate, inputs.overtime_pay, basic)
    ot_tax = adjustments.overtime_tax
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
    # Previous taxable base trusted all uploaded relief and PF deductions:
    # taxable = money(net_basic + inputs.allowances + inputs.pay_difference
    #     - inputs.tax_relief_monthly - inputs.provident_fund + b_excess)
    taxable_before_relief = money(
        net_basic
        + inputs.allowances
        + inputs.pay_difference
        - adjustments.pf_allowable_employee
        + b_excess
        + adjustments.overtime_ordinary_pay
        + adjustments.pf_employer_taxable
    )
    policy_version = getattr(rate, "deductions_policy_version", 0) or 0
    relief_applied = adjustments.tax_relief_applied
    if policy_version >= 1:
        relief_applied = min(relief_applied, max(0.0, taxable_before_relief))
        taxable = money(max(0.0, taxable_before_relief - relief_applied))
    else:
        taxable = money(taxable_before_relief - relief_applied)
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
        pf_fund_employer=money(inputs.pf_fund_employer),
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
