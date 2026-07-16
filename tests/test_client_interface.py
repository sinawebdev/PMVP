"""Phase 3 — client (tenant) interface.

Covers the self-service client plane: a tenant user reaches every /company/*
page and can add/edit their own employees; a platform user is bounced to the
oversight console; and cross-tenant object access (another tenant's employee /
run / payslip item) is 404 through the client routes.
"""

import os
import unittest

os.environ["SKIP_DOTENV"] = "true"
os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["SEED_DEMO_DATA"] = "true"
os.environ["PERSISTENCE_REQUIRED"] = "false"

from app import create_app  # noqa: E402
from app.models import Employee, PayrollItem, PayrollRun, User  # noqa: E402

CLIENT_PAGES = ["/company", "/company/employees", "/company/employees/add",
                "/company/runs", "/company/statutory", "/company/expenses", "/company/audit"]


class ClientInterfaceTestCase(unittest.TestCase):
    def setUp(self):
        self.app = create_app()
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()
        self.ctx = self.app.app_context()
        self.ctx.push()
        self.msc = User.query.filter_by(email="admin@msc.demo").first()
        self.msc_run = PayrollRun.query.filter_by(client_company_id=self.msc.client_company_id).first()
        self.msc_item = PayrollItem.query.filter_by(payroll_run_id=self.msc_run.id).first()
        self.msc_emp = Employee.query.filter_by(client_company_id=self.msc.client_company_id).first()

    def tearDown(self):
        self.ctx.pop()

    def _login(self, email):
        self.assertEqual(self.client.post("/login", data={"email": email, "password": "password123"}).status_code, 302)

    def test_tenant_user_reaches_all_client_pages(self):
        self._login("admin@msc.demo")
        for page in CLIENT_PAGES:
            self.assertEqual(self.client.get(page).status_code, 200, page)
        # own run detail + payslip PDF
        self.assertEqual(self.client.get(f"/company/runs/{self.msc_run.id}").status_code, 200)
        self.assertEqual(self.client.get(f"/company/items/{self.msc_item.id}/payslip").status_code, 200)

    def test_platform_user_bounced_from_client_plane(self):
        self._login("chrisnat.admin@chrisnat.local")
        for page in ["/company/employees", "/company/runs", "/company/statutory"]:
            resp = self.client.get(page)
            self.assertEqual(resp.status_code, 302, page)
            self.assertTrue(resp.headers["Location"].endswith("/dashboard"))

    def test_employee_self_service_add_is_tenant_bound(self):
        self._login("admin@msc.demo")
        resp = self.client.post(
            "/company/employees/add",
            data={"staff_id": "msc new 7", "full_name": "New Worker", "basic_salary": "1500"},
        )
        self.assertEqual(resp.status_code, 302)
        emp = Employee.query.filter_by(staff_id="MSCNEW7").first()
        self.assertIsNotNone(emp)
        self.assertEqual(emp.client_company_id, self.msc.client_company_id)  # forced to tenant
        self.assertEqual(emp.full_name, "New Worker")

    def test_cross_tenant_objects_404_through_client_routes(self):
        self._login("admin@stellar.demo")  # different tenant
        for path in [
            f"/company/employees/{self.msc_emp.id}/edit",
            f"/company/runs/{self.msc_run.id}",
            f"/company/items/{self.msc_item.id}/payslip",
        ]:
            self.assertEqual(self.client.get(path).status_code, 404, path)

    def test_audit_trail_is_tenant_scoped(self):
        # MSC user's action is auditable and shows in MSC's audit; a different
        # tenant never sees it.
        self._login("admin@msc.demo")
        self.client.post(
            "/company/employees/add",
            data={"staff_id": "MSCAUD1", "full_name": "Audit Marker Worker"},
        )
        html = self.client.get("/company/audit").get_data(as_text=True)
        self.assertEqual(self.client.get("/company/audit").status_code, 200)
        self.assertIn("Audit Marker Worker", html)
        # Stellar user must not see MSC's audit entry.
        self.client.get("/logout")
        self._login("admin@stellar.demo")
        stellar_html = self.client.get("/company/audit").get_data(as_text=True)
        self.assertNotIn("Audit Marker Worker", stellar_html)

    def test_employee_list_is_scoped(self):
        self._login("admin@msc.demo")
        # Every employee shown belongs to MSC (checked via DB — the list query is tenant_query).
        html = self.client.get("/company/employees").get_data(as_text=True)
        stellar_emp = Employee.query.filter(
            Employee.client_company_id != self.msc.client_company_id
        ).first()
        self.assertNotIn(stellar_emp.staff_id, html)


if __name__ == "__main__":
    unittest.main()
