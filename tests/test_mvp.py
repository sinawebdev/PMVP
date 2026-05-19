import os
import tempfile
import unittest
from io import BytesIO

os.environ["DATABASE_URL"] = "sqlite:///:memory:"

from openpyxl import Workbook

from app import create_app, format_ghana_cedis
from app.pdf_service import generate_payslip_pdf
from app.excel_utils import calculate_worker_stats, map_columns
from app import db
from app.models import ClientCompany, Employee, PayrollItem, PayrollRun, User


class MvpTestCase(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.app = create_app()
        self.app.config["TESTING"] = True
        self.app.config["EXPORT_FOLDER"] = self.temp_dir.name
        self.client = self.app.test_client()

    def tearDown(self):
        self.temp_dir.cleanup()

    def login_admin(self):
        return self.client.post(
            "/login",
            data={"email": "admin@chrisnat.local", "password": "password123"},
            follow_redirects=True,
        )

    def login_md(self):
        return self.client.post(
            "/login",
            data={"email": "md@chrisnat.local", "password": "password123"},
            follow_redirects=True,
        )

    def build_import_workbook(self):
        workbook = Workbook()
        sheet = workbook.active
        sheet.append(
            [
                "staff id",
                "full name",
                "ssnit no",
                "basic salary",
                "transport allowance",
                "housing allowance",
                "overtime pay",
                "gross pay",
                "paye",
                "ssnit",
                "other deductions",
                "net pay",
            ]
        )
        sheet.append(["NEW-101", "New Import Worker", "SSNIT-NEW-101", 2100, 100, 100, 0, 2300, 100, 80, 0, 2120])
        stream = BytesIO()
        workbook.save(stream)
        stream.seek(0)
        return stream

    def test_seeded_users_and_clients_exist(self):
        with self.app.app_context():
            self.assertIsNotNone(User.query.filter_by(email="admin@chrisnat.local").first())
            self.assertIsNotNone(ClientCompany.query.filter_by(name="MSC Ghana Ltd").first())

    def test_column_mapping_handles_common_payroll_headers(self):
        mapping = map_columns(["Staff No", "Employee Name", "Basic Salary", "Take Home"])

        self.assertEqual(mapping["Staff No"], "staff_id")
        self.assertEqual(mapping["Employee Name"], "full_name")
        self.assertEqual(mapping["Basic Salary"], "basic_salary")
        self.assertEqual(mapping["Take Home"], "net_pay")

    def test_money_is_formatted_as_comma_separated_ghana_cedis(self):
        self.assertEqual(format_ghana_cedis(1234567.5), "GH₵ 1,234,567.50")

    def test_worker_stats_use_unique_worker_identity(self):
        rows = [
            {"staff_id": "CN-001", "full_name": "Kwame Mensah"},
            {"staff_id": "CN-001", "full_name": "Kwame Mensah"},
            {"staff_id": "", "full_name": "Ama Serwaa"},
            {"staff_id": "", "full_name": "Ama Serwaa"},
            {"staff_id": "", "full_name": ""},
        ]

        stats = calculate_worker_stats(rows)

        self.assertEqual(stats["total_rows"], 4)
        self.assertEqual(stats["total_unique_workers"], 2)
        self.assertEqual(stats["duplicate_count"], 2)

    def test_main_pages_render_for_admin(self):
        response = self.login_admin()
        self.assertEqual(response.status_code, 200)

        for path in [
            "/dashboard",
            "/clients",
            "/employees",
            "/payroll/runs",
            "/accounts/",
            "/accounts/vouchers",
            "/accounts/remittances",
            "/proposals",
        ]:
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 200)

    def test_health_endpoint_is_available_for_render(self):
        response = self.client.get("/health")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["status"], "ok")

    def test_approval_creates_voucher_and_remittances(self):
        self.login_admin()
        with self.app.app_context():
            payroll_run = PayrollRun.query.first()
            run_id = payroll_run.id

        response = self.client.post(f"/payroll/runs/{run_id}/approve", follow_redirects=True)

        self.assertEqual(response.status_code, 200)
        with self.app.app_context():
            payroll_run = db.session.get(PayrollRun, run_id)
            self.assertEqual(payroll_run.status, "Approved")
            self.assertIsNotNone(payroll_run.voucher)
            self.assertEqual({item.remittance_type for item in payroll_run.remittances}, {"PAYE", "SSNIT"})

    def test_md_has_admin_clearance_for_admin_pages(self):
        response = self.login_md()
        self.assertEqual(response.status_code, 200)

        self.assertEqual(self.client.get("/clients/add").status_code, 200)
        self.assertEqual(self.client.get("/employees/add").status_code, 200)
        self.assertEqual(self.client.get("/payroll/upload").status_code, 200)

    def test_md_can_record_expenses(self):
        self.login_md()
        response = self.client.post(
            "/accounts/expenses",
            data={
                "expense_date": "2026-05-19",
                "category": "Transport",
                "description": "MD approved client visit transport",
                "amount": "125.50",
                "payment_method": "Mobile Money",
                "receipt_reference": "MD-EXP-001",
            },
            follow_redirects=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Expense recorded.", response.data)

    def test_payslip_pdf_service_creates_pdf_file(self):
        with self.app.app_context():
            item = PayrollItem.query.first()
            file_path = generate_payslip_pdf(item, self.app.config["EXPORT_FOLDER"])

        self.assertTrue(os.path.exists(file_path))
        with open(file_path, "rb") as file:
            self.assertEqual(file.read(4), b"%PDF")

    def test_authenticated_user_can_download_individual_payslip(self):
        self.login_admin()
        with self.app.app_context():
            item = PayrollItem.query.first()
            item_id = item.id

        response = self.client.get(f"/payroll/items/{item_id}/payslip")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["Content-Type"], "application/pdf")
        self.assertTrue(response.data.startswith(b"%PDF"))
        response.close()

    def test_payroll_detail_uses_excel_grid_and_cedis_format(self):
        self.login_admin()
        with self.app.app_context():
            payroll_run = PayrollRun.query.first()
            run_id = payroll_run.id

        response = self.client.get(f"/payroll/runs/{run_id}")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"excel-grid", response.data)
        self.assertIn("GH₵ 8,044.50".encode("utf-8"), response.data)

    def test_inactive_client_status_is_red(self):
        self.login_admin()
        with self.app.app_context():
            client_company = ClientCompany.query.filter_by(name="MSC Ghana Ltd").first()
            client_company.status = "Inactive"
            db.session.commit()

        response = self.client.get("/clients")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"text-bg-danger", response.data)

    def test_confirmed_import_creates_employee_records(self):
        self.login_admin()
        with self.app.app_context():
            client_company = ClientCompany.query.filter_by(name="MSC Ghana Ltd").first()
            client_id = client_company.id

        upload_response = self.client.post(
            "/payroll/upload",
            data={
                "client_company_id": str(client_id),
                "month": "January",
                "year": "2101",
                "payroll_file": (self.build_import_workbook(), "new_workers.xlsx"),
            },
            content_type="multipart/form-data",
            follow_redirects=False,
        )
        import_id = upload_response.headers["Location"].rstrip("/").split("/")[-1]
        response = self.client.post(f"/payroll/confirm/{import_id}", follow_redirects=True)

        self.assertEqual(response.status_code, 200)
        with self.app.app_context():
            employee = Employee.query.filter_by(staff_id="NEW-101", client_company_id=client_id).first()
            self.assertIsNotNone(employee)
            self.assertEqual(employee.full_name, "New Import Worker")
            self.assertEqual(employee.ssnit_number, "SSNIT-NEW-101")

    def test_payroll_runs_filter_by_client_tab(self):
        self.login_admin()
        with self.app.app_context():
            msc = ClientCompany.query.filter_by(name="MSC Ghana Ltd").first()
            stellar = ClientCompany.query.filter_by(name="Stellar Logistics").first()
            stellar_run = PayrollRun(
                month="January",
                year=2101,
                status="Draft",
                created_by=User.query.filter_by(email="admin@chrisnat.local").first().id,
                client_company_id=stellar.id,
                total_workers=1,
                source_filename="stellar.xlsx",
                total_net_pay=500,
            )
            db.session.add(stellar_run)
            db.session.commit()
            msc_id = msc.id

        response = self.client.get(f"/payroll/runs?client_id={msc_id}")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"MSC Ghana Ltd", response.data)
        self.assertNotIn(b"stellar.xlsx", response.data)

    def test_admin_can_create_proposal_draft_for_client(self):
        self.login_admin()
        with self.app.app_context():
            client_company = ClientCompany.query.filter_by(name="MSC Ghana Ltd").first()
            client_id = client_company.id

        response = self.client.post(
            "/proposals",
            data={
                "client_company_id": str(client_id),
                "title": "Payroll Outsourcing Proposal",
                "service_summary": "Monthly payroll processing and statutory reporting.",
                "proposed_amount": "2500",
                "status": "Draft",
            },
            follow_redirects=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Payroll Outsourcing Proposal", response.data)
        from app.models import Proposal

        with self.app.app_context():
            self.assertIsNotNone(Proposal.query.filter_by(title="Payroll Outsourcing Proposal").first())


if __name__ == "__main__":
    unittest.main()
