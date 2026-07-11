"""Net pay = gross - total tax - SSNIT(employee) - post-tax deductions.

Deductions summed here (verified against DZ cell BL): employee SSF, total tax
(ordinary PAYE + overtime + bonus), provident fund, ICU dues, loan, welfare,
donations and other. ``total_deductions`` is returned so ``gross -
total_deductions == net`` holds exactly.
"""
from app.money import money


def net_pay(
    gross_pay,
    total_tax,
    employee_ssnit,
    icu,
    provident_fund=0,
    loan=0,
    welfare=0,
    donations=0,
    other_deductions=0,
):
    """Returns ``(net_pay, total_deductions)``."""
    total_deductions = money(
        total_tax
        + employee_ssnit
        + icu
        + provident_fund
        + loan
        + welfare
        + donations
        + other_deductions
    )
    return money(gross_pay - total_deductions), total_deductions
