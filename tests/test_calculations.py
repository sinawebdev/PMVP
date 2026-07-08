"""Regression tests for the payroll calculators against real client figures.

The salaried fixture is employee AC605 (Sampson K. Kluvie) from the decrypted
ACS workbook, January 2026. The hourly fixture is ERIC DZAH (row 2) from the
DZ workbook. Both must match to the cedi-cent — these are the numbers the
client actually paid.
"""
import os
import unittest
from datetime import date

os.environ["DATABASE_URL"] = "sqlite:///:memory:"

from app import create_app, db
from app.models import (
    ClientCompany,
    Employee,
    PayrollItem,
    PayrollRun,
    RawPayEntry,
    StatutoryRate,
    WageRateProfile,
)
from app.payroll_calculations import period_start, statutory_rate_for_run
from app.payroll_calculations.hourly import HourlyShiftCalculator
from app.payroll_calculations.salaried import SalariedCalculator


class CalculationTestCase(unittest.TestCase):
    def setUp(self):
        self.app = create_app()
        self.app.config["TESTING"] = True
        self.ctx = self.app.app_context()
        self.ctx.push()

    def tearDown(self):
        self.ctx.pop()

    def _rate_jan_2026(self):
        rate = StatutoryRate.active_for(date(2026, 1, 1))
        self.assertIsNotNone(rate, "seeded statutory rate version missing")
        return rate

    def test_salaried_ac605_regression(self):
        """ACS workbook, AC605 (Sampson K. Kluvie), January 2026 — exact
        figures (±0.01), independently verified against Chrisnat's actual
        "WAGE SHT" and "GRA PAYE" Excel tabs, including every derived field.

        The 100.00 line is "PF FUND / EMPLOYEE" (ACS RAW DATA column AA), a
        pre-tax deduction: it reduces both taxable income and net pay.
        """
        calc = SalariedCalculator(self._rate_jan_2026())
        result = calc.calculate(
            2737.37,
            transport_allowance=323.93,
            pf_fund_employee=100.00,
        )

        self.assertAlmostEqual(result.ssnit, 150.56, delta=0.01)
        self.assertAlmostEqual(result.ssf_employer, 355.86, delta=0.01)
        self.assertAlmostEqual(result.net_basic_wage, 2586.81, delta=0.01)
        self.assertAlmostEqual(result.gross_pay, 3061.30, delta=0.01)
        self.assertAlmostEqual(result.taxable_income, 2810.74, delta=0.01)
        self.assertAlmostEqual(result.paye, 382.63, delta=0.01)
        self.assertAlmostEqual(result.net_pay, 2428.11, delta=0.01)
        self.assertAlmostEqual(result.pf_fund_employee, 100.00, delta=0.01)
        self.assertAlmostEqual(result.annual_salary, 32848.44, delta=0.01)
        self.assertAlmostEqual(result.annual_salary_15pct, 4927.27, delta=0.01)
        self.assertEqual(result.overtime_tax, 0.0)
        self.assertEqual(result.bonus_tax, 0.0)
        self.assertEqual(result.bonus_excess, 0.0)
        self.assertEqual(result.pay_difference, 0.0)
        self.assertEqual(result.loan_advance, 0.0)
        self.assertEqual(result.end_of_year_bonus, 0.0)
        # Every new field must survive into the PayrollItem column mapping.
        fields = result.as_payroll_item_fields()
        self.assertAlmostEqual(fields["ssf_employer"], 355.86, delta=0.01)
        self.assertAlmostEqual(fields["net_basic_wage"], 2586.81, delta=0.01)
        self.assertAlmostEqual(fields["annual_salary"], 32848.44, delta=0.01)
        self.assertAlmostEqual(fields["annual_salary_15pct"], 4927.27, delta=0.01)
        self.assertAlmostEqual(fields["taxable_income"], 2810.74, delta=0.01)
        for key in ("overtime_tax", "bonus_tax", "bonus_excess",
                    "pay_difference", "loan_advance", "end_of_year_bonus"):
            self.assertEqual(fields[key], 0.0)

    def test_salaried_ac636_overtime_concession_regression(self):
        """ACS workbook, AC636 (David Kwame Tetteh), January 2026 — verifies
        the three-component tax: overtime is taxed at the concessionary flat
        rates (5% up to 50% of basic, 10% on the excess), never at the
        marginal bands. Verified line-by-line against the live formulas."""
        calc = SalariedCalculator(self._rate_jan_2026())
        result = calc.calculate(
            1675.14,
            overtime_pay=3125.30,
            transport_allowance=323.93,
            pf_fund_employee=100.00,
        )

        # Overtime: 50% of basic = 837.57 -> 41.88 at 5%; excess 2287.73 -> 228.77 at 10%.
        self.assertAlmostEqual(result.overtime_tax, 270.65, delta=0.01)
        self.assertAlmostEqual(result.ordinary_paye, 206.96, delta=0.01)
        self.assertAlmostEqual(result.paye, 477.61, delta=0.01)
        self.assertAlmostEqual(result.net_pay, 4454.63, delta=0.01)
        # Overtime must NOT appear in ordinary taxable income.
        self.assertAlmostEqual(result.taxable_income, 1806.94, delta=0.01)
        self.assertEqual(result.bonus_tax, 0.0)
        self.assertEqual(result.bonus_excess, 0.0)
        # Derived fields for the same row, verified against the WAGE SHT tab.
        self.assertAlmostEqual(result.ssnit, 92.13, delta=0.01)      # 5.5% of basic
        self.assertAlmostEqual(result.ssf_employer, 217.77, delta=0.01)  # 13% of basic
        self.assertAlmostEqual(result.net_basic_wage, 1583.01, delta=0.01)
        self.assertAlmostEqual(result.annual_salary, 20101.68, delta=0.01)
        self.assertAlmostEqual(result.annual_salary_15pct, 3015.25, delta=0.01)
        fields = result.as_payroll_item_fields()
        self.assertAlmostEqual(fields["overtime_tax"], 270.65, delta=0.01)
        self.assertAlmostEqual(fields["ssf_employer"], 217.77, delta=0.01)
        self.assertAlmostEqual(fields["net_basic_wage"], 1583.01, delta=0.01)
        self.assertAlmostEqual(fields["annual_salary"], 20101.68, delta=0.01)
        self.assertAlmostEqual(fields["annual_salary_15pct"], 3015.25, delta=0.01)

    def test_salaried_ac661_mid_overtime_regression(self):
        """ACS workbook, AC661, January 2026 — the mid-overtime golden row
        from the build brief: gross 3811.06, total tax 340.84 (overtime
        component 156.17), net 3215.50.

        Inputs reproduce the brief's targets exactly: overtime 1946.80,
        transport 323.93, PF fund 100.00 and a 70.00 post-tax deduction
        (booked here as loan_deduction; every post-tax bucket subtracts
        identically, so the golden figures don't depend on the bucket)."""
        calc = SalariedCalculator(self._rate_jan_2026())
        result = calc.calculate(
            1540.33,
            overtime_pay=1946.80,
            transport_allowance=323.93,
            pf_fund_employee=100.00,
            loan_deduction=70.00,
        )

        self.assertAlmostEqual(result.gross_pay, 3811.06, delta=0.01)
        # Overtime: 50% of basic = 770.17 -> 38.51 at 5%; excess 1176.64 -> 117.66 at 10%.
        self.assertAlmostEqual(result.overtime_tax, 156.17, delta=0.01)
        self.assertAlmostEqual(result.ordinary_paye, 184.67, delta=0.01)
        self.assertAlmostEqual(result.paye, 340.84, delta=0.01)
        self.assertAlmostEqual(result.net_pay, 3215.50, delta=0.01)
        self.assertAlmostEqual(result.ssnit, 84.72, delta=0.01)
        self.assertAlmostEqual(result.net_basic_wage, 1455.61, delta=0.01)
        self.assertAlmostEqual(result.taxable_income, 1679.54, delta=0.01)
        self.assertLessEqual(result.net_pay, result.gross_pay)

    def test_golden_rows_never_pay_net_above_gross(self):
        """Build-brief invariant, asserted on all three golden fixtures:
        net_pay <= gross_pay on every computed row (loan_advance, absent
        from all three real rows, is the one legitimate exception)."""
        calc = SalariedCalculator(self._rate_jan_2026())
        fixtures = {
            "AC605": dict(transport_allowance=323.93, pf_fund_employee=100.00),
            "AC636": dict(overtime_pay=3125.30, transport_allowance=323.93,
                          pf_fund_employee=100.00),
            "AC661": dict(overtime_pay=1946.80, transport_allowance=323.93,
                          pf_fund_employee=100.00, loan_deduction=70.00),
        }
        basics = {"AC605": 2737.37, "AC636": 1675.14, "AC661": 1540.33}
        for staff, inputs in fixtures.items():
            with self.subTest(staff=staff):
                result = calc.calculate(basics[staff], **inputs)
                self.assertLessEqual(result.net_pay, result.gross_pay)

    def test_salaried_bonus_concession_threshold(self):
        """Synthetic bonus case (no real fixture exists this month): bonus up
        to 15% of ANNUAL basic taxes flat at 5%; the excess joins ordinary
        taxable income and is taxed at the marginal rate."""
        calc = SalariedCalculator(self._rate_jan_2026())
        # basic 2000 -> annual 24000 -> concession cap 3600.
        # Bonus fully within cap: flat 5%, nothing added to taxable income.
        base = calc.calculate(2000)
        within = calc.calculate(2000, productivity_bonus=3600)
        self.assertAlmostEqual(within.bonus_tax, 180.00, delta=0.01)  # 3600 * 5%
        self.assertAlmostEqual(within.taxable_income, base.taxable_income, delta=0.01)
        self.assertAlmostEqual(within.ordinary_paye, base.ordinary_paye, delta=0.01)
        self.assertAlmostEqual(within.paye, base.paye + 180.00, delta=0.01)

        # Bonus over the cap: 3600 taxed flat, the 1400 excess joins taxable.
        over = calc.calculate(2000, productivity_bonus=5000)
        self.assertAlmostEqual(over.bonus_tax, 180.00, delta=0.01)
        self.assertAlmostEqual(
            over.taxable_income, base.taxable_income + 1400.00, delta=0.01
        )
        # taxable = (2000 - 110) + 1400 = 3290 -> (3290-730)*0.175 + 18.5 = 466.50
        self.assertAlmostEqual(over.ordinary_paye, 466.50, delta=0.01)
        self.assertAlmostEqual(over.paye, 646.50, delta=0.01)
        # gross 7000 - ssf 110 - total tax 646.50 = net 6243.50
        self.assertAlmostEqual(over.net_pay, 6243.50, delta=0.01)

    def test_salaried_medical_flows_through_taxable(self):
        """Medical allowance is ordinary taxable income; a bonus within the
        concession cap is not — it is taxed flat instead."""
        calc = SalariedCalculator(self._rate_jan_2026())
        base = calc.calculate(2000)
        with_extras = calc.calculate(
            2000, medical_allowance=150, productivity_bonus=50
        )
        self.assertAlmostEqual(with_extras.gross_pay - base.gross_pay, 200, delta=0.01)
        # Only medical raises ordinary taxable income; the 50 bonus is flat-taxed.
        self.assertAlmostEqual(
            with_extras.taxable_income - base.taxable_income, 150, delta=0.01
        )
        self.assertAlmostEqual(with_extras.bonus_tax, 2.50, delta=0.01)  # 50 * 5%
        fields = with_extras.as_payroll_item_fields()
        self.assertEqual(fields["medical_allowance"], 150)
        self.assertEqual(fields["productivity_bonus"], 50)

    def test_salaried_result_maps_to_payroll_item_fields(self):
        calc = SalariedCalculator(self._rate_jan_2026())
        fields = calc.calculate(2000).as_payroll_item_fields()
        self.assertEqual(fields["basic_salary"], 2000)
        self.assertAlmostEqual(fields["ssnit"], 110.00, delta=0.01)
        self.assertAlmostEqual(
            fields["gross_pay"] - fields["total_deductions"], fields["net_pay"], delta=0.01
        )

    def test_paye_band_boundaries(self):
        """The band function in isolation, at every threshold boundary,
        computed directly from the GRA formula. Catches >= vs > off-by-one
        bugs that no real-employee fixture would (none land on a threshold)."""
        rate = self._rate_jan_2026()
        cases = [
            (489.99, 0.00),      # just below the 5% threshold
            (490.00, 0.00),      # exact 5% threshold, base case
            (550.00, 3.00),      # mid 5% band
            (600.00, 5.50),      # exact 10% threshold
            (650.00, 10.50),     # mid 10% band
            (730.00, 18.50),     # exact 17.5% threshold
            (3896.67, 572.67),   # exact 25% threshold
            (19896.67, 4572.67), # exact 30% threshold
            # 35% starts at GRA's 605,000/12 = 50,416.67 — NOT the source
            # sheet's 50,000. At 50,000 the 30% band still applies.
            (50000.00, 13603.67),  # inside the 30% band (sheet had a gap here)
            (50416.67, 13728.67),  # exact 35% threshold, continuous with 30%
            (60000.00, 17082.84),  # well into the 35% band
        ]
        for taxable, expected in cases:
            with self.subTest(taxable=taxable):
                self.assertAlmostEqual(
                    rate.compute_paye(taxable), expected, delta=0.001
                )

    def test_money_rounds_half_up_at_xx5_boundary(self):
        """.xx5 values must round away from zero, not to-even — the exact
        divergence between accounting convention and Python's round()."""
        from app.money import money

        self.assertEqual(money(56.125), 56.13)   # round() would give 56.12
        self.assertEqual(money(491.875), 491.88)
        self.assertEqual(money(0.005), 0.01)     # round() would give 0.0
        # And through the band function itself: taxable 945 -> 56.125 -> up.
        self.assertEqual(self._rate_jan_2026().compute_paye(945), 56.13)

    def test_period_start_and_rate_lookup(self):
        self.assertEqual(period_start("January", 2026), date(2026, 1, 1))
        self.assertEqual(period_start("march", 2025), date(2025, 3, 1))
        run = PayrollRun(month="February", year=2026, status="Draft")
        self.assertIsNotNone(statutory_rate_for_run(run))
        stale = PayrollRun(month="January", year=2000, status="Draft")
        with self.assertRaises(LookupError):
            statutory_rate_for_run(stale)

    def test_salaried_zero_income_employee(self):
        """On-leave / between-assignments worker: everything zero must come
        out zero without raising — including the 50%-of-basic overtime
        threshold when basic is zero."""
        calc = SalariedCalculator(self._rate_jan_2026())
        result = calc.calculate(0)
        self.assertEqual(result.ssnit, 0.0)
        self.assertEqual(result.paye, 0.0)
        self.assertEqual(result.net_pay, 0.0)
        self.assertEqual(result.gross_pay, 0.0)
        # Overtime against a zero basic: the whole amount is over the (zero)
        # threshold and taxes at the high rate — no crash, no division.
        with_ot = calc.calculate(0, overtime_pay=100)
        self.assertAlmostEqual(with_ot.overtime_tax, 10.00, delta=0.001)

    def test_hourly_zero_hours_employee(self):
        """Raw run with zero-hours lines: ssf/paye/net all zero, no crash."""
        client = ClientCompany.query.first()
        run = PayrollRun(
            month="January", year=2026, status="Draft",
            upload_type="raw", client_company_id=client.id,
        )
        db.session.add(run)
        db.session.flush()
        db.session.add(
            WageRateProfile(
                client_company_id=client.id, employee_id=None,
                pay_code="Z-NORM", hourly_rate=10.0, category="basic",
            )
        )
        db.session.add(
            RawPayEntry(payroll_run_id=run.id, employee_id_str="ZERO1",
                        pay_code="Z-NORM", hours=0)
        )
        db.session.flush()
        result = HourlyShiftCalculator(run, self._rate_jan_2026()).calculate_run()["ZERO1"]
        self.assertEqual(result.gross_pay, 0.0)
        self.assertEqual(result.ssnit, 0.0)
        self.assertEqual(result.paye, 0.0)
        self.assertEqual(result.net_pay, 0.0)

    def test_salaried_combined_bonus_and_overtime(self):
        """Synthetic fixture verified by hand against the source formulas —
        the bonus+overtime combination occurs in neither real roster this
        month, so this is the only coverage of both concessions at once."""
        calc = SalariedCalculator(self._rate_jan_2026())
        result = calc.calculate(3000, overtime_pay=2000, productivity_bonus=6000)

        self.assertAlmostEqual(result.ssnit, 165.00, delta=0.001)
        self.assertAlmostEqual(result.ssf_employer, 390.00, delta=0.001)
        # OT: 1500 within the 50%-of-basic cap at 5% (75) + 500 excess at 10% (50).
        self.assertAlmostEqual(result.overtime_tax, 125.00, delta=0.001)
        # Bonus: 5400 within 15%-of-annual cap at 5% (270); 600 excess to taxable.
        self.assertAlmostEqual(result.bonus_tax, 270.00, delta=0.001)
        self.assertAlmostEqual(result.taxable_income, 3435.00, delta=0.001)
        # (3435 - 730) * 0.175 + 18.5 = 491.875 -> ROUND_HALF_UP -> 491.88.
        self.assertAlmostEqual(result.ordinary_paye, 491.88, delta=0.001)
        self.assertAlmostEqual(result.paye, 886.88, delta=0.001)
        self.assertAlmostEqual(result.gross_pay, 11000.00, delta=0.001)
        self.assertAlmostEqual(result.net_pay, 9948.12, delta=0.001)

    def test_hourly_combined_bonus_and_overtime(self):
        """The same combined synthetic on the hourly path, bucketed by pay-code
        category — the hourly bonus/overtime code paths are separate code."""
        client = ClientCompany.query.first()
        run = PayrollRun(
            month="January", year=2026, status="Draft",
            upload_type="raw", client_company_id=client.id,
        )
        db.session.add(run)
        db.session.flush()
        for pay_code, rate_value, category in [
            ("CB-NORM", 3000.00, "basic"),
            ("CB-OT", 2000.00, "overtime"),
            ("CB-BON", 6000.00, "bonus"),
        ]:
            db.session.add(
                WageRateProfile(
                    client_company_id=client.id, employee_id=None,
                    pay_code=pay_code, hourly_rate=rate_value, category=category,
                )
            )
            db.session.add(
                RawPayEntry(payroll_run_id=run.id, employee_id_str="COMBO1",
                            pay_code=pay_code, hours=1)
            )
        db.session.flush()
        result = HourlyShiftCalculator(run, self._rate_jan_2026()).calculate_run()["COMBO1"]

        self.assertAlmostEqual(result.ssnit, 165.00, delta=0.001)
        self.assertAlmostEqual(result.overtime_tax, 125.00, delta=0.001)
        self.assertAlmostEqual(result.bonus_tax, 270.00, delta=0.001)
        self.assertAlmostEqual(result.taxable_income, 3435.00, delta=0.001)
        self.assertAlmostEqual(result.ordinary_paye, 491.88, delta=0.001)
        self.assertAlmostEqual(result.paye, 886.88, delta=0.001)
        self.assertAlmostEqual(result.gross_pay, 11000.00, delta=0.001)
        self.assertAlmostEqual(result.net_pay, 9948.12, delta=0.001)

    def test_salaried_tax_relief_reduces_ordinary_paye(self):
        """A nonzero tax_relief_monthly reduces ordinary taxable income before
        the bands (mechanism test — the correct GRA amount per relief category
        is entered per employee, never hardcoded)."""
        calc = SalariedCalculator(self._rate_jan_2026())
        base = calc.calculate(2000)
        relieved = calc.calculate(2000, tax_relief_monthly=200)
        self.assertAlmostEqual(
            relieved.taxable_income, base.taxable_income - 200, delta=0.001
        )
        # Both taxable values sit in the 17.5% band: paye drops by 200 * 0.175.
        self.assertAlmostEqual(
            base.ordinary_paye - relieved.ordinary_paye, 35.00, delta=0.001
        )
        # Relief is not a cash deduction: gross and total_deductions structure
        # unchanged apart from the smaller PAYE.
        self.assertAlmostEqual(relieved.gross_pay, base.gross_pay, delta=0.001)
        self.assertAlmostEqual(
            relieved.net_pay, base.net_pay + 35.00, delta=0.001
        )
        # It must not touch the concessionary components.
        with_ot = calc.calculate(2000, overtime_pay=500, tax_relief_monthly=200)
        self.assertAlmostEqual(with_ot.overtime_tax, 25.00, delta=0.001)

    def test_hourly_tax_relief_from_roster(self):
        """Hourly path pulls tax_relief_monthly from the roster employee
        record and subtracts it from ordinary taxable income."""
        client = ClientCompany.query.first()
        employee = Employee(
            staff_id="RLF01", full_name="Relief Worker",
            client_company_id=client.id, status="Active",
            tax_relief_monthly=200.0,
        )
        db.session.add(employee)
        run = PayrollRun(
            month="January", year=2026, status="Draft",
            upload_type="raw", client_company_id=client.id,
        )
        db.session.add(run)
        db.session.flush()
        db.session.add(
            WageRateProfile(
                client_company_id=client.id, employee_id=None,
                pay_code="RLF-NORM", hourly_rate=20.0, category="basic",
            )
        )
        db.session.add(
            RawPayEntry(payroll_run_id=run.id, employee_id_str="RLF01",
                        pay_code="RLF-NORM", hours=100)
        )
        db.session.flush()
        result = HourlyShiftCalculator(run, self._rate_jan_2026()).calculate_run()["RLF01"]
        # basic 2000 -> ssf 110 -> taxable 2000 - 110 - 200 = 1690.
        self.assertAlmostEqual(result.taxable_income, 1690.00, delta=0.001)
        self.assertAlmostEqual(result.tax_relief_monthly, 200.00, delta=0.001)
        expected_paye = self._rate_jan_2026().compute_paye(1690)
        self.assertAlmostEqual(result.ordinary_paye, expected_paye, delta=0.001)

    def test_salaried_overtime_under_threshold(self):
        """Overtime wholly within 50% of basic taxes flat at the low rate only
        and never touches ordinary taxable income."""
        calc = SalariedCalculator(self._rate_jan_2026())
        base = calc.calculate(2000)
        result = calc.calculate(2000, overtime_pay=500)  # cap = 1000
        self.assertAlmostEqual(result.overtime_tax, 25.00, delta=0.01)  # 500 * 5%
        self.assertAlmostEqual(result.taxable_income, base.taxable_income, delta=0.01)
        self.assertAlmostEqual(result.ordinary_paye, base.ordinary_paye, delta=0.01)
        self.assertAlmostEqual(result.paye, base.paye + 25.00, delta=0.01)

    def test_hourly_richard_woode_regression(self):
        """DZ workbook, Richard Woode (DCL9) — verified against DZ's own
        formulas (AR44/AU47/AY51). Exercises OT-over-threshold + bonus on the
        hourly path: overtime concessionary, bonus flat within the annual cap,
        shift allowances as ordinary taxable income, SSF on basic wage only.

        Components (2dp): basic 631.35, overtime 700.00 + 278.59 = 978.59,
        bonus 120.97, shift allowances 50.51 + 265.17. The workbook's gross is
        2046.58 from unrounded internals; the 2dp components sum to 2046.59,
        hence the slightly wider gross tolerance.
        """
        client = ClientCompany.query.first()
        run = PayrollRun(
            month="January", year=2026, status="Draft",
            upload_type="raw", client_company_id=client.id,
        )
        db.session.add(run)
        db.session.flush()
        profiles = [
            ("DCL-NORM", 631.35, "basic", "Normal hours"),
            ("DCL-OTWD", 700.00, "overtime", "Weekday OT"),
            ("DCL-OTSAT", 278.59, "overtime", "Saturday OT"),
            ("DCL-BONUS", 120.97, "bonus", "Productivity bonus"),
            ("DCL-AFT", 50.51, "allowance", "Afternoon shift"),
            ("DCL-NGT", 265.17, "allowance", "Night shift"),
        ]
        for pay_code, rate_value, category, description in profiles:
            db.session.add(
                WageRateProfile(
                    client_company_id=client.id, employee_id=None,
                    pay_code=pay_code, hourly_rate=rate_value,
                    category=category, description=description,
                )
            )
            db.session.add(
                RawPayEntry(
                    payroll_run_id=run.id, employee_id_str="DCL9",
                    pay_code=pay_code, hours=1,
                )
            )
        db.session.flush()

        calc = HourlyShiftCalculator(run, self._rate_jan_2026())
        result = calc.calculate_run()["DCL9"]

        self.assertAlmostEqual(result.basic_wage, 631.35, delta=0.01)
        self.assertAlmostEqual(result.overtime_pay, 978.59, delta=0.01)
        self.assertAlmostEqual(result.bonus, 120.97, delta=0.01)
        self.assertAlmostEqual(result.allowances, 315.68, delta=0.01)
        self.assertAlmostEqual(result.gross_pay, 2046.58, delta=0.011)
        self.assertAlmostEqual(result.ssnit, 34.72, delta=0.01)  # 5.5% of basic only
        self.assertAlmostEqual(result.taxable_income, 912.31, delta=0.01)
        self.assertAlmostEqual(result.ordinary_paye, 50.40, delta=0.01)
        self.assertAlmostEqual(result.overtime_tax, 82.08, delta=0.01)
        self.assertAlmostEqual(result.bonus_tax, 6.05, delta=0.01)
        self.assertAlmostEqual(result.paye, 138.53, delta=0.01)

    def test_hourly_overtime_under_threshold(self):
        """Hourly OT wholly within 50% of the basic wage taxes flat at the low
        rate; allowances stay in ordinary taxable income; SSF on basic only."""
        client = ClientCompany.query.first()
        run = PayrollRun(
            month="January", year=2026, status="Draft",
            upload_type="raw", client_company_id=client.id,
        )
        db.session.add(run)
        db.session.flush()
        db.session.add(
            WageRateProfile(
                client_company_id=client.id, employee_id=None,
                pay_code="SYN-NORM", hourly_rate=10.0, category="basic",
            )
        )
        db.session.add(
            WageRateProfile(
                client_company_id=client.id, employee_id=None,
                pay_code="SYN-OT", hourly_rate=10.0, category="overtime",
            )
        )
        db.session.add(
            RawPayEntry(payroll_run_id=run.id, employee_id_str="SYN01",
                        pay_code="SYN-NORM", hours=100)
        )
        db.session.add(
            RawPayEntry(payroll_run_id=run.id, employee_id_str="SYN01",
                        pay_code="SYN-OT", hours=30)
        )
        db.session.flush()

        result = HourlyShiftCalculator(run, self._rate_jan_2026()).calculate_run()["SYN01"]
        # basic 1000, OT 300 within the 500 cap -> flat 5% only.
        self.assertAlmostEqual(result.overtime_tax, 15.00, delta=0.01)
        self.assertAlmostEqual(result.ssnit, 55.00, delta=0.01)
        self.assertAlmostEqual(result.taxable_income, 945.00, delta=0.01)
        # (945 - 730) * 0.175 + 18.5 = 56.125 exactly -> ROUND_HALF_UP gives
        # 56.13 (Python's default banker's rounding would wrongly give 56.12).
        self.assertAlmostEqual(result.ordinary_paye, 56.13, delta=0.001)
        self.assertAlmostEqual(result.paye, 71.13, delta=0.001)

    def test_hourly_eric_dzah_ssf_regression(self):
        """DZ workbook, ERIC DZAH: wage 2100 -> SSF employee 115.50 /
        employer 273.00 / net-of-SSF wage 1984.50. Confirms the SSF maths is
        identical across both wage types."""
        client = ClientCompany.query.first()
        run = PayrollRun(
            month="January", year=2026, status="Draft",
            upload_type="raw", client_company_id=client.id,
        )
        db.session.add(run)
        db.session.flush()
        # 140 normal hours at the client's 15.00 rate = 2100 gross wage.
        db.session.add(
            WageRateProfile(
                client_company_id=client.id, employee_id=None,
                pay_code="NORM01", hourly_rate=15.0, description="Normal hours",
            )
        )
        db.session.add(
            RawPayEntry(
                payroll_run_id=run.id, employee_id_str="DZ001",
                pay_code="NORM01", hours=140,
            )
        )
        db.session.flush()

        calc = HourlyShiftCalculator(run, self._rate_jan_2026())
        results = calc.calculate_run()
        self.assertEqual(len(results), 1)
        result = results["DZ001"]

        self.assertAlmostEqual(result.gross_pay, 2100.00, delta=0.01)
        self.assertAlmostEqual(result.ssnit, 115.50, delta=0.01)
        self.assertAlmostEqual(result.ssf_employer, 273.00, delta=0.01)
        self.assertAlmostEqual(result.gross_pay - result.ssnit, 1984.50, delta=0.01)
        # Same PAYE bands then apply to the taxable (net-of-SSF) wage.
        self.assertAlmostEqual(
            result.paye, self._rate_jan_2026().compute_paye(1984.50), delta=0.01
        )
        self.assertAlmostEqual(
            result.net_pay, result.gross_pay - result.ssnit - result.paye, delta=0.01
        )

    def test_hourly_employee_override_beats_client_default(self):
        client = ClientCompany.query.first()
        employee = Employee(
            staff_id="DZ099", full_name="Override Worker",
            client_company_id=client.id, status="Active",
        )
        db.session.add(employee)
        db.session.flush()
        db.session.add(
            WageRateProfile(
                client_company_id=client.id, employee_id=None,
                pay_code="OT-SAT", hourly_rate=10.0,
            )
        )
        db.session.add(
            WageRateProfile(
                client_company_id=client.id, employee_id=employee.id,
                pay_code="OT-SAT", hourly_rate=12.5,
            )
        )
        db.session.flush()
        self.assertEqual(
            WageRateProfile.rate_for(client.id, employee.id, "OT-SAT"), 12.5
        )
        self.assertEqual(WageRateProfile.rate_for(client.id, None, "OT-SAT"), 10.0)

    def test_hourly_missing_rate_is_reported_not_guessed(self):
        client = ClientCompany.query.first()
        run = PayrollRun(
            month="January", year=2026, status="Draft",
            upload_type="raw", client_company_id=client.id,
        )
        db.session.add(run)
        db.session.flush()
        db.session.add(
            RawPayEntry(
                payroll_run_id=run.id, employee_id_str="DZ777",
                pay_code="MYSTERY", hours=10,
            )
        )
        db.session.flush()
        calc = HourlyShiftCalculator(run, self._rate_jan_2026())
        results = calc.calculate_run()
        self.assertIn("MYSTERY", results["DZ777"].missing_rate_codes)
        self.assertEqual(results["DZ777"].gross_pay, 0)


class NewFieldBehaviourTestCase(unittest.TestCase):
    """Spec behaviours for the enhancement fields: pay difference is ordinary
    taxable income, the end-of-year bonus shares the annual concession cap,
    and a loan advance adds to net pay without touching tax."""

    def setUp(self):
        self.app = create_app()
        self.app.config["TESTING"] = True
        self.ctx = self.app.app_context()
        self.ctx.push()
        self.calc = SalariedCalculator(StatutoryRate.active_for(date(2026, 1, 1)))

    def tearDown(self):
        self.ctx.pop()

    def test_pay_difference_taxed_normally(self):
        base = self.calc.calculate(2000)
        with_diff = self.calc.calculate(2000, pay_difference=400)
        self.assertAlmostEqual(with_diff.gross_pay, base.gross_pay + 400, delta=0.001)
        self.assertAlmostEqual(
            with_diff.taxable_income, base.taxable_income + 400, delta=0.001
        )
        # Both taxables sit in the 17.5% band — no concessionary treatment.
        self.assertAlmostEqual(
            with_diff.ordinary_paye - base.ordinary_paye, 70.00, delta=0.001
        )
        self.assertEqual(with_diff.overtime_tax, base.overtime_tax)
        self.assertEqual(with_diff.bonus_tax, base.bonus_tax)

    def test_end_of_year_bonus_shares_annual_cap_with_productivity(self):
        # basic 2000 -> annual 24000 -> concession cap 3600. Split 2000 + 2000
        # across the two bonus fields: 3600 flat at 5%, 400 excess to taxable.
        base = self.calc.calculate(2000)
        result = self.calc.calculate(
            2000, productivity_bonus=2000, end_of_year_bonus=2000
        )
        self.assertAlmostEqual(result.bonus_tax, 180.00, delta=0.001)  # 3600 * 5%
        self.assertAlmostEqual(result.bonus_excess, 400.00, delta=0.001)
        self.assertAlmostEqual(
            result.taxable_income, base.taxable_income + 400.00, delta=0.001
        )
        self.assertAlmostEqual(result.gross_pay, base.gross_pay + 4000, delta=0.001)

    def test_bonus_cap_enforced_once_per_tax_year_across_runs(self):
        """The 15%-of-annual-basic bonus concession is an ANNUAL cap, not a
        per-run one. If an employee already used it all in an earlier run
        this tax year, a later run's bonus should get none of it."""
        from app.payroll import recalculate_salaried_items
        from app.payroll_calculations import bonus_concession_used_ytd

        client = ClientCompany.query.first()
        employee = Employee(
            staff_id="YTD1", full_name="YTD Cap Test",
            client_company_id=client.id, basic_salary=2000,
        )
        db.session.add(employee)
        db.session.flush()
        rate = StatutoryRate.active_for(date(2026, 1, 1))

        # Run 1 (June): productivity bonus of 3600 exactly exhausts the
        # annual cap (2000 x 12 x 15% = 3600) at the flat 5% rate.
        run1 = PayrollRun(
            month="June", year=2026, status="Approved",
            upload_type="standard", client_company_id=client.id,
        )
        db.session.add(run1)
        db.session.flush()
        item1 = PayrollItem(
            payroll_run_id=run1.id, employee_id=employee.id,
            staff_id=employee.staff_id, full_name=employee.full_name,
            basic_salary=2000, productivity_bonus=3600,
        )
        db.session.add(item1)
        db.session.flush()
        recalculate_salaried_items(run1, rate)
        self.assertAlmostEqual(item1.bonus_tax, 180.00, delta=0.001)
        self.assertAlmostEqual(item1.bonus_excess, 0.0, delta=0.001)

        # bonus_concession_used_ytd should now report the full cap consumed.
        used = bonus_concession_used_ytd(employee.id, 2026)
        self.assertAlmostEqual(used, 3600.00, delta=0.001)

        # Run 2 (December): a 1000 end-of-year bonus with NO cap left should
        # get zero concessionary treatment — all of it joins taxable income.
        run2 = PayrollRun(
            month="December", year=2026, status="Draft",
            upload_type="standard", client_company_id=client.id,
        )
        db.session.add(run2)
        db.session.flush()
        item2 = PayrollItem(
            payroll_run_id=run2.id, employee_id=employee.id,
            staff_id=employee.staff_id, full_name=employee.full_name,
            basic_salary=2000, end_of_year_bonus=1000,
        )
        db.session.add(item2)
        db.session.flush()
        recalculate_salaried_items(run2, rate)
        self.assertAlmostEqual(item2.bonus_tax, 0.0, delta=0.001)
        self.assertAlmostEqual(item2.bonus_excess, 1000.00, delta=0.001)

    def test_draft_run_bonus_does_not_count_toward_ytd_cap(self):
        """A Draft (or Rejected) run never actually paid the employee, so its
        bonus figures must not shrink the annual cap for a later, real run."""
        from app.payroll import recalculate_salaried_items

        client = ClientCompany.query.first()
        employee = Employee(
            staff_id="YTD2", full_name="YTD Draft Test",
            client_company_id=client.id, basic_salary=2000,
        )
        db.session.add(employee)
        db.session.flush()
        rate = StatutoryRate.active_for(date(2026, 1, 1))

        # A Draft run exhausts the cap on paper only — never approved.
        draft_run = PayrollRun(
            month="June", year=2026, status="Draft",
            upload_type="standard", client_company_id=client.id,
        )
        db.session.add(draft_run)
        db.session.flush()
        draft_item = PayrollItem(
            payroll_run_id=draft_run.id, employee_id=employee.id,
            staff_id=employee.staff_id, full_name=employee.full_name,
            basic_salary=2000, productivity_bonus=3600,
        )
        db.session.add(draft_item)
        db.session.flush()
        recalculate_salaried_items(draft_run, rate)

        # A later Approved run's 1000 end-of-year bonus should get the FULL
        # cap (3600 unused, since the draft never counted) — no excess.
        approved_run = PayrollRun(
            month="December", year=2026, status="Approved",
            upload_type="standard", client_company_id=client.id,
        )
        db.session.add(approved_run)
        db.session.flush()
        approved_item = PayrollItem(
            payroll_run_id=approved_run.id, employee_id=employee.id,
            staff_id=employee.staff_id, full_name=employee.full_name,
            basic_salary=2000, end_of_year_bonus=1000,
        )
        db.session.add(approved_item)
        db.session.flush()
        recalculate_salaried_items(approved_run, rate)
        self.assertAlmostEqual(approved_item.bonus_tax, 50.00, delta=0.001)  # 1000*5%
        self.assertAlmostEqual(approved_item.bonus_excess, 0.0, delta=0.001)

    def test_loan_advance_adds_to_net_pay_untaxed(self):
        base = self.calc.calculate(2000)
        result = self.calc.calculate(2000, loan_advance=300)
        # Opposite cash direction to loan_deduction: net rises, nothing else moves.
        self.assertAlmostEqual(result.net_pay, base.net_pay + 300, delta=0.001)
        self.assertAlmostEqual(result.gross_pay, base.gross_pay, delta=0.001)
        self.assertAlmostEqual(result.taxable_income, base.taxable_income, delta=0.001)
        self.assertAlmostEqual(result.paye, base.paye, delta=0.001)
        self.assertAlmostEqual(
            result.total_deductions, base.total_deductions, delta=0.001
        )

    def test_hourly_result_persists_new_fields(self):
        client = ClientCompany.query.first()
        run = PayrollRun(
            month="January", year=2026, status="Draft",
            upload_type="raw", client_company_id=client.id,
        )
        db.session.add(run)
        db.session.flush()
        db.session.add(
            WageRateProfile(
                client_company_id=client.id, employee_id=None,
                pay_code="NF-NORM", hourly_rate=20.0, category="basic",
            )
        )
        db.session.add(
            RawPayEntry(payroll_run_id=run.id, employee_id_str="NF01",
                        pay_code="NF-NORM", hours=100)
        )
        db.session.flush()
        rate = StatutoryRate.active_for(date(2026, 1, 1))
        result = HourlyShiftCalculator(run, rate).calculate_run()["NF01"]
        fields = result.as_payroll_item_fields()
        # basic 2000 -> ssf 110 / 260, net basic 1890, annual 24000, 15% 3600.
        self.assertAlmostEqual(fields["ssf_employer"], 260.00, delta=0.001)
        self.assertAlmostEqual(fields["net_basic_wage"], 1890.00, delta=0.001)
        self.assertAlmostEqual(fields["annual_salary"], 24000.00, delta=0.001)
        self.assertAlmostEqual(fields["annual_salary_15pct"], 3600.00, delta=0.001)
        self.assertAlmostEqual(fields["taxable_income"], 1890.00, delta=0.001)
        self.assertEqual(fields["overtime_tax"], 0.0)
        self.assertEqual(fields["bonus_tax"], 0.0)
        self.assertEqual(fields["bonus_excess"], 0.0)


class ColumnAliasTestCase(unittest.TestCase):
    """Alias-map fixes: ID-shaped headers never land in amount fields, derived
    output headers never map at all, and the new dedicated fields resolve
    without colliding with their older siblings."""

    def _map(self, *headers):
        from app.excel_utils import map_columns

        return map_columns(list(headers))

    def test_social_security_id_headers_map_to_ssnit_number(self):
        mapping = self._map(
            "Social Security Number", "S.S Number", "SS Number", "SSNIT No"
        )
        for header, field in mapping.items():
            self.assertEqual(field, "ssnit_number", header)

    def test_ssnit_amount_header_still_maps_to_amount(self):
        self.assertEqual(self._map("SSNIT")["SSNIT"], "ssnit")
        self.assertEqual(self._map("SSNIT Employee")["SSNIT Employee"], "ssnit")

    def test_derived_output_headers_stay_unmapped(self):
        mapping = self._map(
            "Basic Salary", "Net Basic Wage", "Annual Salary", "15% of Annual Salary"
        )
        self.assertEqual(mapping["Basic Salary"], "basic_salary")
        self.assertEqual(mapping["Net Basic Wage"], "unmapped")
        self.assertEqual(mapping["Annual Salary"], "unmapped")
        self.assertEqual(mapping["15% of Annual Salary"], "unmapped")

    def test_loan_advance_and_deduction_stay_separate(self):
        mapping = self._map("Loan Advance", "Loan Deduction", "Loan")
        self.assertEqual(mapping["Loan Advance"], "loan_advance")
        self.assertEqual(mapping["Loan Deduction"], "loan_deduction")
        # A bare "Loan" header keeps its historical meaning: a deduction.
        self.assertEqual(mapping["Loan"], "loan_deduction")

    def test_end_of_year_bonus_does_not_collide_with_productivity(self):
        mapping = self._map("End of Year Bonus", "13th Month", "Annual Bonus", "Bonus")
        self.assertEqual(mapping["End of Year Bonus"], "end_of_year_bonus")
        self.assertEqual(mapping["13th Month"], "end_of_year_bonus")
        self.assertEqual(mapping["Annual Bonus"], "end_of_year_bonus")
        self.assertEqual(mapping["Bonus"], "productivity_bonus")

    def test_new_explicit_aliases(self):
        mapping = self._map(
            "Job Title", "Gh Card", "A/C Number", "Company Assigned",
            "Overtime Allowance", "Meal Allowance", "Welfare Supplies",
            "IOU Deduction", "Pay Difference",
        )
        self.assertEqual(mapping["Job Title"], "job_role")
        self.assertEqual(mapping["Gh Card"], "ghana_card_number")
        self.assertEqual(mapping["A/C Number"], "bank_account_number")
        self.assertEqual(mapping["Company Assigned"], "client_company")
        self.assertEqual(mapping["Overtime Allowance"], "overtime_pay")
        # Meal/welfare/IOU now land in their own dedicated columns (build
        # spec §3) instead of folding into other_allowances/other_deductions.
        self.assertEqual(mapping["Meal Allowance"], "meal_allowance")
        self.assertEqual(mapping["Welfare Supplies"], "welfare_deduction")
        self.assertEqual(mapping["IOU Deduction"], "iou_deduction")
        self.assertEqual(mapping["Pay Difference"], "pay_difference")


class GridEditTestCase(unittest.TestCase):
    """The in-app grid edit: Draft-only, raw inputs only, audit-logged."""

    def setUp(self):
        self.app = create_app()
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()
        self.client.post(
            "/login",
            data={"email": "admin@chrisnat.local", "password": "password123"},
        )

    def _make_standard_run(self, status="Draft"):
        with self.app.app_context():
            from app.models import PayrollItem

            client_co = ClientCompany.query.first()
            run = PayrollRun(
                month="January", year=2026, status=status,
                client_company_id=client_co.id,
            )
            db.session.add(run)
            db.session.flush()
            item = PayrollItem(
                payroll_run_id=run.id, staff_id="GRID1", full_name="Grid Worker",
                basic_salary=1000.0, transport_allowance=50.0,
                paye=99.99, ssnit=55.0, net_pay=845.01,
            )
            db.session.add(item)
            db.session.commit()
            return run.id, item.id

    def test_edit_saves_raw_inputs_and_audits_each_change(self):
        from app.models import AuditTrail, PayrollItem

        run_id, item_id = self._make_standard_run()
        response = self.client.post(
            f"/payroll/runs/{run_id}/items/edit",
            data={
                f"item-{item_id}-basic_salary": "1200.00",
                f"item-{item_id}-transport_allowance": "75.50",
                # An attempt to write a computed field directly — must be ignored.
                f"item-{item_id}-paye": "0.01",
                f"item-{item_id}-net_pay": "99999",
            },
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        with self.app.app_context():
            item = db.session.get(PayrollItem, item_id)
            self.assertAlmostEqual(item.basic_salary, 1200.00, delta=0.001)
            self.assertAlmostEqual(item.transport_allowance, 75.50, delta=0.001)
            # Computed fields untouched by the POST.
            self.assertAlmostEqual(item.paye, 99.99, delta=0.001)
            self.assertAlmostEqual(item.net_pay, 845.01, delta=0.001)
            audits = AuditTrail.query.filter_by(
                action="Payroll figures edited",
                related_record_type="PayrollRun",
                related_record_id=run_id,
            ).all()
            self.assertEqual(len(audits), 2)  # one per changed field
            notes = " | ".join(a.notes for a in audits)
            self.assertIn("basic_salary: 1000.00 -> 1200.00", notes)
            self.assertIn("transport_allowance: 50.00 -> 75.50", notes)

    def test_edit_rejected_once_run_leaves_draft(self):
        from app.models import PayrollItem

        run_id, item_id = self._make_standard_run(status="Pending Approval")
        response = self.client.post(
            f"/payroll/runs/{run_id}/items/edit",
            data={f"item-{item_id}-basic_salary": "9999"},
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"read-only", response.data)
        with self.app.app_context():
            item = db.session.get(PayrollItem, item_id)
            self.assertAlmostEqual(item.basic_salary, 1000.00, delta=0.001)
        # GET renders, but as read-only.
        page = self.client.get(f"/payroll/runs/{run_id}/items/edit")
        self.assertEqual(page.status_code, 200)
        self.assertIn(b"read-only", page.data)

    def test_raw_run_hours_edit(self):
        with self.app.app_context():
            client_co = ClientCompany.query.first()
            run = PayrollRun(
                month="January", year=2026, status="Draft",
                upload_type="raw", client_company_id=client_co.id,
            )
            db.session.add(run)
            db.session.flush()
            entry = RawPayEntry(
                payroll_run_id=run.id, employee_id_str="GRIDR1",
                pay_code="NORM", hours=40,
            )
            db.session.add(entry)
            db.session.commit()
            run_id, entry_id = run.id, entry.id

        response = self.client.post(
            f"/payroll/runs/{run_id}/items/edit",
            data={f"entry-{entry_id}-hours": "44.5"},
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        with self.app.app_context():
            entry = db.session.get(RawPayEntry, entry_id)
            self.assertAlmostEqual(float(entry.hours), 44.5, delta=0.001)

    def test_negative_and_garbage_values_rejected(self):
        from app.models import PayrollItem

        run_id, item_id = self._make_standard_run()
        self.client.post(
            f"/payroll/runs/{run_id}/items/edit",
            data={
                f"item-{item_id}-basic_salary": "-500",
                f"item-{item_id}-transport_allowance": "abc",
            },
            follow_redirects=True,
        )
        with self.app.app_context():
            item = db.session.get(PayrollItem, item_id)
            self.assertAlmostEqual(item.basic_salary, 1000.00, delta=0.001)
            self.assertAlmostEqual(item.transport_allowance, 50.00, delta=0.001)


if __name__ == "__main__":
    unittest.main()
