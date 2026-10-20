"""Independent calculation checks for documented payroll deduction inputs."""

import os
from pathlib import Path
import tempfile
import unittest
from datetime import date
from unittest.mock import patch

import openpyxl

from app import create_app, db
from app.models import (
    ClientCompany, Employee, EmployeeTaxReliefApproval, PayrollItem,
    PayrollRun, RawPayEntry, StatutoryRate, WageRateProfile,
)
from app.payroll_calculations import statutory_rate_for_run
from app.payroll_calculations.deductions import DeductionContext, DeductionPolicyError
from app.payroll_calculations.hourly import HourlyShiftCalculator
from app.payroll_calculations.salaried import SalariedCalculator
from app.raw_engine.compute import PayslipInputs, compute_payslip
from app.raw_engine.mapping import ELEMENT_SET
from app.raw_engine.run import compute_seed_month
from app.raw_engine.seed import RateSpec, SeedContext, SeedEmployee
from app.raw_engine.template import generate_monthly_template
from app.raw_engine.thin import (
    ThinEmployeeInput, join_and_compute, parse_thin_workbook, write_thin_workbook,
)
from app.seed import seed_statutory_rates


PERIOD = date(2026, 9, 1)


class DeductionCalculatorTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {
            "APP_ENV": "development", "FLASK_ENV": "development",
            "DATABASE_URL": "sqlite:///:memory:", "SKIP_DOTENV": "true",
            "PERSISTENCE_REQUIRED": "false", "AUTO_INIT_DB": "false",
            "DISTRIBUTION_WORKER_INLINE": "false",
            "PAYROLLA_INSTANCE_PATH": self.temp_dir.name,
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        self.addCleanup(self.temp_dir.cleanup)
        self.app = create_app()
        self.app.config["TESTING"] = True
        self.ctx = self.app.app_context()
        self.ctx.push()
        self.addCleanup(self.ctx.pop)
        self.addCleanup(db.session.remove)
        db.create_all()
        seed_statutory_rates()
        db.session.flush()
        self.rate = StatutoryRate.active_for(PERIOD)
        self.assertEqual(self.rate.deductions_policy_version, 1)

    @staticmethod
    def context(**overrides):
        values = {"period": PERIOD}
        values.update(overrides)
        return DeductionContext(**values)

    def both(self, basic, *, context=None, pf=0, employer_pf=0, relief=0, overtime=0, allowances=0, rate=None):
        rate = rate or self.rate
        salary = SalariedCalculator(rate).calculate(
            basic, pf_fund_employee=pf, pf_fund_employer=employer_pf,
            tax_relief_monthly=relief, overtime_pay=overtime,
            other_allowances=allowances, calculation_context=context,
        )
        raw = compute_payslip(PayslipInputs(
            staff_id="DEDUCTION", basic_wage=basic, provident_fund=pf,
            pf_fund_employer=employer_pf, tax_relief_monthly=relief,
            overtime_pay=overtime, allowances=allowances, context=context,
        ), rate)
        self.assertEqual(salary.taxable_income, raw.taxable_income)
        self.assertEqual(salary.paye, raw.total_tax)
        self.assertEqual(salary.net_pay, raw.net_pay)
        return salary, raw

    def employee_run(self, *, basic=5000, relief=0, **metadata):
        company = ClientCompany(name="Deduction regression company")
        db.session.add(company)
        db.session.flush()
        employee = Employee(
            staff_id="DEDUCTION", full_name="Deduction regression worker",
            client_company_id=company.id, basic_salary=basic,
            tax_relief_monthly=relief, pay_type="salaried", **metadata,
        )
        run = PayrollRun(
            client_company_id=company.id, month="September", year=2026,
            upload_type="raw", status="Draft",
        )
        db.session.add_all([employee, run])
        db.session.flush()
        return employee, run

    def add_award(self, employee, amount=100):
        award = EmployeeTaxReliefApproval(
            employee_id=employee.id, monthly_amount=amount, reference="GRA-TEST-AWARD",
            category="Recorded approval", valid_from=PERIOD, valid_to=date(2026, 9, 30),
        )
        db.session.add(award)
        db.session.flush()
        return award

    def test_unapproved_pf_is_cash_deduction_and_does_not_reduce_taxable_income(self):
        for result in self.both(5000, context=self.context(), pf=1000):
            self.assertEqual(result.pf_allowable_employee, 0)
            self.assertEqual(result.taxable_income, 4725)
            self.assertEqual(result.ordinary_paye, 785.75)
            self.assertEqual(result.net_pay, 2939.25)
            self.assertTrue(result.warnings)

    def test_approved_pf_caps_tax_deduction_but_keeps_full_cash_deduction(self):
        context = self.context(pf_scheme_approved=True, pf_scheme_reference="APPROVED-SCHEME")
        for result in self.both(5000, context=context, pf=1000):
            # 5,000 * 16.5% = 825 allowed; all 1,000 is withheld from cash.
            self.assertEqual(result.pf_allowable_employee, 825)
            self.assertEqual(result.taxable_income, 3900)
            self.assertEqual(result.ordinary_paye, 579.50)
            self.assertEqual(result.net_pay, 3145.50)
            fields = result.as_payroll_item_fields()
            self.assertEqual(fields["pf_fund_employee"], 1000)
            self.assertEqual(fields["pf_allowable_employee"], 825)
            self.assertEqual(fields["deductions_policy_version"], 1)

    def test_employer_pf_within_combined_cap_is_employer_cost(self):
        context = self.context(pf_scheme_approved=True, pf_scheme_reference="APPROVED-SCHEME")
        for result in self.both(5000, context=context, pf=500, employer_pf=100):
            self.assertEqual(result.pf_allowable_employee, 500)
            self.assertEqual(result.pf_fund_employer, 100)
            self.assertEqual(result.pf_employer_taxable, 0)
            self.assertEqual(result.taxable_income, 4225)
            self.assertEqual(result.net_pay, 3564.25)

    def test_unresolved_combined_pf_allocation_blocks_both_calculators(self):
        context = self.context(pf_scheme_approved=True, pf_scheme_reference="APPROVED-SCHEME")
        for calculate in (
            lambda: SalariedCalculator(self.rate).calculate(5000, pf_fund_employee=500,
                pf_fund_employer=400, calculation_context=context),
            lambda: compute_payslip(PayslipInputs(staff_id="PF", basic_wage=5000,
                provident_fund=500, pf_fund_employer=400, context=context), self.rate),
        ):
            with self.assertRaisesRegex(DeductionPolicyError, "allocation"):
                calculate()

    def test_only_evidenced_junior_with_current_year_income_gets_overtime_concession(self):
        context = self.context(is_junior=True, qualifying_annual_income=18000, income_year=2026)
        for result in self.both(1000, context=context, overtime=700):
            self.assertEqual(result.taxable_income, 945)
            self.assertEqual(result.overtime_tax, 45)
            self.assertEqual(result.overtime_ordinary_pay, 0)
            self.assertEqual(result.ordinary_paye, 44.98)
            self.assertEqual(result.net_pay, 1555.02)

    def test_missing_nonjunior_excess_income_or_stale_year_routes_overtime_to_ordinary(self):
        contexts = (
            self.context(),
            self.context(is_junior=False, qualifying_annual_income=18000, income_year=2026),
            self.context(is_junior=True, qualifying_annual_income=18000.01, income_year=2026),
            self.context(is_junior=True, qualifying_annual_income=18000, income_year=2025),
        )
        for context in contexts:
            with self.subTest(context=context):
                for result in self.both(1000, context=context, overtime=700):
                    self.assertEqual(result.taxable_income, 1645)
                    self.assertEqual(result.overtime_tax, 0)
                    self.assertEqual(result.overtime_ordinary_pay, 700)
                    self.assertEqual(result.ordinary_paye, 167.48)
                    self.assertEqual(result.net_pay, 1477.52)
                    self.assertTrue(result.warnings)

    def test_only_dated_approved_relief_amount_is_used(self):
        context = self.context(relief_approval_ref="GRA-AWARD", relief_valid_from=PERIOD,
            relief_valid_to=date(2026, 9, 30), approved_relief_monthly=100)
        for result in self.both(1000, context=context, relief=999):
            self.assertEqual(result.tax_relief_applied, 100)
            self.assertEqual(result.taxable_income, 845)
            self.assertEqual(result.ordinary_paye, 27.48)
            self.assertEqual(result.net_pay, 917.52)
        for invalid in (
            self.context(approved_relief_monthly=100),
            self.context(relief_approval_ref="GRA-OLD", relief_valid_from=date(2026, 1, 1),
                relief_valid_to=date(2026, 8, 31), approved_relief_monthly=100),
        ):
            for result in self.both(1000, context=invalid, relief=100):
                self.assertEqual(result.tax_relief_applied, 0)
                self.assertEqual(result.taxable_income, 945)

    def test_relief_cannot_exceed_chargeable_income_or_make_taxable_income_negative(self):
        context = self.context(relief_approval_ref="GRA-AWARD", relief_valid_from=PERIOD,
            relief_valid_to=date(2026, 9, 30), approved_relief_monthly=2000)
        for result in self.both(1000, context=context, relief=2000):
            self.assertEqual(result.tax_relief_applied, 945)
            self.assertEqual(result.taxable_income, 0)
            self.assertEqual(result.ordinary_paye, 0)
        for result in self.both(0, context=context, relief=2000):
            self.assertEqual(result.tax_relief_applied, 0)
            self.assertEqual(result.taxable_income, 0)
            self.assertEqual(result.net_pay, 0)

    def test_pension_split_is_persisted_without_extra_employee_cash_deduction(self):
        for result in self.both(600, context=self.context()):
            fields = result.as_payroll_item_fields()
            self.assertEqual(fields["ssnit"], 33)
            self.assertEqual(fields["ssf_employer"], 78)
            self.assertEqual(fields["pensionable_salary"], 600)
            self.assertEqual(fields["ssnit_remittance"], 81)
            self.assertEqual(fields["tier_2_remittance"], 30)
            self.assertEqual(result.net_pay, 567)

    def test_unresolved_pension_edges_and_negative_inputs_are_rejected(self):
        for basic in (587.79, 587.80, 69000.01, -1):
            with self.subTest(basic=basic):
                with self.assertRaises(DeductionPolicyError):
                    self.both(basic, context=self.context())
        for kwargs in ({"pf": -1}, {"employer_pf": -1}, {"overtime": -1}):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(DeductionPolicyError):
                    self.both(1000, context=self.context(), **kwargs)

    def test_historical_version_retains_previous_workbook_calculation(self):
        legacy = StatutoryRate.active_for(date(2026, 1, 1))
        for result in self.both(1000, pf=100, relief=100, overtime=700, rate=legacy):
            self.assertEqual(result.taxable_income, 745)
            self.assertEqual(result.overtime_tax, 45)
            self.assertEqual(result.ordinary_paye, 21.13)
            self.assertEqual(result.net_pay, 1478.87)
            self.assertEqual(result.deductions_policy_version, 0)

    def test_hourly_uses_current_run_pf_cash_and_stored_approval_history(self):
        employee, run = self.employee_run(relief=999, pf_scheme_approved=True,
            pf_scheme_reference="APPROVED-SCHEME")
        self.add_award(employee)
        db.session.add(PayrollItem(payroll_run_id=run.id, employee_id=employee.id,
            staff_id=employee.staff_id, pf_fund_employee=1000))
        for code, category, amount in (("BASE", "basic", 5000), ("OT", "overtime", 500)):
            db.session.add(WageRateProfile(client_company_id=run.client_company_id,
                pay_code=code, category=category, hourly_rate=amount))
            db.session.add(RawPayEntry(payroll_run_id=run.id,
                employee_id_str=employee.staff_id, pay_code=code, hours=1))
        db.session.flush()
        result = HourlyShiftCalculator(run, statutory_rate_for_run(run)).calculate_run()[employee.staff_id]
        self.assertEqual(result.pf_fund_employee, 1000)
        self.assertEqual(result.pf_allowable_employee, 825)
        self.assertEqual(result.tax_relief_applied, 100)
        self.assertEqual(result.overtime_ordinary_pay, 500)
        self.assertEqual(result.taxable_income, 4300)
        self.assertEqual(result.paye, 679.50)
        self.assertEqual(result.net_pay, 3545.50)

    def test_thin_pf_and_other_allowance_round_trip_and_calculate(self):
        employee, run = self.employee_run(pf_scheme_approved=True,
            pf_scheme_reference="APPROVED-SCHEME")
        path = Path(self.temp_dir.name) / "monthly.xlsx"
        write_thin_workbook(path, [ThinEmployeeInput(staff_id=employee.staff_id,
            provident_fund=1000, other_allowance=200)])
        records, warnings = parse_thin_workbook(path)
        self.assertEqual(warnings, [])
        self.assertEqual(records[0].provident_fund, 1000)
        self.assertEqual(records[0].other_allowance, 200)
        result = join_and_compute(records, run, self.rate).payslips[employee.staff_id]
        self.assertEqual(result.gross_pay, 5200)
        self.assertEqual(result.pf_allowable_employee, 825)
        self.assertEqual(result.taxable_income, 4100)
        self.assertEqual(result.total_tax, 629.50)
        self.assertEqual(result.net_pay, 3295.50)

    def test_downloaded_template_keeps_reference_columns_aligned_after_new_adjustments(self):
        employee, run = self.employee_run(bank_name="Regression Bank", bank_account_number="00123",
            icu_member=True)
        code, label, category = ELEMENT_SET[0]
        db.session.add(WageRateProfile(client_company_id=run.client_company_id,
            pay_code=code, hourly_rate=10, category=category))
        db.session.flush()
        path = generate_monthly_template(run.client_company_id, self.temp_dir.name, "September", 2026)
        workbook = openpyxl.load_workbook(path, read_only=True, data_only=True)
        try:
            header, row = list(workbook.active.iter_rows(values_only=True))
            values = dict(zip(header, row))
            self.assertEqual(values["Employee Provident Fund"], 0)
            self.assertEqual(values["Employer Provident Fund"], 0)
            self.assertEqual(values["Other Allowance"], 0)
            self.assertEqual(values["ICU Member"], "Member")
            self.assertEqual(values["Bank"], "Regression Bank")
            self.assertEqual(values["A/C No."], "00123")
        finally:
            workbook.close()

    def test_seed_month_uses_saved_employee_approvals_and_run_period(self):
        employee, run = self.employee_run(basic=1000, relief=999,
            pf_scheme_approved=True, pf_scheme_reference="APPROVED-SCHEME",
            overtime_is_junior=True, overtime_qualifying_annual_income=18000,
            overtime_income_year=2026)
        self.add_award(employee)
        parsed_employee = SeedEmployee(staff_id=employee.staff_id, full_name=employee.full_name,
            basic_salary=1000, provident_fund=100, tax_relief_monthly=999,
            rates=[RateSpec(pay_code="OT", category="overtime", hourly_rate=500, description="OT")],
            raw_hours={"OT": 1})
        parsed = SeedContext(client_company_id=run.client_company_id,
            source_filename="test.xlsx", employees=[parsed_employee])
        result = compute_seed_month(parsed, self.rate, payroll_run=run)[employee.staff_id]
        self.assertEqual(result.employee_id, employee.id)
        self.assertEqual(result.pf_allowable_employee, 100)
        self.assertEqual(result.tax_relief_applied, 100)
        self.assertEqual(result.overtime_tax, 25)
        self.assertEqual(result.taxable_income, 745)
        self.assertEqual(result.total_tax, 36.70)
        self.assertEqual(result.net_pay, 1308.30)
        with self.assertRaisesRegex(DeductionPolicyError, "payroll run"):
            compute_seed_month(parsed, self.rate)


if __name__ == "__main__":
    unittest.main()
