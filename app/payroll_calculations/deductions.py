"""Evidence-based resident payroll deductions, shared by all three engines.

Official sources and deliberately unresolved pension/PF cases are recorded in
docs/research/gra-paye-2026-09-01/deductions/. This module never treats missing
eligibility data as proof of a tax concession. Version 0 is an explicit
historical compatibility policy, rather than a fallback for missing context.
"""

from dataclasses import dataclass, field
from datetime import date
from decimal import InvalidOperation
import math

from app.money import D, money


class DeductionPolicyError(ValueError):
    """The payroll needs an evidenced configuration before it can be costed."""


@dataclass
class DeductionContext:
    period: date = None
    is_junior: bool = None
    qualifying_annual_income: float = None
    income_year: int = None
    pf_scheme_approved: bool = False
    pf_scheme_reference: str = ""
    relief_approval_ref: str = ""
    relief_valid_from: date = None
    relief_valid_to: date = None
    approved_relief_monthly: float = 0.0


@dataclass
class DeductionAdjustments:
    ssnit: float = 0.0
    ssf_employer: float = 0.0
    pensionable_salary: float = 0.0
    ssnit_remittance: float = 0.0
    tier_2_remittance: float = 0.0
    pf_allowable_employee: float = 0.0
    pf_employer_taxable: float = 0.0
    tax_relief_applied: float = 0.0
    overtime_tax: float = 0.0
    overtime_ordinary_pay: float = 0.0
    warnings: list = field(default_factory=list)


def context_for_employee(employee, on_date):
    """Build policy inputs from roster evidence for this payroll's date.

    A saved monthly amount alone is not a GRA award. New awards are appended
    to history; select the latest recorded award valid for the actual period,
    including a zero-value record that explicitly revokes an earlier award.
    """
    context = DeductionContext(period=on_date)
    if employee is None:
        return context
    context.is_junior = employee.overtime_is_junior
    context.qualifying_annual_income = employee.overtime_qualifying_annual_income
    context.income_year = employee.overtime_income_year
    context.pf_scheme_approved = bool(employee.pf_scheme_approved)
    context.pf_scheme_reference = employee.pf_scheme_reference or ""
    awards = [
        award for award in employee.tax_relief_approvals
        if award.valid_from <= on_date <= award.valid_to
    ]
    if awards:
        award = max(awards, key=lambda row: row.id or 0)
        context.relief_approval_ref = award.reference or ""
        context.relief_valid_from = award.valid_from
        context.relief_valid_to = award.valid_to
        context.approved_relief_monthly = award.monthly_amount or 0
    elif not employee.tax_relief_approvals:
        # Explicit dated fields can support newly staged (unflushed) employees;
        # the unapproved legacy float is never promoted to an approval.
        context.relief_approval_ref = employee.tax_relief_approval_ref or ""
        context.relief_valid_from = employee.tax_relief_valid_from
        context.relief_valid_to = employee.tax_relief_valid_to
        context.approved_relief_monthly = employee.tax_relief_monthly or 0
    return context


def _amount(value, label):
    try:
        result = D(value or 0)
        if not result.is_finite() or result < 0:
            raise ValueError
        return result
    except (ValueError, TypeError, InvalidOperation):
        raise DeductionPolicyError(f"{label} must be a finite non-negative amount.")


def deduction_adjustments(
    rate, basic_salary, *, overtime_pay=0, provident_fund=0,
    employer_provident_fund=0, requested_relief=0, context=None,
):
    """Return the tax/cash distinction before computing ordinary PAYE.

    An employer/employee PF split above their combined exemption needs an
    evidenced allocation. Pension pay outside the published SSNIT range needs
    a confirmed allocation across tiers. Neither is guessed here.
    """
    basic = _amount(basic_salary, "Basic salary")
    overtime = _amount(overtime_pay, "Overtime")
    employee_pf = _amount(provident_fund, "Employee provident fund")
    employer_pf = _amount(employer_provident_fund, "Employer provident fund")
    requested = _amount(requested_relief, "Tax relief")
    result = DeductionAdjustments(pensionable_salary=money(basic))
    strict = int(getattr(rate, "deductions_policy_version", 0) or 0) >= 1

    if not strict:
        # Historical rules (previous calculator blocks):
        # ssnit = basic * rate.ssf_employee_rate
        # allowed_pf = employee_pf; relief = requested_relief
        # overtime_tax = rate.compute_overtime_tax(overtime, basic)
        result.ssnit = money(basic * D(rate.ssf_employee_rate))
        result.ssf_employer = money(basic * D(rate.ssf_employer_rate))
        result.pf_allowable_employee = money(employee_pf)
        result.tax_relief_applied = money(requested)
        result.overtime_tax = rate.compute_overtime_tax(overtime, basic)
        return result

    context = context or DeductionContext()
    period = context.period or getattr(rate, "effective_from", None)
    if basic > 0:
        minimum = D(getattr(rate, "ssnit_min_insurable", None) or "587.80")
        maximum = D(getattr(rate, "ssnit_max_insurable", None) or "69000")
        limits_year = int(getattr(rate, "ssnit_limits_year", None) or 2026)
        if period is None or period.year != limits_year:
            raise DeductionPolicyError(
                "Confirm SSNIT limits for this payroll year before calculating "
                "pension deductions. The configured notice covers "
                f"{limits_year}."
            )
        if basic < minimum or basic > maximum:
            raise DeductionPolicyError(
                f"Basic salary GHS {money(basic):,.2f} is outside the verified "
                f"{limits_year} SSNIT monthly range GHS {money(minimum):,.2f} "
                f"to {money(maximum):,.2f}. Confirm the pension treatment for "
                "this employee and period; the SSNIT notice alone does not "
                "establish the employee/employer or Tier 2 allocation."
            )
        if D(rate.ssf_employee_rate) != D("0.055") or D(rate.ssf_employer_rate) != D("0.13"):
            raise DeductionPolicyError(
                "The verified pension split is employee 5.5% and employer 13%. "
                "Confirm a separately documented allocation for other rates."
            )
        # Within the verified range the existing mandatory contributions stand.
        # The Tier 1/2 split is employer remittance, not a second cash deduction.
        result.ssnit = money(basic * D(rate.ssf_employee_rate))
        result.ssf_employer = money(basic * D(rate.ssf_employer_rate))
        result.ssnit_remittance = money(basic * D("0.135"))
        result.tier_2_remittance = money(basic * D("0.05"))
        minimum_remit = D(getattr(rate, "ssnit_min_monthly_remittance", None) or "79.40")
        if D(result.ssnit_remittance) < minimum_remit:
            raise DeductionPolicyError(
                "The computed SSNIT remittance is below the published minimum "
                f"GHS {money(minimum_remit):,.2f}. Confirm the minimum-contribution "
                "allocation before calculating; the notice's minimum earnings "
                "and remittance figures do not reconcile by simple rounding."
            )
        contribution_total = money(D(result.ssnit) + D(result.ssf_employer))
        remittance_total = money(D(result.ssnit_remittance) + D(result.tier_2_remittance))
        if contribution_total != remittance_total:
            result.warnings.append(
                "Pension component rounding needs reconciliation: employee plus "
                f"employer contributions total GHS {contribution_total:.2f}, "
                f"while separately rounded Tier 1 and Tier 2 total GHS {remittance_total:.2f}. "
                "No employee deduction or employer contribution was silently adjusted."
            )

    if employee_pf or employer_pf:
        documented_scheme = bool(
            context.pf_scheme_approved and context.pf_scheme_reference.strip()
        )
        if not documented_scheme:
            if employer_pf:
                raise DeductionPolicyError(
                    "Record the approved provident-fund scheme/reference before "
                    "calculating employer provident-fund contributions."
                )
            result.warnings.append(
                "Employee provident fund remains a cash deduction; no tax "
                "deduction applied because an approved scheme/reference is missing."
            )
        else:
            cap = basic * D(getattr(rate, "provident_fund_tax_limit", None) or "0.165")
            if employer_pf and employee_pf + employer_pf > cap:
                raise DeductionPolicyError(
                    "Combined employee/employer provident-fund contributions "
                    "exceed the 16.5% basic-salary tax exemption. Confirm the "
                    "approved taxable allocation; no contribution priority is assumed."
                )
            result.pf_allowable_employee = money(min(employee_pf, max(D(0), cap - employer_pf)))
            if employee_pf > cap and not employer_pf:
                result.warnings.append(
                    "Provident-fund cash deduction exceeds the tax exemption; "
                    "only the allowable amount reduces chargeable income."
                )

    approved = _amount(context.approved_relief_monthly, "Approved tax relief")
    valid_award = bool(
        period and context.relief_approval_ref.strip()
        and context.relief_valid_from and context.relief_valid_to
        and context.relief_valid_from <= period <= context.relief_valid_to
    )
    if valid_award:
        result.tax_relief_applied = money(approved)
    elif approved or requested:
        result.warnings.append(
            "Tax relief not applied: a GRA approval reference and dates valid "
            "for this payroll period are required."
        )

    if overtime:
        annual = context.qualifying_annual_income
        known_annual = (
            annual is not None and isinstance(annual, (int, float))
            and math.isfinite(annual) and annual >= 0
        )
        limit = D(getattr(rate, "overtime_qualifying_annual_limit", None) or "18000")
        qualifies = bool(
            context.is_junior is True and known_annual and period
            and context.income_year == period.year and D(annual) <= limit
        )
        if qualifies:
            result.overtime_tax = rate.compute_overtime_tax(overtime, basic)
        else:
            result.overtime_ordinary_pay = money(overtime)
            result.warnings.append(
                "Overtime included in ordinary PAYE: the junior-staff and "
                "qualifying annual-income conditions are not both evidenced "
                "for this payroll year."
            )
    return result
