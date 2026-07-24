"""Phase 2 — breadcrumb trail on pages nested under the sidebar.

macros/breadcrumbs.html renders a (label, href) trail; wired into the four
pages where the parent/child relationship isn't visible from the sidebar
alone: payroll run detail, client detail, employee roster, and payslip
distribution. Base.html's ``breadcrumbs`` block is empty by default, so pages
that don't opt in (dashboard, payroll runs list) show nothing.
"""

import os
import unittest

os.environ["SKIP_DOTENV"] = "true"
os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["SEED_DEMO_DATA"] = "true"
os.environ["PERSISTENCE_REQUIRED"] = "false"

from app import create_app, db  # noqa: E402
from app.models import ClientCompany, PayrollRun  # noqa: E402


class BreadcrumbsTestCase(unittest.TestCase):
    def setUp(self):
        self.app = create_app()
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()
        self.ctx = self.app.app_context()
        self.ctx.push()
        self.assertEqual(
            self.client.post(
                "/login", data={"email": "admin@chrisnat.local", "password": "password123"}
            ).status_code,
            302,
        )
        self.company = ClientCompany.query.first()

    def tearDown(self):
        self.ctx.pop()

    def _get(self, url):
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        return resp.get_data(as_text=True)

    def test_payroll_run_detail_breadcrumb(self):
        run = PayrollRun(
            month="August", year=2099, status="Draft",
            client_company_id=self.company.id,
        )
        db.session.add(run)
        db.session.commit()
        html = self._get(f"/payroll/runs/{run.id}")
        self.assertIn("payrolla-breadcrumb", html)
        self.assertIn(f'href="/dashboard"', html)
        self.assertIn(f'href="/payroll/runs"', html)
        self.assertIn(f'href="/clients/{self.company.id}"', html)
        self.assertIn(self.company.name, html)
        self.assertIn("August 2099", html)
        # The current page is the unlinked, active crumb.
        self.assertIn('<li class="breadcrumb-item active" aria-current="page">August 2099</li>', html)

    def test_payroll_run_detail_breadcrumb_without_client(self):
        # A run with no client_company_id (shouldn't happen in practice, but
        # the trail-building logic must not crash on it).
        run = PayrollRun(month="August", year=2099, status="Draft")
        db.session.add(run)
        db.session.commit()
        html = self._get(f"/payroll/runs/{run.id}")
        self.assertIn("payrolla-breadcrumb", html)
        self.assertIn("August 2099", html)

    def test_client_detail_breadcrumb(self):
        html = self._get(f"/clients/{self.company.id}")
        self.assertIn("payrolla-breadcrumb", html)
        self.assertIn('href="/dashboard"', html)
        self.assertIn('href="/clients"', html)
        self.assertIn(
            f'<li class="breadcrumb-item active" aria-current="page">{self.company.name}</li>',
            html,
        )

    def test_employee_roster_breadcrumb_links_back_to_client(self):
        html = self._get(f"/employees/clients/{self.company.id}/roster")
        self.assertIn("payrolla-breadcrumb", html)
        self.assertIn('href="/dashboard"', html)
        self.assertIn('href="/clients"', html)
        self.assertIn(f'href="/clients/{self.company.id}"', html)
        self.assertIn(self.company.name, html)
        self.assertIn(
            '<li class="breadcrumb-item active" aria-current="page">Employee Roster</li>', html
        )

    def test_distribution_run_status_breadcrumb_links_back_to_run(self):
        run = PayrollRun(
            month="August", year=2099, status="Approved",
            client_company_id=self.company.id,
        )
        db.session.add(run)
        db.session.commit()
        html = self._get(f"/distribution/run/{run.id}")
        self.assertIn("payrolla-breadcrumb", html)
        self.assertIn('href="/dashboard"', html)
        self.assertIn('href="/payroll/runs"', html)
        self.assertIn(f'href="/clients/{self.company.id}"', html)
        self.assertIn(f'href="/payroll/runs/{run.id}"', html)
        self.assertIn(
            '<li class="breadcrumb-item active" aria-current="page">Payslip Delivery</li>', html
        )

    def test_dashboard_has_no_breadcrumb(self):
        # Top-level pages reachable directly from the sidebar don't need one.
        html = self._get("/dashboard")
        self.assertNotIn("payrolla-breadcrumb", html)

    def test_payroll_runs_list_has_no_breadcrumb(self):
        html = self._get("/payroll/runs")
        self.assertNotIn("payrolla-breadcrumb", html)


if __name__ == "__main__":
    unittest.main()
