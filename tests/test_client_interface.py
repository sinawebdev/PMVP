"""Phase 3 — client (tenant) interface.

Covers the self-service client plane: a tenant user reaches every /company/*
page and can add/edit their own employees; a platform user is bounced to the
oversight console; and cross-tenant object access (another tenant's employee /
run / payslip item) is 404 through the client routes.
"""

import os
import re
import unittest
from io import BytesIO

os.environ["SKIP_DOTENV"] = "true"
os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["SEED_DEMO_DATA"] = "true"
os.environ["PERSISTENCE_REQUIRED"] = "false"

from openpyxl import Workbook  # noqa: E402

from app import create_app  # noqa: E402
from app.models import (  # noqa: E402
    Employee,
    Expense,
    ImportBatch,
    PayrollItem,
    PayrollRun,
    User,
)

CLIENT_PAGES = ["/company", "/company/employees", "/company/employees/add",
                "/company/runs", "/company/statutory", "/company/expenses", "/company/audit"]


class ClientInterfaceTestCase(unittest.TestCase):
    def setUp(self):
        self.app = create_app()
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()
        self.ctx = self.app.app_context()
        self.ctx.push()
        self.msc = User.query.filter_by(email="admin@msc.com").first()
        self.msc_run = PayrollRun.query.filter_by(client_company_id=self.msc.client_company_id).first()
        self.msc_item = PayrollItem.query.filter_by(payroll_run_id=self.msc_run.id).first()
        self.msc_emp = Employee.query.filter_by(client_company_id=self.msc.client_company_id).first()

    def tearDown(self):
        self.ctx.pop()

    def _login(self, email):
        self.assertEqual(self.client.post("/login", data={"email": email, "password": "password123"}).status_code, 302)

    def test_tenant_user_reaches_all_client_pages(self):
        self._login("admin@msc.com")
        for page in CLIENT_PAGES:
            self.assertEqual(self.client.get(page).status_code, 200, page)
        # own run detail + payslip PDF
        self.assertEqual(self.client.get(f"/company/runs/{self.msc_run.id}").status_code, 200)
        self.assertEqual(self.client.get(f"/company/items/{self.msc_item.id}/payslip").status_code, 200)

    def test_platform_user_bounced_from_client_plane(self):
        self._login("operator@payrolla.com")
        for page in ["/company/employees", "/company/runs", "/company/statutory"]:
            resp = self.client.get(page)
            self.assertEqual(resp.status_code, 302, page)
            self.assertTrue(resp.headers["Location"].endswith("/dashboard"))

    def test_employee_self_service_add_is_tenant_bound(self):
        self._login("admin@msc.com")
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
        self._login("admin@acme.com")  # different tenant
        for path in [
            f"/company/employees/{self.msc_emp.id}/edit",
            f"/company/runs/{self.msc_run.id}",
            f"/company/items/{self.msc_item.id}/payslip",
        ]:
            self.assertEqual(self.client.get(path).status_code, 404, path)

    def test_audit_trail_is_tenant_scoped(self):
        # MSC user's action is auditable and shows in MSC's audit; a different
        # tenant never sees it.
        self._login("admin@msc.com")
        self.client.post(
            "/company/employees/add",
            data={"staff_id": "MSCAUD1", "full_name": "Audit Marker Worker"},
        )
        html = self.client.get("/company/audit").get_data(as_text=True)
        self.assertEqual(self.client.get("/company/audit").status_code, 200)
        self.assertIn("Audit Marker Worker", html)
        # Stellar user must not see MSC's audit entry.
        self.client.get("/logout")
        self._login("admin@acme.com")
        stellar_html = self.client.get("/company/audit").get_data(as_text=True)
        self.assertNotIn("Audit Marker Worker", stellar_html)

    def test_employee_list_is_scoped(self):
        self._login("admin@msc.com")
        # Every employee shown belongs to MSC (checked via DB — the list query is tenant_query).
        html = self.client.get("/company/employees").get_data(as_text=True)
        stellar_emp = Employee.query.filter(
            Employee.client_company_id != self.msc.client_company_id
        ).first()
        self.assertNotIn(stellar_emp.staff_id, html)


    def _upload_payroll_draft(self):
        """Create an import DRAFT so the preview page has something to render.

        A period the seeded MSC run does not already use, so the duplicate-period
        guard does not reject it. Confirming the draft is deliberately not done —
        the preview is the page under test.
        """
        workbook = Workbook()
        sheet = workbook.active
        sheet.append([
            "staff id", "full name", "ssnit no", "basic salary", "transport allowance",
            "housing allowance", "overtime pay", "gross pay", "paye", "ssnit",
            "other deductions", "net pay",
        ])
        sheet.append(["ID-1", "Preview Worker", "SSN-ID-1", 2100, 100, 100, 0, 2300, 100, 80, 0, 2120])
        stream = BytesIO()
        workbook.save(stream)
        stream.seek(0)
        return self.client.post(
            "/company/runs/upload",
            data={"month": "March", "year": "2024", "payroll_file": (stream, "preview.xlsx")},
            content_type="multipart/form-data",
        )

    # --- company identity renders exactly once on every client page ---------
    def _identity_renders(self, html):
        """How many times this page tells the user whose data they are looking at.

        Two shapes count, and only two: the shell's identity line, and a page
        whose own <h1> IS the company name (the dashboard, which suppresses the
        shell line so it does not say it twice). Anything else that happens to
        contain the name — the Branding editor's live preview, the Employer
        field on the GRA schedule — is page content, not identity chrome.
        """
        body = re.sub(r"<title>.*?</title>", "", html, flags=re.S)
        name = re.escape(self.msc.client_company.name)
        return (
            len(re.findall(r'class="portal-pagehead"', body))
            + len(re.findall(r"<h1[^>]*>\s*" + name + r"\s*</h1>", body))
        )

    def test_every_client_page_names_the_company_exactly_once(self):
        """Multi-tenant safety: a tenant must always be able to see whose data is
        on screen, and must never be told twice.

        Zero is the failure that matters — a user acting on the wrong company's
        payroll. Twice is the one that erodes the first, because a line that
        repeats stops being read. Both are regressions this asserts against.

        The 404 is in the list on purpose. It is a client page — a tenant lands
        on it by mistyping a URL inside their own portal — and it was rendering
        the OPERATOR shell: no identity at all, an operator sidebar, and a way
        out that pointed at a page a tenant is bounced from.

        The list is meant to be EXHAUSTIVE, not a sample: every template that
        extends client/base.html reaches it through at least one route here.
        Adding a client page means adding its route to this list. To re-check
        that nothing has escaped::

            grep -rl 'extends "client/base.html"' app/templates/

        Two routes are listed per shared template where the object-bound one
        is a distinct render path (employee add/edit, expense add/edit) —
        expense_detail.html was reachable only through the object-bound
        route and was the one template a page-level sample had missed.
        """
        self._login("admin@msc.com")
        self._upload_payroll_draft()
        draft = ImportBatch.query.filter_by(
            client_company_id=self.msc.client_company_id
        ).order_by(ImportBatch.id.desc()).first()
        self.assertIsNotNone(draft, "fixture: the preview page needs a draft to render")
        expense = Expense.query.filter_by(
            client_company_id=self.msc.client_company_id
        ).first()
        self.assertIsNotNone(expense, "fixture: the expense pages need a seeded expense")

        pages = [
            "/company",
            "/company/employees",
            "/company/employees/add",
            f"/company/employees/{self.msc_emp.id}/edit",
            "/company/runs",
            f"/company/runs/{self.msc_run.id}",
            "/company/runs/upload",
            f"/company/runs/{self.msc_run.id}/reports",
            f"/company/runs/{self.msc_run.id}/distribute",
            f"/company/imports/{draft.id}/preview",
            "/company/statutory",
            "/company/expenses",
            "/company/expenses/add",
            f"/company/expenses/{expense.id}",
            f"/company/expenses/{expense.id}/edit",
            "/company/audit",
            "/company/branding",
            "/notifications",
            "/company/no-such-page",          # 404, in the tenant's own shell
        ]
        for path in pages:
            resp = self.client.get(path)
            self.assertIn(resp.status_code, (200, 404), path)
            html = resp.get_data(as_text=True)
            self.assertEqual(self._identity_renders(html), 1, f"identity count on {path}")
            self.assertIn(self.msc.client_company.name, html, path)

    def test_tenant_error_page_stays_on_the_tenant_plane(self):
        """A tenant's 404 must not hand them the operator console.

        Before this, errors/404.html extended the operator base, so the way out
        was a "Return to Dashboard" button pointing at /dashboard — which
        redirects a tenant straight back to /company — beside a sidebar link to
        Client Companies, a page belonging to the other plane entirely.
        """
        self._login("admin@msc.com")
        resp = self.client.get("/company/no-such-page")
        self.assertEqual(resp.status_code, 404)
        html = resp.get_data(as_text=True)
        # The way out points at the tenant's own dashboard, not the operator one.
        self.assertIn('href="/company"', html)
        self.assertNotIn('href="/clients"', html)
        self.assertNotIn('href="/dashboard"', html)

    def test_operator_error_page_is_unchanged(self):
        """The tenant shell is chosen by plane, not applied to everyone."""
        self._login("operator@payrolla.com")
        html = self.client.get("/no-such-page").get_data(as_text=True)
        self.assertIn('href="/clients"', html)
        self.assertNotIn("portal-pagehead", html)


if __name__ == "__main__":
    unittest.main()
