"""Characterization / parity test: the standard SalariedCalculator and the raw
Hours engine's compute_payslip must agree, component for component, on the same
monetary inputs.

This is the safeguard called for before the old calculator (and its PAYE table)
can be retired (checklist item F): don't delete proven logic until an equality
test pins the new engine to it across a spread of cases — zero, salaried
basic-only, heavy overtime, a top-band (35%) earner, a bonus over the annual
concession cap, and an ICU union member.

Both engines resolve their statutory maths through the SAME effective-dated
StatutoryRate primitives (compute_paye / ssf rates / compute_overtime_tax /
split_bonus / icu_dues), so agreement should be exact to the pesewa. The one
deliberate asymmetry is ICU union dues: they exist only in the raw engine, so
for a member the raw net is the standard net minus exactly that 3%-of-basic
deduction — asserted explicitly rather than ignored.
"""
import os
import unittest
from datetime import date

os.environ["DATABASE_URL"] = "sqlite:///:memory:"

from app import create_app, db
from app.models import StatutoryRate
from app.payroll_calculations.salaried import SalariedCalculator
from app.raw_engine.compute import PayslipInputs, compute_payslip


# Each scenario maps to a single set of monetary inputs fed to BOTH engines. All
# allowances are folded into one field per engine so the two are directly
# comparable (the standard engine splits transport/housing/… but sums them the
# same way the raw engine sums its single `allowances`).
SCENARIOS = [
    # id, basic, extra inputs
    ("zero_everything", 0.0, {}),
    ("salaried_basic_only", 2737.37, {"allowances": 323.93, "pf": 100.0}),
    ("heavy_overtime", 1675.14, {"overtime": 3125.30, "allowances": 323.93, "pf": 100.0}),
    ("top_band_35pct", 60000.0, {"allowances": 500.0}),
    ("bonus_over_annual_cap", 3000.0, {"bonus": 9000.0}),  # 9000 >> 15% of 36000 = 5400
    ("icu_member", 1800.0, {"icu_member": True}),
    ("icu_member_with_overtime", 631.35, {"overtime": 978.59, "icu_member": True}),
]


class EngineParityTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app()
        self.app.config["TESTING"] = True
        self.ctx = self.app.app_context()
        self.ctx.push()
        self.rate = StatutoryRate.active_for(date(2026, 1, 1))
        self.assertIsNotNone(self.rate, "seed must provide a 2026 StatutoryRate")

    def tearDown(self):
        db.session.rollback()
        self.ctx.pop()

    def _run_both(self, basic, allowances=0.0, overtime=0.0, bonus=0.0, pay_diff=0.0,
                  relief=0.0, pf=0.0, loan=0.0, welfare=0.0, other=0.0, icu_member=False):
        std = SalariedCalculator(self.rate).calculate(
            basic,
            other_allowances=allowances,
            overtime_pay=overtime,
            productivity_bonus=bonus,
            pay_difference=pay_diff,
            tax_relief_monthly=relief,
            pf_fund_employee=pf,
            loan_deduction=loan,
            welfare_deduction=welfare,
            other_deductions=other,
        )
        raw = compute_payslip(
            PayslipInputs(
                staff_id="PARITY",
                basic_wage=basic,
                allowances=allowances,
                overtime_pay=overtime,
                bonus=bonus,
                pay_difference=pay_diff,
                tax_relief_monthly=relief,
                provident_fund=pf,
                is_icu_member=icu_member,
                loan=loan,
                welfare=welfare,
                other_deductions=other,
            ),
            self.rate,
        )
        return std, raw

    def test_engines_agree_on_every_statutory_component(self):
        for scenario_id, basic, extra in SCENARIOS:
            with self.subTest(scenario=scenario_id):
                std, raw = self._run_both(basic, **extra)

                # Shared statutory outputs must match to the pesewa.
                self.assertAlmostEqual(std.ssnit, raw.employee_ssnit, places=2)
                self.assertAlmostEqual(std.ssf_employer, raw.employer_ssnit, places=2)
                self.assertAlmostEqual(std.net_basic_wage, raw.net_basic_wage, places=2)
                self.assertAlmostEqual(std.annual_salary, raw.annual_salary, places=2)
                self.assertAlmostEqual(std.annual_salary_15pct, raw.annual_salary_15pct, places=2)
                self.assertAlmostEqual(std.taxable_income, raw.taxable_income, places=2)
                self.assertAlmostEqual(std.ordinary_paye, raw.ordinary_paye, places=2)
                self.assertAlmostEqual(std.overtime_tax, raw.overtime_tax, places=2)
                self.assertAlmostEqual(std.bonus_tax, raw.bonus_tax, places=2)
                self.assertAlmostEqual(std.bonus_excess, raw.bonus_excess, places=2)
                self.assertAlmostEqual(std.gross_pay, raw.gross_pay, places=2)
                # Total tax: standard's `paye` == raw's `total_tax`.
                self.assertAlmostEqual(std.paye, raw.total_tax, places=2)

                # Net pay: identical once the raw-only ICU deduction is accounted
                # for. The standard engine has no ICU concept, so raw net is the
                # standard net minus exactly the union dues.
                self.assertAlmostEqual(raw.net_pay, std.net_pay - raw.icu, places=2)

    def test_top_band_earner_actually_reaches_35_percent(self):
        # Guard the fixture: a 60,000 basic must land in the top band, otherwise
        # "top-band earner" parity would be vacuous.
        _, raw = self._run_both(60000.0)
        self.assertGreater(raw.ordinary_paye, 13728.67)  # base of the 35% band

    def test_icu_member_difference_is_exactly_three_percent_of_basic(self):
        # Make the ICU asymmetry concrete: for a member the ONLY divergence
        # between the engines is 3% of basic, and it is non-zero.
        std, raw = self._run_both(1800.0, icu_member=True)
        self.assertAlmostEqual(raw.icu, round(1800.0 * self.rate.icu_member_rate, 2), places=2)
        self.assertGreater(raw.icu, 0)
        self.assertAlmostEqual(std.net_pay - raw.net_pay, raw.icu, places=2)

    def test_non_member_has_no_icu_and_nets_identically(self):
        std, raw = self._run_both(1800.0, icu_member=False)
        self.assertEqual(raw.icu, 0)
        self.assertAlmostEqual(raw.net_pay, std.net_pay, places=2)


if __name__ == "__main__":
    unittest.main()
