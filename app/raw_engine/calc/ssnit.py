"""SSNIT (SSF): 5.5% employee / 13% employer of the basic wage.

Employer SSF is a Chrisnat cost, not a payslip deduction. Both rates come from
the effective-dated StatutoryRate, never a constant.
"""
from app.money import D, money


def employee_ssnit(rate, basic_wage):
    """Employee SSF contribution (deducted from pay)."""
    return money(D(basic_wage) * D(rate.ssf_employee_rate))


def employer_ssnit(rate, basic_wage):
    """Employer SSF contribution (Chrisnat cost, not a payslip deduction)."""
    return money(D(basic_wage) * D(rate.ssf_employer_rate))
