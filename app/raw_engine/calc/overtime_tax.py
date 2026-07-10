"""Concessionary overtime tax: low rate up to half the basic wage, high rate on
the excess (Ghana's overtime concession) — never the marginal bands.

Verified against DZ cell AU: OT 978.59 on basic 631.35 -> 82.08.
"""


def overtime_tax(rate, overtime_pay, basic_wage):
    """Concessionary tax on overtime pay for this rate version."""
    return rate.compute_overtime_tax(overtime_pay, basic_wage)
