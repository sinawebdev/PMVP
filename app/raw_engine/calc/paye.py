"""Ordinary PAYE on monthly taxable income — Ghana's graduated bands.

Bands come from the StatutoryRate version effective for the run's period (never
frozen at seed), so a run stays reproducible when rates change. Overtime and
bonus are taxed separately (see overtime_tax / bonus_tax) and never enter the
marginal bands here.
"""


def ordinary_paye(rate, taxable_income):
    """Marginal-band PAYE on ordinary taxable income for this rate version."""
    return rate.compute_paye(taxable_income)
