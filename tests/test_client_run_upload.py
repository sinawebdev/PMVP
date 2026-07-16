"""Deferred Phase 3 item — client self-service run upload.

A client uploads a standard payroll workbook for their OWN company; it reuses the
operator import pipeline (client_company_id forced to the tenant) and lands in the
Phase 5 risk gate. Raw-hours workbooks and non-Excel files are refused, a platform
user cannot reach the page, and the created run belongs to the uploading tenant.
"""

import os
import unittest
from io import BytesIO

os.environ["SKIP_DOTENV"] = "true"
os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["SEED_DEMO_DATA"] = "true"
os.environ["PERSISTENCE_REQUIRED"] = "false"

from openpyxl import Workbook  # noqa: E402

from app import create_app, db  # noqa: E402
from app.models import DomainEvent, PayrollRun, User  # noqa: E402
from app.payroll_status import AUTO_ACCEPTED, HELD  # noqa: E402

# A period deliberately distinct from the seeded MSC run (which uses "now"),
# so the duplicate guard doesn't reject the upload.
UPLOAD_MONTH = "March"
UPLOAD_YEAR = 2024


def _payroll_workbook():
    workbook = Workbook()
    sheet = workbook.active
    sheet.append([
        "staff id", "full name", "ssnit no", "basic salary", "transport allowance",
        "housing allowance", "overtime pay", "gross pay", "paye", "ssnit",
        "other deductions", "net pay",
    ])
    sheet.append(["UP-1", "Upload Worker One", "SSN-UP-1", 2100, 100, 100, 0, 2300, 100, 80, 0, 2120])
    sheet.append(["UP-2", "Upload Worker Two", "SSN-UP-2", 1800, 80, 80, 0, 1960, 90, 70, 0, 1800])
    stream = BytesIO()
    workbook.save(stream)
    stream.seek(0)
    return stream


class ClientRunUploadTestCase(unittest.TestCase):
    def setUp(self):
        self.app = create_app()
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()
        self.ctx = self.app.app_context()
        self.ctx.push()
        self.msc = User.query.filter_by(email="admin@msc.demo").first()
        self.tenant_id = self.msc.client_company_id

    def tearDown(self):
        self.ctx.pop()

    def _login(self, email):
        self.assertEqual(
            self.client.post("/login", data={"email": email, "password": "password123"}).status_code,
            302,
        )

    def _upload(self, **overrides):
        data = {
            "month": overrides.get("month", UPLOAD_MONTH),
            "year": str(overrides.get("year", UPLOAD_YEAR)),
            "payroll_file": (overrides.get("stream", _payroll_workbook()), overrides.get("filename", "payroll.xlsx")),
        }
        return self.client.post(
            "/company/runs/upload", data=data, content_type="multipart/form-data"
        )

    def _new_run(self):
        return PayrollRun.query.filter_by(
            client_company_id=self.tenant_id, month=UPLOAD_MONTH, year=UPLOAD_YEAR
        ).first()

    def test_upload_creates_tenant_run_through_risk_gate(self):
        self._login("admin@msc.demo")
        resp = self._upload()
        self.assertEqual(resp.status_code, 302)
        run = self._new_run()
        self.assertIsNotNone(run)
        self.assertEqual(run.client_company_id, self.tenant_id)  # forced to tenant
        self.assertGreater(len(run.items), 0)
        # It went through the risk gate — MSC already has a seeded run, so this
        # (its 2nd) is still inside the new-client window and is Held.
        self.assertEqual(run.status, HELD)
        self.assertEqual(run.risk_status, "held")
        # Chrisnat oversight was notified (tenant -> platform).
        event = DomainEvent.query.filter_by(
            event_type="run.risk_held", subject_id=run.id
        ).first()
        self.assertIsNotNone(event)
        self.assertEqual(event.client_company_id, self.tenant_id)

    def test_non_excel_is_rejected(self):
        self._login("admin@msc.demo")
        resp = self._upload(stream=BytesIO(b"not a workbook"), filename="notes.txt")
        self.assertEqual(resp.status_code, 302)
        self.assertIsNone(self._new_run())

    def test_platform_user_cannot_upload(self):
        self._login("chrisnat.admin@chrisnat.local")
        # tenant_role_required bounces a platform user to the oversight dashboard.
        self.assertEqual(self.client.get("/company/runs/upload").status_code, 302)
        self.assertEqual(self._upload().status_code, 302)
        self.assertIsNone(self._new_run())


if __name__ == "__main__":
    unittest.main()
