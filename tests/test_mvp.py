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

    def build_offset_phase2_workbook(self):
        workbook = Workbook()
        sheet = workbook.active
        sheet.append(["Chrisnat Limited", "Payroll schedule"])
        sheet.append(["Client", "MSC Ghana Ltd"])
        sheet.append([])
        sheet.append(
            [
                "Employee No",
                "Employee Name",
                "Bank",
                "Account Number",
                "Basic Salary",
                "Allowances",
                "Gross Salary",
                "PAYE Tax",
                "SSNIT Contribution",
                "Deductions",
                "Net Salary",
            ]
        )
        sheet.append(["P2-001", "Akua Boateng", "GCB Bank", "0012345678", "GHC 1,000.00", "200", "", "50", "30", "20", "1,100.00"])
        sheet.append(["P2-001", "Akua Boateng", "GCB Bank", "0012345678", "GHC 1,000.00", "200", "", "50", "30", "20", "1,100.00"])
        sheet.append(["", "", "", "", "", "", "", "", "", "", ""])
        sheet.append(["TOTAL", "", "", "", "", "", "", "", "", "", ""])
        stream = BytesIO()
        workbook.save(stream)
        stream.seek(0)
        return stream

    def build_multisheet_stress_workbook(self):
        workbook = Workbook()
        guide = workbook.active
        guide.title = "Stress_Test_Guide"
        guide.append(["This workbook starts with a guide sheet and should not be imported."])
        guide.append(["The importer must inspect the payroll sheets that follow."])

        sheet = workbook.create_sheet("MSC_Ghana_Ltd")
        sheet.append(["Chrisnat Limited", "Client payroll schedule"])
        sheet.append(["Client Company", "MSC Ghana Ltd"])
        sheet.append(["Prepared for stress testing"])
        sheet.append(
            [
                "Row ID",
                "Staff ID",
                "Employee Name",
                "Status",
                "Service Line",
                "Job Role",
                "Payroll Month",
                "Basic Salary",
                "Transport Allowance",
                "Housing Allowance",
                "Overtime Pay",
                "Other Allowances",
                "Gross Pay",
                "PAYE",
                "SSNIT Employee",
                "Tier 2 Pension",
                "Loan Deduction",
                "Other Deduction",
                "Total Deductions",
                "Net Pay",
                "Bank Name",
                "Bank Account",
                "MoMo Number",
                "Ghana Card No",
                "SSNIT Number",
            ]
        )
        sheet.append(
            [
                1,
                "MSC-001",
                "Abena Owusu",
                "Active",
                "Port Services",
                "Clerk",
                "May 2026",
                "GHC 1,200.00",
                "150",
                "100",
                "50",
                "25",
                "1,525.00",
                "120",
                "80",
                "40",
                "0",
                "10",
                "250",
                "1,275.00",
                "GCB Bank",
                "0012345678",
                "0244000001",
                "GHA-111",
                "SSNIT-111",
            ]
        )
        sheet.append(
            [
                2,
                "MSC-001",
                "Abena Owusu",
                "Inactive",
                "Port Services",
                "Clerk",
                "May 2026",
                "GHC 1,200.00",
                "150",
                "100",
                "50",
                "25",
                "1,525.00",
                "120",
                "80",
                "40",
                "0",
                "10",
                "250",
                "1,275.00",
                "GCB Bank",
                "0012345678",
                "0244000001",
                "GHA-111",
                "SSNIT-111",
            ]
        )
        sheet.append(
            [
                3,
                "",
                "Kojo Mensah",
                "Terminated",
                "Terminal",
                "Operator",
                "May 2026",
                "900",
                "100",
                "",
                "",
                "",
                "",
                "0",
                "0",
                "",
                "",
                "",
                "0",
                "",
                "",
                "",
                "",
                "",
                "",
            ]
        )
        sheet.append(["TOTAL", "", "", "", "", "", "", "", "", "", "", "", "3,950.00", "", "", "", "", "", "", ""])

        stellar = workbook.create_sheet("Stellar_Logistics")
        stellar.append(["Metadata"])
        stellar.append([])
        stellar.append(["Staff ID", "Employee Name", "Gross Pay", "Net Pay"])
        stellar.append(["STL-001", "Afia Darko", 1000, 900])

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

    def test_phase2_import_detects_offset_headers_and_cleans_payroll_values(self):
        from app.excel_utils import mapped_rows_from_dataframe, read_excel_file

        workbook = self.build_offset_phase2_workbook()
        file_path = os.path.join(self.temp_dir.name, "offset_payroll.xlsx")
        with open(file_path, "wb") as file:
            file.write(workbook.read())

        df, mapping = read_excel_file(file_path)
        rows = mapped_rows_from_dataframe(df, mapping)

        self.assertEqual(mapping["Employee No"], "staff_id")
        self.assertEqual(mapping["Employee Name"], "full_name")
        self.assertEqual(mapping["Gross Salary"], "gross_pay")
        self.assertEqual(mapping["Net Salary"], "net_pay")
        self.assertEqual(mapping["Account Number"], "bank_account_number")
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["staff_id"], "P2-001")
        self.assertEqual(rows[0]["bank_account_number"], "0012345678")
        self.assertEqual(rows[0]["gross_pay"], 1200.0)
        self.assertEqual(rows[0]["net_pay"], 1100.0)

    def test_import_handles_duplicate_excel_headers_without_crashing(self):
        import pandas as pd
        from app.excel_utils import mapped_rows_from_dataframe

        df = pd.DataFrame(
            [["CN-900", "Duplicate Header Worker", 1200, 95, 1105]],
            columns=["Staff ID", "Employee Name", "Gross Pay", "Gross Pay", "Net Pay"],
        )
        mapping = {
            "Staff ID": "staff_id",
            "Employee Name": "full_name",
            "Gross Pay": "gross_pay",
            "Net Pay": "net_pay",
        }

        rows = mapped_rows_from_dataframe(df, mapping)

        self.assertEqual(rows[0]["gross_pay"], 1200.0)
        self.assertEqual(rows[0]["net_pay"], 1105.0)

    def test_multisheet_import_matches_selected_client_sheet_and_offset_header(self):
        from app.excel_utils import extract_payroll_sheet, match_client_sheet, payroll_sheet_candidates

        workbook = self.build_multisheet_stress_workbook()
        file_path = os.path.join(self.temp_dir.name, "stress_test.xlsx")
        with open(file_path, "wb") as file:
            file.write(workbook.read())

        candidates = payroll_sheet_candidates(file_path)
        matched_sheet = match_client_sheet("MSC Ghana Ltd", [candidate["sheet_name"] for candidate in candidates])
        extraction = extract_payroll_sheet(file_path, matched_sheet)

        self.assertEqual(matched_sheet, "MSC_Ghana_Ltd")
        self.assertEqual(extraction["detected_header_row"], 4)
        self.assertEqual(extraction["worker_stats"]["total_rows"], 3)
        self.assertEqual(extraction["worker_stats"]["total_unique_workers"], 2)
        self.assertEqual(extraction["worker_stats"]["duplicate_count"], 1)
        self.assertEqual(extraction["status_breakdown"]["active"], 1)
        self.assertEqual(extraction["status_breakdown"]["inactive"], 1)
        self.assertEqual(extraction["status_breakdown"]["terminated"], 1)
        self.assertEqual(extraction["totals"]["gross_total"], 4050.0)
        self.assertEqual(extraction["totals"]["paye_total"], 240.0)
        self.assertEqual(extraction["totals"]["ssnit_total"], 160.0)
        self.assertEqual(extraction["mapping"]["SSNIT Employee"], "ssnit")
        self.assertEqual(extraction["mapping"]["Bank Account"], "bank_account_number")

    def test_money_is_formatted_as_comma_separated_ghana_cedis(self):
        self.assertEqual(format_ghana_cedis(1234567.5), "GH₵ 1,234,567.50")

    def test_dashboard_requires_authentication(self):
        response = self.client.get("/dashboard")

        self.assertEqual(response.status_code, 302)
        self.assertIn("/login", response.headers["Location"])

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

    def test_phase2_upload_creates_import_batch_preview_record(self):
        self.login_admin()
        from app.models import ImportBatch

        with self.app.app_context():
            client_company = ClientCompany.query.filter_by(name="MSC Ghana Ltd").first()
            client_id = client_company.id

        response = self.client.post(
            "/payroll/upload",
            data={
                "client_company_id": str(client_id),
                "month": "February",
                "year": "2101",
                "payroll_file": (self.build_offset_phase2_workbook(), "phase2.xlsx"),
            },
            content_type="multipart/form-data",
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 302)
        with self.app.app_context():
            batch = ImportBatch.query.filter_by(original_filename="phase2.xlsx").first()
            self.assertIsNotNone(batch)
            self.assertEqual(batch.status, "Previewed")
            self.assertEqual(batch.total_rows, 2)
            self.assertEqual(batch.total_workers, 1)
            self.assertEqual(batch.gross_total, 2400.0)

    def test_upload_uses_matching_client_sheet_in_multisheet_workbook(self):
        self.login_admin()
        from app.models import ImportBatch

        with self.app.app_context():
            client_company = ClientCompany.query.filter_by(name="MSC Ghana Ltd").first()
            client_id = client_company.id

        response = self.client.post(
            "/payroll/upload",
            data={
                "client_company_id": str(client_id),
                "month": "May",
                "year": "2101",
                "payroll_file": (self.build_multisheet_stress_workbook(), "stress_test.xlsx"),
            },
            content_type="multipart/form-data",
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 302)
        with self.app.app_context():
            batch = ImportBatch.query.filter_by(original_filename="stress_test.xlsx").first()
            self.assertIsNotNone(batch)
            self.assertEqual(batch.total_rows, 3)
            self.assertEqual(batch.total_workers, 2)
            self.assertEqual(batch.gross_total, 4050.0)

        preview_response = self.client.get(response.headers["Location"])
        self.assertEqual(preview_response.status_code, 200)
        self.assertIn(b"MSC_Ghana_Ltd", preview_response.data)
        self.assertIn(b"Detected Header Row", preview_response.data)

    def test_upload_rejects_workbook_with_no_valid_payroll_rows(self):
        self.login_admin()
        from app.models import ImportBatch

        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "Stress_Test_Guide"
        sheet.append(["Guide only"])
        stream = BytesIO()
        workbook.save(stream)
        stream.seek(0)

        with self.app.app_context():
            client_company = ClientCompany.query.filter_by(name="MSC Ghana Ltd").first()
            client_id = client_company.id
            original_batches = ImportBatch.query.count()

        response = self.client.post(
            "/payroll/upload",
            data={
                "client_company_id": str(client_id),
                "month": "June",
                "year": "2101",
                "payroll_file": (stream, "empty_stress.xlsx"),
            },
            content_type="multipart/form-data",
            follow_redirects=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"No valid payroll rows were found", response.data)
        with self.app.app_context():
            self.assertEqual(ImportBatch.query.count(), original_batches)

    def test_duplicate_payroll_requires_replacement_confirmation(self):
        self.login_admin()
        with self.app.app_context():
            client_company = ClientCompany.query.filter_by(name="MSC Ghana Ltd").first()
            client_id = client_company.id
            original_count = PayrollRun.query.filter_by(
                client_company_id=client_id,
                month="May",
                year=2026,
            ).count()

        upload_response = self.client.post(
            "/payroll/upload",
            data={
                "client_company_id": str(client_id),
                "month": "May",
                "year": "2026",
                "payroll_file": (self.build_import_workbook(), "duplicate.xlsx"),
            },
            content_type="multipart/form-data",
            follow_redirects=False,
        )
        import_id = upload_response.headers["Location"].rstrip("/").split("/")[-1]
        response = self.client.post(f"/payroll/confirm/{import_id}", follow_redirects=True)

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"confirm replacement", response.data.lower())
        with self.app.app_context():
            new_count = PayrollRun.query.filter_by(
                client_company_id=client_id,
                month="May",
                year=2026,
            ).count()
            self.assertEqual(new_count, original_count)

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

    def test_accounts_dashboard_is_finance_control_center(self):
        self.login_admin()
        with self.app.app_context():
            admin = User.query.filter_by(email="admin@chrisnat.local").first()
            client_company = ClientCompany.query.filter_by(name="MSC Ghana Ltd").first()
            missing_voucher_run = PayrollRun(
                month="May",
                year=2026,
                status="Approved",
                created_by=admin.id,
                client_company_id=client_company.id,
                total_workers=2,
                total_gross_pay=3000,
                total_deductions=500,
                total_net_pay=2500,
                total_paye=300,
                total_ssnit=200,
            )
            db.session.add(missing_voucher_run)
            db.session.commit()

        response = self.client.get("/accounts/")

        self.assertEqual(response.status_code, 200)
        for text in [
            b"Approved Payrolls Awaiting Payment",
            b"Total Net Pay This Month",
            b"PAYE Due",
            b"SSNIT Due",
            b"Expenses This Month",
            b"Overdue Remittances",
            b"Action Required",
            b"Approved payrolls without payment vouchers",
            b"Client Payroll Cost Breakdown",
            b"Recent Payment Vouchers",
            b"Recent Expenses",
            b"Recorded By",
            b"Remittances",
        ]:
            self.assertIn(text, response.data)
        self.assertIn(b"compact-topbar", response.data)
        self.assertNotIn(b"Admin User | Admin", response.data)

    def test_dashboard_has_month_filter_sparkbars_and_action_queue(self):
        self.login_admin()
        response = self.client.get("/dashboard?month=May&year=2026")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"dashboard-controls", response.data)
        self.assertIn(b"sparkbar", response.data)
        self.assertIn(b"Approval Queue", response.data)
        self.assertIn(b"No run submitted", response.data)

    def test_health_endpoint_is_available_for_render(self):
        response = self.client.get("/health")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["status"], "ok")

    def test_payroll_approval_queue_filter_is_server_side(self):
        self.login_admin()
        response = self.client.get("/payroll/runs?status=needs_approval")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Approval queue", response.data)
        self.assertNotIn(b"Approved</span>", response.data)

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
        self.assertEqual(self.client.get("/reports").status_code, 200)
        self.assertEqual(self.client.get("/audit").status_code, 200)

    def test_phase2_payroll_workflow_creates_audit_trail_and_voucher_fields(self):
        self.login_admin()
        from app.models import AuditTrail

        with self.app.app_context():
            payroll_run = PayrollRun(
                month="March",
                year=2101,
                status="Draft",
                created_by=User.query.filter_by(email="admin@chrisnat.local").first().id,
                client_company_id=ClientCompany.query.filter_by(name="MSC Ghana Ltd").first().id,
                total_workers=1,
                total_gross_pay=1200,
                total_deductions=100,
                total_net_pay=1100,
                total_paye=50,
                total_ssnit=30,
            )
            db.session.add(payroll_run)
            db.session.commit()
            run_id = payroll_run.id

        self.assertEqual(
            self.client.post(f"/payroll/runs/{run_id}/submit-review", follow_redirects=True).status_code,
            200,
        )
        self.client.get("/logout")
        self.client.post(
            "/login",
            data={"email": "accounts@chrisnat.local", "password": "password123"},
            follow_redirects=True,
        )
        self.assertEqual(
            self.client.post(f"/payroll/runs/{run_id}/submit-md-approval", follow_redirects=True).status_code,
            200,
        )
        self.client.get("/logout")
        self.login_md()
        self.assertEqual(
            self.client.post(f"/payroll/runs/{run_id}/approve", follow_redirects=True).status_code,
            200,
        )

        with self.app.app_context():
            run = db.session.get(PayrollRun, run_id)
            self.assertEqual(run.status, "Approved")
            self.assertIsNotNone(run.voucher)
            self.assertEqual(run.voucher.status, "Pending Payment")
            self.assertEqual(run.voucher.gross_payroll, 1200)
            self.assertEqual(run.voucher.net_amount_payable, 1100)
            self.assertGreaterEqual(AuditTrail.query.filter_by(related_record_type="PayrollRun", related_record_id=run_id).count(), 3)

    def test_accounts_can_mark_approved_payroll_paid(self):
        self.login_admin()
        with self.app.app_context():
            payroll_run = PayrollRun.query.first()
            run_id = payroll_run.id
        self.client.post(f"/payroll/runs/{run_id}/approve", follow_redirects=True)
        self.client.get("/logout")
        self.client.post(
            "/login",
            data={"email": "accounts@chrisnat.local", "password": "password123"},
            follow_redirects=True,
        )

        response = self.client.post(f"/payroll/runs/{run_id}/mark-paid", follow_redirects=True)

        self.assertEqual(response.status_code, 200)
        with self.app.app_context():
            run = db.session.get(PayrollRun, run_id)
            self.assertEqual(run.status, "Paid")
            self.assertEqual(run.voucher.status, "Paid")

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

    def test_client_detail_shows_phase2_payroll_dashboard_metrics(self):
        self.login_admin()
        with self.app.app_context():
            client_company = ClientCompany.query.filter_by(name="MSC Ghana Ltd").first()
            client_id = client_company.id

        response = self.client.get(f"/clients/{client_id}")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Current Month Payroll Cost", response.data)
        self.assertIn(b"Previous Month Payroll Cost", response.data)
        self.assertIn(b"Validation Warnings", response.data)

    def test_reports_page_supports_excel_export(self):
        self.login_md()
        response = self.client.get("/reports")
        export_response = self.client.get("/reports/monthly-payroll.xlsx?month=May&year=2026")
        pdf_response = self.client.get("/reports/monthly-payroll.pdf?month=May&year=2026")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Monthly payroll summary", response.data)
        self.assertEqual(export_response.status_code, 200)
        self.assertIn(
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            export_response.headers["Content-Type"],
        )
        export_response.close()
        self.assertEqual(pdf_response.status_code, 200)
        self.assertEqual(pdf_response.headers["Content-Type"], "application/pdf")
        self.assertTrue(pdf_response.data.startswith(b"%PDF"))
        pdf_response.close()


if __name__ == "__main__":
    unittest.main()
