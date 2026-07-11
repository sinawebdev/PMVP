"""Bonus final tax: flat rate up to the annual-basic cap; the excess spills into
ordinary taxable income (Ghana's bonus rule).

``already_used`` is bonus-concession cedis this employee consumed in other
finalized runs this tax year, so the cap is enforced once per year, not per run.
Verified against DZ cell AY: bonus 120.97 within the cap -> 6.05 flat.
"""


def bonus_split(rate, bonus, basic_wage, already_used=0):
    """(concessionary tax, excess-into-taxable-income) for a one-off bonus."""
    return rate.split_bonus(bonus, basic_wage, already_used=already_used)
