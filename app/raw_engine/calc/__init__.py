"""Pure calculation layer for the raw-hours engine.

Each module owns one statutory rule and is a thin, side-effect-free wrapper over
the effective-dated :class:`~app.models.StatutoryRate` (the single source of
truth for the tax maths, shared with the standard engine) — never a hardcoded
rate. :func:`app.raw_engine.compute.compute_payslip` orchestrates them into a
full costed payslip that matches the DZ workbook's own formulas to the cent.
"""
from app.raw_engine.calc.bonus_tax import bonus_split
from app.raw_engine.calc.icu import icu_dues
from app.raw_engine.calc.net import net_pay
from app.raw_engine.calc.overtime_tax import overtime_tax
from app.raw_engine.calc.paye import ordinary_paye
from app.raw_engine.calc.rates import RateApplication, apply_rates
from app.raw_engine.calc.ssnit import employee_ssnit, employer_ssnit

__all__ = [
    "apply_rates",
    "RateApplication",
    "employee_ssnit",
    "employer_ssnit",
    "ordinary_paye",
    "overtime_tax",
    "bonus_split",
    "icu_dues",
    "net_pay",
]
