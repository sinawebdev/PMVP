"""Post-launch fix regression tests.

Covers four production bugs Sina hit on the live deploy:
  1. Sheet-title crash on client names with '/', ':', or brackets.
  2. Rejected runs could not be hard-deleted or replaced on reupload.
  3. Dashboard SSNIT total omitted the employer 13% share.
  4. No hard-delete for employees (only deactivate/reactivate).
"""
import os
import tempfile
import unittest

os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["SEED_DEMO_DATA"] = "true"

from openpyxl import load_workbook

from app import create_app, db, format_ghana_cedis
from app.excel_utils import (
    export_bank_listing,
    export_gra_paye_schedule,
    export_payroll_run,
    export_wages_sheet,
    safe_sheet_title,
)
from app.employees import employee_delete_blockers
from app.models import (
    AuditTrail,
    ClientCompany,
    Employee,
    PayrollItem,
    PayrollRun,
)
from app.payroll import (
    DELETABLE_STATUSES,
    payroll_run_delete_blockers,
    replace_existing_runs,
)
from app.payroll_status import APPROVED, DRAFT, REJECTED


class SheetTitleSanitizationTestCase(unittest.TestCase):
    """Item 1: create_workbook must never crash on forbidden sheet-title chars."""

    def test_forbidden_characters_replaced_with_space(self):
        # Every character openpyxl rejects: \ / ? * [ ] :
        self.assertEqual(safe_sheet_title("ACS/GMT Shipping"), "ACS GMT Shipping")
        self.assertEqual(safe_sheet_title("Books: 2026"), "Books 2026")
        self.assertEqual(safe_sheet_title("Data [Q1]"), "Data Q1")
        self.assertEqual(safe_sheet_title(r"a\b?c*d"), "a b c d")

    def test_truncated_to_31_and_never_empty(self):
        self.assertLessEqual(len(safe_sheet_title("X" * 50)), 31)
        self.assertEqual(safe_sheet_title("///"), "Sheet")
        self.assertEqual(safe_sheet_title(""), "Sheet")

    def test_original_title_preserved_in_body_cell(self):
        from app.excel_utils import create_workbook

        workbook, sheet = create_workbook("ACS/GMT Shipping Payroll July 2026")
        # Tab name is sanitized...
        self.assertNotIn("/", sheet.title)
        # ...but the A2 header cell keeps the real client name, slash included.
        self.assertEqual(sheet["A2"].value, "ACS/GMT Shipping Payroll July 2026")


class SlashClientExportTestCase(unittest.TestCase):
    """Item 1: all four exports must succeed for a client whose name has '/'."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.app = create_app()
        self.app.config["TESTING"] = True
        self.ctx = self.app.app_context()
        self.ctx.push()
        # "ACS/GMT Shipping" is a real seeded client — the exact one whose slash
        # crashed the exports in production. Reuse it (name is unique).
        self.client_co = ClientCompany.query.filter_by(name="ACS/GMT Shipping").first()
        self.assertIsNotNone(self.client_co, "seed should include ACS/GMT Shipping")
        self.run = PayrollRun(
            client_company_id=self.client_co.id, month="July", year=2026,
            status=APPROVED, total_gross_pay=3061.30, total_paye=382.63,
            total_ssnit=150.56, total_ssnit_employer=355.86,
            total_deductions=633.19, total_net_pay=2428.11,
        )
        db.session.add(self.run)
        db.session.flush()
        db.session.add(
            PayrollItem(
                payroll_run_id=self.run.id, staff_id="AC605",
                full_name="Sampson Kluvie", basic_salary=2737.37,
                transport_allowance=323.93, gross_pay=3061.30, paye=382.63,
                ssnit=150.56, ssf_employer=355.86, net_basic_wage=2586.81,
                annual_salary=32848.44, annual_salary_15pct=4927.27,
                total_deductions=633.19, net_pay=2428.11, bank_name="GCB",
                bank_account_number="123",
            )
        )
        db.session.commit()

    def tearDown(self):
        self.ctx.pop()
        self.temp_dir.cleanup()

    def _assert_opens(self, path):
        # "Didn't throw" is not enough — the file must actually load and its
        # sheet title must be free of forbidden characters.
        wb = load_workbook(path)
        for ch in r"\/?*[]:":
            self.assertNotIn(ch, wb.active.title)

    def test_all_four_exports_no_crash_and_openable(self):
        for exporter in (
            export_payroll_run,
            export_bank_listing,
            export_wages_sheet,
            export_gra_paye_schedule,
        ):
            with self.subTest(exporter=exporter.__name__):
                path = exporter(self.run, self.temp_dir.name)
                self._assert_opens(path)


class RejectedRunDeletionTestCase(unittest.TestCase):
    """Item 2: Rejected runs are deletable and replaceable."""

    def setUp(self):
        self.app = create_app()
        self.app.config["TESTING"] = True
        self.ctx = self.app.app_context()
        self.ctx.push()
        self.client_co = ClientCompany.query.first()

    def tearDown(self):
        db.session.rollback()
        self.ctx.pop()

    def _make_run(self, status):
        run = PayrollRun(
            client_company_id=self.client_co.id, month="May", year=2026,
            status=status,
        )
        db.session.add(run)
        db.session.flush()
        return run

    def test_rejected_is_a_deletable_status(self):
        self.assertIn(REJECTED, DELETABLE_STATUSES)
        self.assertIn(DRAFT, DELETABLE_STATUSES)

    def test_rejected_run_has_no_delete_blockers(self):
        run = self._make_run(REJECTED)
        self.assertEqual(payroll_run_delete_blockers(run), [])

    def test_approved_run_still_blocked(self):
        run = self._make_run(APPROVED)
        blockers = payroll_run_delete_blockers(run)
        self.assertTrue(blockers)
        self.assertIn("Approved", blockers[0])

    def test_replace_existing_removes_a_rejected_run(self):
        run = self._make_run(REJECTED)
        run_id = run.id
        ok, reason = replace_existing_runs(self.client_co.id, "May", 2026)
        self.assertTrue(ok, reason)
        db.session.commit()
        self.assertIsNone(db.session.get(PayrollRun, run_id))


class RejectedRunDeleteRouteTestCase(unittest.TestCase):
    """Item 2: the delete route erases a Rejected run for an admin."""

    def setUp(self):
        self.app = create_app()
        self.app.config["TESTING"] = True
        self.http = self.app.test_client()
        self.http.post(
            "/login", data={"email": "admin@chrisnat.local", "password": "password123"}
        )
        with self.app.app_context():
            client_co = ClientCompany.query.first()
            run = PayrollRun(
                client_company_id=client_co.id, month="April", year=2026,
                status=REJECTED,
            )
            db.session.add(run)
            db.session.commit()
            self.run_id = run.id

    def test_delete_route_removes_rejected_run(self):
        resp = self.http.post(
            f"/payroll/runs/{self.run_id}/delete", follow_redirects=True
        )
        self.assertEqual(resp.status_code, 200)
        with self.app.app_context():
            self.assertIsNone(db.session.get(PayrollRun, self.run_id))


class DashboardSsnitTotalTestCase(unittest.TestCase):
    """Item 3: dashboard SSNIT figure = employee 5.5% + employer 13%."""

    def setUp(self):
        self.app = create_app()
        self.app.config["TESTING"] = True
        self.http = self.app.test_client()
        self.http.post(
            "/login", data={"email": "admin@chrisnat.local", "password": "password123"}
        )
        with self.app.app_context():
            client_co = ClientCompany.query.first()
            # Two runs in an off-current month so the total is fully controlled.
            db.session.add_all([
                PayrollRun(
                    client_company_id=client_co.id, month="January", year=2020,
                    status=APPROVED, total_ssnit=150.56, total_ssnit_employer=355.86,
                ),
                PayrollRun(
                    client_company_id=client_co.id, month="January", year=2020,
                    status=APPROVED, total_ssnit=100.00, total_ssnit_employer=200.00,
                ),
            ])
            db.session.commit()
        # employee 250.56 + employer 555.86 = 806.42 combined
        self.expected_combined = format_ghana_cedis(150.56 + 355.86 + 100.00 + 200.00)
        self.employee_only = format_ghana_cedis(150.56 + 100.00)

    def test_dashboard_shows_combined_ssnit(self):
        resp = self.http.get("/dashboard?month=January&year=2020")
        self.assertEqual(resp.status_code, 200)
        body = resp.data.decode("utf-8")
        self.assertIn(self.expected_combined, body)
        # The old (employee-only) figure must NOT be what's shown.
        self.assertNotIn(self.employee_only, body)
        self.assertIn("SSNIT Payable", body)


class EmployeeDeleteTestCase(unittest.TestCase):
    """Item 4: employees delete when history-free, refuse otherwise, and audit."""

    def setUp(self):
        self.app = create_app()
        self.app.config["TESTING"] = True
        self.http = self.app.test_client()
        self.http.post(
            "/login", data={"email": "admin@chrisnat.local", "password": "password123"}
        )
        with self.app.app_context():
            self.client_id = ClientCompany.query.first().id

    def _add_employee(self, staff_id, with_history=False):
        with self.app.app_context():
            emp = Employee(
                client_company_id=self.client_id, staff_id=staff_id,
                full_name=f"Worker {staff_id}", status="Active",
            )
            db.session.add(emp)
            db.session.flush()
            if with_history:
                run = PayrollRun(
                    client_company_id=self.client_id, month="March", year=2026,
                    status=APPROVED,
                )
                db.session.add(run)
                db.session.flush()
                db.session.add(
                    PayrollItem(
                        payroll_run_id=run.id, employee_id=emp.id,
                        staff_id=staff_id, full_name=emp.full_name,
                    )
                )
            db.session.commit()
            return emp.id

    def test_blocker_helper_flags_payroll_history(self):
        emp_id = self._add_employee("HIST1", with_history=True)
        with self.app.app_context():
            emp = db.session.get(Employee, emp_id)
            blockers = employee_delete_blockers(emp)
            self.assertTrue(blockers)
            self.assertIn("payroll record", blockers[0])

    def test_delete_succeeds_without_history_and_audits(self):
        emp_id = self._add_employee("CLEAN1", with_history=False)
        resp = self.http.post(
            f"/employees/clients/{self.client_id}/delete/{emp_id}",
            follow_redirects=True,
        )
        self.assertEqual(resp.status_code, 200)
        with self.app.app_context():
            self.assertIsNone(db.session.get(Employee, emp_id))
            audit = AuditTrail.query.filter_by(
                action="Employee hard-deleted",
                related_record_type="Employee",
                related_record_id=emp_id,
            ).first()
            self.assertIsNotNone(audit)
            self.assertIn("CLEAN1", audit.notes)

    def test_delete_refused_with_history(self):
        emp_id = self._add_employee("HIST2", with_history=True)
        resp = self.http.post(
            f"/employees/clients/{self.client_id}/delete/{emp_id}",
            follow_redirects=True,
        )
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"Cannot delete", resp.data)
        with self.app.app_context():
            # Still present — refusal, not deletion.
            self.assertIsNotNone(db.session.get(Employee, emp_id))

    def test_delete_forbidden_for_non_admin_md(self):
        emp_id = self._add_employee("CLEAN2", with_history=False)
        officer = self.app.test_client()
        officer.post(
            "/login",
            data={"email": "payroll@chrisnat.local", "password": "password123"},
        )
        officer.post(
            f"/employees/clients/{self.client_id}/delete/{emp_id}",
            follow_redirects=True,
        )
        with self.app.app_context():
            # payroll_officer is not admin/md — the row must survive.
            self.assertIsNotNone(db.session.get(Employee, emp_id))


if __name__ == "__main__":
    unittest.main()
