"""Employer evidence, persisted deductions and GRA arithmetic across real workflows."""
import os
import json
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch
from flask import g

from app import create_app, db
from app.employees import parse_deduction_metadata, save_deduction_metadata
from app.excel_utils import _fill_gra_skeleton_workbook, mapped_rows_from_dataframe, map_columns
from app.validators import collect_blocking_errors
from app.models import ClientCompany, Employee, PayrollItem, PayrollRun, StatutoryRate, User
from app.payroll import recalculate_salaried_items
from app.payroll_calculations.deductions import context_for_employee, DeductionPolicyError
from app.payroll_calculations.salaried import SalariedCalculator
from app.seed import seed_statutory_rates


class DeductionWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.env = patch.dict(os.environ, {
            "APP_ENV": "development", "FLASK_ENV": "development",
            "DATABASE_URL": "sqlite:///:memory:", "SKIP_DOTENV": "true",
            "PERSISTENCE_REQUIRED": "false", "AUTO_INIT_DB": "false",
            "DISTRIBUTION_WORKER_INLINE": "false", "WTF_CSRF_ENABLED": "false",
            "PAYROLLA_INSTANCE_PATH": self.temp.name,
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        self.app = create_app()
        self.app.config["TESTING"] = True
        self.ctx = self.app.app_context()
        self.ctx.push()
        self.addCleanup(self.ctx.pop)
        db.create_all()
        self.addCleanup(db.session.remove)
        self.company = ClientCompany(name="Deduction Workflow Company")
        db.session.add(self.company)
        db.session.flush()
        self.operator = User(name="Workflow operator", email="workflow@example.test", role="admin", password_hash="unused-test-value")
        self.tenant = User(name="Workflow tenant", email="tenant@example.test", role="client_admin", password_hash="unused-test-value", client_company_id=self.company.id)
        db.session.add_all([self.operator, self.tenant])
        seed_statutory_rates()
        db.session.commit()
        self.client = self.app.test_client()

    def login(self, user):
        # This test owns an outer app context. Clear Flask-Login's cached user
        # when switching roles; real requests get a fresh application context.
        g.pop("_login_user", None)
        with self.client.session_transaction() as session:
            session["_user_id"] = str(user.id)
            session["_fresh"] = True

    def metadata(self, **changes):
        form = {
            "tax_relief": "40", "tax_relief_approval_ref": "GRA-WORKFLOW-2026",
            "tax_relief_category": "Approved personal relief",
            "tax_relief_valid_from": "2026-09-01", "tax_relief_valid_to": "2026-12-31",
            "pf_scheme_approved": "true", "pf_scheme_reference": "NPRA-WORKFLOW",
            "overtime_is_junior": "", "overtime_qualifying_annual_income": "",
            "overtime_income_year": "",
        }
        form.update(changes)
        return form

    def employee(self):
        employee = Employee(staff_id="POL001", full_name="Policy Worker", client_company_id=self.company.id, basic_salary=2000)
        values, errors = parse_deduction_metadata(self.metadata())
        self.assertFalse(errors)
        save_deduction_metadata(employee, values)
        db.session.add(employee)
        db.session.flush()
        return employee

    def test_metadata_requires_evidence_and_finite_amounts(self):
        for form, field in (
            ({"tax_relief": "20"}, "tax_relief_approval_ref"),
            ({"tax_relief": "nan"}, "tax_relief"),
            ({"overtime_qualifying_annual_income": "inf", "overtime_income_year": "2026"}, "overtime_qualifying_annual_income"),
            ({"overtime_qualifying_annual_income": "18000"}, "overtime_income_year"),
            ({"pf_scheme_approved": "true"}, "pf_scheme_reference"),
            (self.metadata(tax_relief_valid_to="2026-01-01"), "tax_relief_valid_to"),
        ):
            with self.subTest(form=form):
                self.assertIn(field, parse_deduction_metadata(form)[1])
        unknown, _ = parse_deduction_metadata({"overtime_is_junior": ""})
        no, _ = parse_deduction_metadata({"overtime_is_junior": "false"})
        self.assertIsNone(unknown["overtime_is_junior"])
        self.assertIs(no["overtime_is_junior"], False)

    def test_relief_changes_and_explicit_withdrawal_retain_period_history(self):
        employee = self.employee()
        revised = self.metadata(tax_relief="60", tax_relief_valid_from="2026-10-01", tax_relief_approval_ref="GRA-REVISION")
        values, errors = parse_deduction_metadata(revised, employee)
        self.assertFalse(errors)
        save_deduction_metadata(employee, values)
        db.session.flush()
        self.assertEqual(context_for_employee(employee, date(2026, 9, 1)).approved_relief_monthly, 40)
        self.assertEqual(context_for_employee(employee, date(2026, 10, 1)).approved_relief_monthly, 60)
        # Reposting the same document does not create another historical entry.
        save_deduction_metadata(employee, values)
        db.session.flush()
        self.assertEqual(len(employee.tax_relief_approvals), 2)
        withdrawn = self.metadata(tax_relief="0", tax_relief_valid_from="2026-11-01", tax_relief_approval_ref="GRA-WITHDRAWAL")
        values, errors = parse_deduction_metadata(withdrawn, employee)
        self.assertFalse(errors)
        save_deduction_metadata(employee, values)
        db.session.flush()
        self.assertEqual(context_for_employee(employee, date(2026, 11, 1)).approved_relief_monthly, 0)
        self.assertEqual(context_for_employee(employee, date(2026, 10, 1)).approved_relief_monthly, 60)
        self.assertEqual(len(employee.tax_relief_approvals), 3)

    def test_both_roles_can_clear_unapproved_legacy_relief_without_award_history(self):
        for user in (self.operator, self.tenant):
            with self.subTest(role=user.role):
                employee = Employee(
                    staff_id=f"LEGACY-{user.id}", full_name="Legacy Worker",
                    client_company_id=self.company.id, basic_salary=2000,
                    tax_relief_monthly=125,
                )
                db.session.add(employee)
                db.session.commit()
                self.assertEqual(employee.tax_relief_approvals, [])
                self.login(user)
                path = (
                    f"/employees/clients/{self.company.id}/edit/{employee.id}"
                    if user is self.operator
                    else f"/company/employees/{employee.id}/edit"
                )
                response = self.client.post(path, data={
                    "staff_id": employee.staff_id, "full_name": employee.full_name,
                    "basic_salary": "2000", "tax_relief": "0",
                    "tax_relief_approval_ref": "", "tax_relief_category": "",
                    "tax_relief_valid_from": "", "tax_relief_valid_to": "",
                })
                self.assertEqual(response.status_code, 302)
                db.session.refresh(employee)
                self.assertEqual(employee.tax_relief_monthly, 0)
                self.assertEqual(employee.tax_relief_approvals, [])
                self.assertIsNone(employee.tax_relief_approval_ref)
                self.assertEqual(
                    context_for_employee(employee, date(2026, 10, 1)).approved_relief_monthly, 0,
                )

    def test_clearing_approved_relief_still_requires_withdrawal_evidence(self):
        employee = self.employee()
        evidence_fields = (
            "tax_relief_approval_ref", "tax_relief_category",
            "tax_relief_valid_from", "tax_relief_valid_to",
        )
        cleared = {name: "" for name in evidence_fields}
        cleared["tax_relief"] = "0"
        _values, errors = parse_deduction_metadata(cleared, employee)
        self.assertEqual(set(errors), set(evidence_fields))
        self.assertEqual(employee.tax_relief_monthly, 40)
        self.assertEqual(len(employee.tax_relief_approvals), 1)
        # A documented award staged on the roster also needs a withdrawal,
        # even before its history row is recorded.
        staged = Employee(tax_relief_monthly=40, **{
            name: getattr(employee, name) for name in evidence_fields
        })
        self.assertEqual(
            set(parse_deduction_metadata(cleared, staged)[1]), set(evidence_fields),
        )

    def test_operator_form_rejects_unapproved_relief_then_saves_evidence(self):
        self.login(self.operator)
        path = f"/employees/clients/{self.company.id}/add"
        invalid = self.client.post(path, data={"staff_id": "POL2", "full_name": "New Worker", "tax_relief": "40"})
        self.assertEqual(invalid.status_code, 200)
        self.assertIsNone(Employee.query.filter_by(staff_id="POL2").first())
        valid = self.client.post(path, data=dict(self.metadata(), staff_id="POL2", full_name="New Worker"))
        self.assertEqual(valid.status_code, 302)
        employee = Employee.query.filter_by(staff_id="POL2").one()
        self.assertEqual(len(employee.tax_relief_approvals), 1)
        self.assertEqual(employee.tax_relief_approvals[0].reference, "GRA-WORKFLOW-2026")
        self.assertIsNone(employee.overtime_is_junior)

    def test_tenant_form_captures_same_evidence_and_keeps_company_binding(self):
        self.login(self.tenant)
        response = self.client.post("/company/employees/add", data=dict(self.metadata(), staff_id="TEN1", full_name="Tenant Worker", client_company_id="999"))
        self.assertEqual(response.status_code, 302)
        employee = Employee.query.filter_by(staff_id="TEN1").one()
        self.assertEqual(employee.client_company_id, self.company.id)
        self.assertEqual(len(employee.tax_relief_approvals), 1)
        page = self.client.get(f"/company/employees/{employee.id}/edit").get_data(as_text=True)
        self.assertIn('name="overtime_income_year"', page)
        self.assertIn('name="tax_relief_valid_to"', page)

    def test_standard_recalculation_snapshots_evidence_and_employer_cost(self):
        employee = self.employee()
        run = PayrollRun(month="September", year=2026, client_company_id=self.company.id, status="Draft")
        db.session.add(run)
        db.session.flush()
        item = PayrollItem(payroll_run_id=run.id, employee_id=employee.id, staff_id=employee.staff_id, full_name=employee.full_name, basic_salary=2000, other_allowances=200, overtime_pay=500, productivity_bonus=5000, pf_fund_employee=100, pf_fund_employer=200)
        db.session.add(item)
        db.session.flush()
        rate = StatutoryRate.active_for(date(2026, 9, 1))
        recalculate_salaried_items(run, rate)
        self.assertEqual(item.deductions_policy_version, 1)
        self.assertEqual(item.tax_relief_applied, 40)
        self.assertEqual(item.pf_allowable_employee, 100)
        self.assertEqual(item.overtime_ordinary_pay, 500)
        self.assertEqual(item.pensionable_salary, 2000)
        self.assertEqual(item.ssnit_remittance, 270)
        self.assertEqual(item.tier_2_remittance, 100)
        self.assertEqual(item.total_deductions, round(item.paye + item.ssnit + 100, 2))
        self.assertEqual(item.net_pay, round(item.gross_pay - item.total_deductions, 2))
        self.assertIn("ordinary PAYE", item.warning_notes)
        employee.pf_scheme_approved = False
        item.pf_fund_employer = 0
        recalculate_salaried_items(run, rate)
        self.assertIn("approved scheme/reference is missing", item.warning_notes)
        employee.pf_scheme_approved = True
        recalculate_salaried_items(run, rate)
        self.assertNotIn("approved scheme/reference is missing", item.warning_notes)

    def test_policy_failure_does_not_partly_recalculate_standard_run(self):
        employee = self.employee()
        run = PayrollRun(month="September", year=2026, client_company_id=self.company.id)
        db.session.add(run)
        db.session.flush()
        first = PayrollItem(payroll_run_id=run.id, employee_id=employee.id, staff_id="FIRST", full_name="First", basic_salary=2000, paye=123)
        invalid = PayrollItem(payroll_run_id=run.id, staff_id="SECOND", full_name="Second", basic_salary=2000, pf_fund_employer=100, paye=456)
        db.session.add_all([first, invalid])
        db.session.flush()
        with self.assertRaises(RuntimeError):
            recalculate_salaried_items(run, StatutoryRate.active_for(date(2026, 9, 1)))
        self.assertEqual(first.paye, 123)
        self.assertEqual(invalid.paye, 456)

    def test_employer_pf_import_cannot_silently_convert_nonfinite_amount_to_zero(self):
        import pandas as pd

        for bad_value in ("inf", "nan", -1):
            frame = pd.DataFrame([{"Staff ID": "PF1", "Name": "PF Worker", "Basic": 2000, "Employer Provident Fund": bad_value}])
            rows = mapped_rows_from_dataframe(frame, map_columns(frame.columns))
            self.assertTrue(any("provident fund" in error for error in collect_blocking_errors(rows)))

    def test_gra_export_uses_stored_relief_pf_and_official_column_arithmetic(self):
        employee = self.employee()
        rate = StatutoryRate.active_for(date(2026, 9, 1))
        for qualified in (False, True):
            with self.subTest(qualified=qualified):
                employee.overtime_is_junior = qualified
                employee.overtime_qualifying_annual_income = 18000
                employee.overtime_income_year = 2026
                context = context_for_employee(employee, date(2026, 9, 1))
                basic = 1000 if qualified else 2000
                result = SalariedCalculator(rate).calculate(basic, other_allowances=200, overtime_pay=600, productivity_bonus=5000, pf_fund_employee=50, pf_fund_employer=100, calculation_context=context)
                item = PayrollItem(staff_id="SNAP1", full_name="Snapshot Worker", employee=employee, **result.as_payroll_item_fields())
                run = PayrollRun(month="September", year=2026, client_company_id=self.company.id)
                run.items.append(item)
                # Export must not use a later edit of the employee's relief.
                employee.tax_relief_monthly = 99999
                workbook = _fill_gra_skeleton_workbook(run, "", "")
                sheet = workbook.active
                self.assertEqual(sheet["T18"].value, 40)
                self.assertEqual(sheet["J18"].value, 50)
                self.assertEqual(sheet["O18"].value, round(sheet["F18"].value + sheet["K18"].value + sheet["N18"].value, 2))
                self.assertEqual(sheet["S18"].value, sheet["O18"].value)
                self.assertEqual(sheet["U18"].value, round(sheet["I18"].value + sheet["J18"].value + sheet["T18"].value, 2))
                self.assertEqual(sheet["V18"].value, round(sheet["S18"].value - sheet["U18"].value, 2))
                self.assertEqual(sheet["Z18"].value, round(sheet["M18"].value + sheet["W18"].value + sheet["Y18"].value, 2))
                self.assertEqual(sheet["X18"].value, 600 if qualified else 0)
                workbook.close()

    def test_legacy_gra_export_remains_unchanged(self):
        item = PayrollItem(staff_id="OLD1", full_name="Historical Worker", basic_salary=1000, gross_pay=2000, pf_fund_employee=100, overtime_pay=600, ssnit=55, taxable_income=845, paye=200)
        run = PayrollRun(month="January", year=2026)
        run.items.append(item)
        workbook = _fill_gra_skeleton_workbook(run, "", "")
        self.assertEqual(workbook.active["O18"].value, 2000)
        self.assertEqual(workbook.active["J18"].value, 100)
        self.assertEqual(workbook.active["T18"].value, "")
        self.assertEqual(workbook.active["X18"].value, 600)
        workbook.close()

    def test_raw_policy_failure_returns_actionable_error_and_rolls_back_for_both_roles(self):
        bin_path = Path(self.temp.name) / "staged.bin"
        meta_path = Path(self.temp.name) / "staged.json"
        bin_path.write_bytes(b"isolated staged input")
        meta_path.write_text(json.dumps({
            "client_company_id": self.company.id, "month": "September",
            "year": 2026, "filename": "test.xlsx", "mode": "thin",
        }), encoding="utf-8")
        for route, module, session_key, user in (
            ("/company/runs/raw/confirm", "app.client.raw", "client_raw_token", self.tenant),
            ("/raw/confirm", "app.raw_engine.web", "raw_web_token", self.operator),
        ):
            with self.subTest(route=route):
                self.login(user)
                with self.client.session_transaction() as session:
                    session[session_key] = "policy-review-test"
                with patch(module + "._stage_paths", return_value=(str(bin_path), str(meta_path))), \
                        patch(module + ".parse_thin_workbook", return_value=([], [])), \
                        patch(module + ".join_and_compute", side_effect=DeductionPolicyError("Confirm pension allocation")):
                    response = self.client.post(route, data={"token": "policy-review-test"})
                self.assertEqual(response.status_code, 400)
                self.assertIn("Confirm pension allocation", response.get_json()["error"])
                self.assertIn("Nothing was written", response.get_json()["error"])
                self.assertEqual(PayrollRun.query.count(), 0)

    def test_payroll_grid_accepts_employer_pf_but_rejects_nonfinite_and_derived_changes(self):
        self.login(self.operator)
        run = PayrollRun(month="September", year=2026, client_company_id=self.company.id, status="Draft")
        db.session.add(run)
        db.session.flush()
        item = PayrollItem(payroll_run_id=run.id, staff_id="GRID1", full_name="Grid Worker", basic_salary=2000, pf_allowable_employee=10)
        db.session.add(item)
        db.session.commit()
        path = f"/payroll/runs/{run.id}/items/edit"
        html = self.client.get(path).get_data(as_text=True)
        self.assertIn("Employer PF (company contribution)", html)
        self.client.post(path, data={f"item-{item.id}-pf_fund_employer": "100", f"item-{item.id}-pf_allowable_employee": "999"})
        self.assertEqual(item.pf_fund_employer, 100)
        self.assertEqual(item.pf_allowable_employee, 10)
        self.client.post(path, data={f"item-{item.id}-pf_fund_employer": "nan"})
        self.assertEqual(item.pf_fund_employer, 100)


if __name__ == "__main__":
    unittest.main()
