"""Independent rule examples and explicit unresolved-case guards."""
import unittest
from datetime import date
from types import SimpleNamespace

from app.models import StatutoryRate
from app.payroll_calculations.deductions import (
    DeductionContext, DeductionPolicyError, context_for_employee,
    deduction_adjustments,
)


class DeductionPolicyTests(unittest.TestCase):
    def setUp(self):
        self.rate = StatutoryRate(
            effective_from=date(2026, 9, 1), deductions_policy_version=1,
            ssf_employee_rate=0.055, ssf_employer_rate=0.13,
            overtime_rate_low=0.05, overtime_rate_high=0.10,
            overtime_basic_threshold=0.5, paye_bands_json="[]",
        )
        self.context = DeductionContext(period=date(2026, 10, 1))

    def compute(self, basic=1000, **kwargs):
        return deduction_adjustments(self.rate, basic, context=self.context, **kwargs)

    def test_mandatory_pension_split_and_employer_remittances_are_separate(self):
        result = self.compute()
        self.assertEqual(result.ssnit, 55)
        self.assertEqual(result.ssf_employer, 130)
        self.assertEqual(result.ssnit_remittance, 135)
        self.assertEqual(result.tier_2_remittance, 50)
        upper = self.compute(69000)
        self.assertEqual(upper.ssnit, 3795)
        self.assertEqual(upper.ssf_employer, 8970)
        self.assertEqual(upper.ssnit_remittance, 9315)
        self.assertEqual(upper.tier_2_remittance, 3450)

    def test_pension_outside_verified_range_and_minimum_rounding_require_confirmation(self):
        for basic in (100, 587.79, 587.80, 588, 69000.01):
            with self.subTest(basic=basic), self.assertRaises(DeductionPolicyError):
                self.compute(basic)
        zero = self.compute(0)
        self.assertEqual(zero.ssnit, 0)
        self.assertEqual(zero.ssf_employer, 0)

    def test_separately_rounded_pension_components_disclose_the_reconciliation_difference(self):
        result = self.compute(600.19)
        self.assertEqual(result.ssnit, 33.01)
        self.assertEqual(result.ssf_employer, 78.02)
        self.assertEqual(result.ssnit_remittance, 81.03)
        self.assertEqual(result.tier_2_remittance, 30.01)
        self.assertTrue(any("rounding needs reconciliation" in note for note in result.warnings))

    def test_unverified_year_or_nonstandard_split_cannot_reuse_notice_implicitly(self):
        self.context.period = date(2027, 1, 1)
        with self.assertRaisesRegex(DeductionPolicyError, "payroll year"):
            self.compute()
        self.context.period = date(2026, 10, 1)
        self.rate.ssf_employee_rate = 0.06
        with self.assertRaisesRegex(DeductionPolicyError, "separately documented"):
            self.compute()

    def test_employee_pf_cash_and_tax_allowance_are_distinct(self):
        unknown = self.compute(provident_fund=200)
        self.assertEqual(unknown.pf_allowable_employee, 0)
        self.assertTrue(unknown.warnings)
        self.context.pf_scheme_approved = True
        self.context.pf_scheme_reference = "NPRA-SCHEME-TEST"
        approved = self.compute(provident_fund=200)
        self.assertEqual(approved.pf_allowable_employee, 165)

    def test_shared_pf_cap_never_assumes_employee_or_employer_priority(self):
        self.context.pf_scheme_approved = True
        self.context.pf_scheme_reference = "NPRA-SCHEME-TEST"
        within = self.compute(provident_fund=50, employer_provident_fund=100)
        self.assertEqual(within.pf_allowable_employee, 50)
        self.assertEqual(within.pf_employer_taxable, 0)
        with self.assertRaisesRegex(DeductionPolicyError, "no contribution priority"):
            self.compute(provident_fund=100, employer_provident_fund=100)
        self.context.pf_scheme_approved = False
        with self.assertRaisesRegex(DeductionPolicyError, "scheme/reference"):
            self.compute(employer_provident_fund=100)

    def test_overtime_requires_both_annual_income_and_junior_evidence_for_same_year(self):
        self.context.is_junior = True
        self.context.qualifying_annual_income = 18000
        self.context.income_year = 2026
        qualified = self.compute(overtime_pay=600)
        # 500 at 5%, plus 100 at 10%, from the official GRA worked example.
        self.assertEqual(qualified.overtime_tax, 35)
        self.assertEqual(qualified.overtime_ordinary_pay, 0)
        for attribute, value in (
            ("is_junior", False), ("is_junior", None),
            ("qualifying_annual_income", 18000.01),
            ("qualifying_annual_income", None), ("income_year", 2025),
        ):
            old = getattr(self.context, attribute)
            setattr(self.context, attribute, value)
            result = self.compute(overtime_pay=600)
            self.assertEqual(result.overtime_tax, 0)
            self.assertEqual(result.overtime_ordinary_pay, 600)
            setattr(self.context, attribute, old)

    def test_approved_relief_amount_and_validity_override_uploaded_request(self):
        self.context.relief_approval_ref = "GRA-TRC-TEST"
        self.context.relief_valid_from = date(2026, 10, 1)
        self.context.relief_valid_to = date(2026, 12, 31)
        self.context.approved_relief_monthly = 100
        self.assertEqual(self.compute(requested_relief=999).tax_relief_applied, 100)
        self.context.period = date(2026, 9, 1)
        self.assertEqual(self.compute(requested_relief=999).tax_relief_applied, 0)
        self.context.period = date(2026, 10, 1)
        self.context.relief_approval_ref = ""
        self.assertEqual(self.compute(requested_relief=999).tax_relief_applied, 0)

    def test_relief_history_retains_prior_awards_and_explicit_revocation(self):
        award = lambda ident, amount, start, end: SimpleNamespace(
            id=ident, monthly_amount=amount, valid_from=start, valid_to=end,
            reference=f"TRC-{ident}",
        )
        employee = SimpleNamespace(
            overtime_is_junior=None, overtime_qualifying_annual_income=None,
            overtime_income_year=None, pf_scheme_approved=False,
            pf_scheme_reference=None, tax_relief_approvals=[
                award(1, 100, date(2026, 1, 1), date(2026, 12, 31)),
                award(2, 150, date(2026, 10, 1), date(2026, 12, 31)),
                award(3, 0, date(2026, 11, 1), date(2026, 12, 31)),
            ],
        )
        for month, amount in ((9, 100), (10, 150), (11, 0)):
            self.assertEqual(
                context_for_employee(employee, date(2026, month, 1)).approved_relief_monthly,
                amount,
            )

    def test_legacy_version_preserves_workbook_behavior_explicitly(self):
        self.rate.deductions_policy_version = 0
        legacy = self.compute(200, overtime_pay=500, provident_fund=500, requested_relief=100)
        self.assertEqual(legacy.ssnit, 11)
        self.assertEqual(legacy.ssf_employer, 26)
        self.assertEqual(legacy.pf_allowable_employee, 500)
        self.assertEqual(legacy.tax_relief_applied, 100)
        self.assertEqual(legacy.overtime_tax, 45)

    def test_negative_and_nonfinite_inputs_are_rejected_before_money_rounding(self):
        for kwargs in (
            {"provident_fund": -1}, {"employer_provident_fund": float("nan")},
            {"overtime_pay": float("inf")}, {"requested_relief": -1},
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises(DeductionPolicyError):
                self.compute(**kwargs)


if __name__ == "__main__":
    unittest.main()
