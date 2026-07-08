"""Compute-engine build-spec coverage (CLAUDE_CODE_BUILD_PROMPT.md).

Covers the deltas the spec added on top of the existing calculators:
dedicated meal/welfare/IOU columns (§3), the corrected 35% PAYE threshold
(§6), the junior-staff overtime concession warning (§7.1), and the importer
hard-stops (§8).
"""
import os
import unittest
from datetime import date

os.environ["DATABASE_URL"] = "sqlite:///:memory:"

from app import create_app, db
from app.excel_utils import map_columns
from app.models import ClientCompany, PayrollItem, PayrollRun, StatutoryRate
from app.payroll import apply_overtime_concession_warning, verify_statutory_invariants
from app.payroll_calculations.salaried import SalariedCalculator
from app.validators import collect_blocking_errors


class ComputeEngineSpecTestCase(unittest.TestCase):
    def setUp(self):
        self.app = create_app()
        self.app.config["TESTING"] = True
        self.ctx = self.app.app_context()
        self.ctx.push()

    def tearDown(self):
        db.session.rollback()
        self.ctx.pop()

    def _rate_jan_2026(self):
        rate = StatutoryRate.active_for(date(2026, 1, 1))
        self.assertIsNotNone(rate, "seeded statutory rate version missing")
        return rate

    # ---- §3 dedicated columns ------------------------------------------

    def test_meal_allowance_is_taxable_income_and_gross(self):
        """Meal (ACS column L) behaves like transport/medical: joins gross
        and ordinary taxable income. Built on the AC605 fixture, whose
        figures without meal are verified against the real workbook."""
        calc = SalariedCalculator(self._rate_jan_2026())
        base = calc.calculate(
            2737.37, transport_allowance=323.93, pf_fund_employee=100.00
        )
        with_meal = calc.calculate(
            2737.37,
            transport_allowance=323.93,
            pf_fund_employee=100.00,
            meal_allowance=50.00,
        )
        self.assertAlmostEqual(with_meal.gross_pay - base.gross_pay, 50, delta=0.01)
        self.assertAlmostEqual(
            with_meal.taxable_income - base.taxable_income, 50, delta=0.01
        )
        # AC605's taxable sits in the 17.5% band: +50 taxable -> +8.75 PAYE.
        self.assertAlmostEqual(with_meal.paye - base.paye, 8.75, delta=0.01)
        self.assertEqual(with_meal.as_payroll_item_fields()["meal_allowance"], 50.00)

    def test_welfare_and_iou_are_post_tax_deductions(self):
        """Welfare (AC) and IOU (AE) reduce net pay but never taxable income."""
        calc = SalariedCalculator(self._rate_jan_2026())
        base = calc.calculate(2737.37, transport_allowance=323.93)
        with_deductions = calc.calculate(
            2737.37,
            transport_allowance=323.93,
            welfare_deduction=20.00,
            iou_deduction=30.00,
        )
        self.assertAlmostEqual(
            with_deductions.taxable_income, base.taxable_income, delta=0.001
        )
        self.assertAlmostEqual(with_deductions.paye, base.paye, delta=0.001)
        self.assertAlmostEqual(
            base.net_pay - with_deductions.net_pay, 50.00, delta=0.01
        )
        self.assertAlmostEqual(
            with_deductions.total_deductions - base.total_deductions,
            50.00,
            delta=0.01,
        )
        fields = with_deductions.as_payroll_item_fields()
        self.assertEqual(fields["welfare_deduction"], 20.00)
        self.assertEqual(fields["iou_deduction"], 30.00)

    def test_importer_maps_dedicated_columns(self):
        """MEALS / WELFARE / IOU headers resolve to their own fields instead
        of folding into other_allowances / other_deductions."""
        mapping = map_columns(
            ["STAFF ID", "NAME", "BASIC", "MEALS", "WELFARE", "IOU",
             "MEAL ALLOWANCE", "IOU DEDUCTION", "OTHER DEDUCTIONS"]
        )
        self.assertEqual(mapping["MEALS"], "meal_allowance")
        self.assertEqual(mapping["MEAL ALLOWANCE"], "meal_allowance")
        self.assertEqual(mapping["WELFARE"], "welfare_deduction")
        self.assertEqual(mapping["IOU"], "iou_deduction")
        self.assertEqual(mapping["IOU DEDUCTION"], "iou_deduction")
        self.assertEqual(mapping["OTHER DEDUCTIONS"], "other_deductions")

    # ---- §6 corrected PAYE bands ---------------------------------------

    def test_35_percent_band_starts_at_gra_threshold(self):
        """The sheet started 35% at 50,000; GRA is 605,000/12 = 50,416.67.
        The band function must be continuous through the corrected boundary."""
        rate = self._rate_jan_2026()
        self.assertAlmostEqual(rate.compute_paye(50416.67), 13728.67, delta=0.001)
        # Just below the threshold the 30% formula applies — the sheet's
        # 50,000 cutover produced a 125-cedi discontinuity here.
        self.assertAlmostEqual(rate.compute_paye(50416.66), 13728.67, delta=0.01)
        self.assertAlmostEqual(rate.compute_paye(50000.00), 13603.67, delta=0.001)

    # ---- §7.1 junior-staff overtime warning ----------------------------

    def test_junior_ot_warning_flags_high_earner_with_overtime(self):
        rate = self._rate_jan_2026()
        item = PayrollItem(
            staff_id="AC636", basic_salary=1675.14, overtime_pay=3125.30,
            warning_notes="", validation_status="OK",
        )
        apply_overtime_concession_warning(item, rate)
        self.assertEqual(item.validation_status, "Warning")
        self.assertIn("junior-staff qualifying threshold", item.warning_notes)
        # Idempotent: recalculating must not stack duplicate notes.
        apply_overtime_concession_warning(item, rate)
        self.assertEqual(
            item.warning_notes.count("junior-staff qualifying threshold"), 1
        )

    def test_junior_ot_warning_spares_qualifying_and_no_ot_workers(self):
        rate = self._rate_jan_2026()
        junior = PayrollItem(
            staff_id="J1", basic_salary=1200.00, overtime_pay=400.00,
            warning_notes="", validation_status="OK",
        )
        apply_overtime_concession_warning(junior, rate)
        self.assertEqual(junior.validation_status, "OK")
        self.assertEqual(junior.warning_notes, "")

        no_ot = PayrollItem(
            staff_id="N1", basic_salary=5000.00, overtime_pay=0.0,
            warning_notes="", validation_status="OK",
        )
        apply_overtime_concession_warning(no_ot, rate)
        self.assertEqual(no_ot.validation_status, "OK")

    def test_junior_ot_warning_clears_when_no_longer_applicable(self):
        """A stale copy of OUR note is removed on recalculation while other
        warnings survive untouched."""
        rate = self._rate_jan_2026()
        item = PayrollItem(
            staff_id="X1", basic_salary=1200.00, overtime_pay=100.00,
            warning_notes=(
                "Missing Ghana Card number.; Overtime ... above the GRA "
                "junior-staff qualifying threshold (GHS 1,500.00/month) ..."
            ),
            validation_status="Warning",
        )
        apply_overtime_concession_warning(item, rate)
        self.assertNotIn("junior-staff qualifying threshold", item.warning_notes)
        self.assertIn("Missing Ghana Card number.", item.warning_notes)
        self.assertEqual(item.validation_status, "Warning")

    # ---- §8 hard-stops --------------------------------------------------

    def test_blocking_errors_zero_basic_active_worker(self):
        rows = [
            {"staff_id": "AC1", "full_name": "A", "basic_salary": 0, "status": "Active"},
            {"staff_id": "AC2", "full_name": "B", "basic_salary": 1500, "status": "Active"},
        ]
        errors = collect_blocking_errors(rows)
        self.assertEqual(len(errors), 1)
        self.assertIn("AC1", errors[0])

    def test_blocking_errors_skip_inactive_and_blank_rows(self):
        rows = [
            {"staff_id": "AC1", "full_name": "A", "basic_salary": 0, "status": "Terminated"},
            {"staff_id": "AC2", "full_name": "B", "basic_salary": 0, "status": "Inactive"},
            {"staff_id": "", "full_name": "", "basic_salary": 0, "status": ""},
        ]
        self.assertEqual(collect_blocking_errors(rows), [])

    def test_blocking_errors_header_label_company_name(self):
        """A header-label company name blocks only with corroborating shifted
        rows (worker names that read like headings/bare numbers — run 9). A
        header-label company over clean rows stays a warning, not a block:
        detect_company_name can grab a header cell off an aligned sheet."""
        shifted_rows = [
            {"staff_id": "AC1", "full_name": "0", "basic_salary": 1000, "status": "Active"},
            {"staff_id": "AC2", "full_name": "JOB TITLE", "basic_salary": 900, "status": "Active"},
        ]
        errors = collect_blocking_errors(shifted_rows, detected_company_name="GH CARD")
        self.assertEqual(len(errors), 1)
        self.assertIn("column heading", errors[0])

        clean_rows = [
            {"staff_id": "AC1", "full_name": "Ama Serwaa", "basic_salary": 1000, "status": "Active"}
        ]
        self.assertEqual(
            collect_blocking_errors(clean_rows, detected_company_name="Staff ID"), []
        )
        self.assertEqual(
            collect_blocking_errors(clean_rows, detected_company_name="ACS/GMT Shipping"),
            [],
        )

    def test_statutory_invariants_catch_net_over_gross_and_bad_ssnit(self):
        rate = self._rate_jan_2026()
        client = ClientCompany.query.first()
        run = PayrollRun(
            month="January", year=2026, status="Draft",
            client_company_id=client.id if client else None,
        )
        good = PayrollItem(
            staff_id="OK1", basic_salary=2000.00, ssnit=110.00,
            gross_pay=2000.00, net_pay=1700.00,
        )
        run.items.append(good)
        verify_statutory_invariants(run, rate)  # must not raise

        run.items.append(
            PayrollItem(
                staff_id="BAD1", basic_salary=2000.00, ssnit=110.00,
                gross_pay=1000.00, net_pay=1500.00,  # net > gross: run-9 defect
            )
        )
        with self.assertRaises(RuntimeError):
            verify_statutory_invariants(run, rate)

        run.items.pop()
        run.items.append(
            PayrollItem(
                staff_id="BAD2", basic_salary=2000.00, ssnit=55.00,  # not 5.5%
                gross_pay=2000.00, net_pay=1700.00,
            )
        )
        with self.assertRaises(RuntimeError):
            verify_statutory_invariants(run, rate)

    def test_net_may_exceed_gross_by_loan_advance_only(self):
        """loan_advance is the one legitimate way net lands above gross."""
        rate = self._rate_jan_2026()
        run = PayrollRun(month="January", year=2026, status="Draft")
        run.items.append(
            PayrollItem(
                staff_id="ADV1", basic_salary=2000.00, ssnit=110.00,
                gross_pay=2000.00, loan_advance=500.00, net_pay=2200.00,
            )
        )
        verify_statutory_invariants(run, rate)  # must not raise


if __name__ == "__main__":
    unittest.main()
