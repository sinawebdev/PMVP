"""Phase 2 — cross-tenant isolation.

Proves a tenant (client) user can never reach another tenant's data:
  * every oversight/operator route redirects a tenant user to /company (302),
    never 200-with-data;
  * a platform (Chrisnat) user still gets those routes;
  * the object helpers (owns_object / tenant_get_or_404) deny cross-tenant
    objects — direct-owned (Employee, PayrollRun) and child-via-run (PayrollItem)
    — returning 404, never the row.

In-memory SQLite, seeded with demo data (one MSC payroll run with items).
"""

import os
import unittest

os.environ["SKIP_DOTENV"] = "true"
os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["SEED_DEMO_DATA"] = "true"
os.environ["PERSISTENCE_REQUIRED"] = "false"

from flask_login import login_user  # noqa: E402
from werkzeug.exceptions import NotFound  # noqa: E402

from app import create_app, db  # noqa: E402
from app.models import Employee, PayrollItem, PayrollRun, User  # noqa: E402
from app.tenancy import owns_object, tenant_get_or_404  # noqa: E402

# Oversight/operator GET routes that must never serve a tenant user.
OVERSIGHT_ROUTES = ["/dashboard", "/clients", "/search?q=a", "/payroll/runs", "/payslip"]


class TenantIsolationTestCase(unittest.TestCase):
    def setUp(self):
        self.app = create_app()
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()
        self.ctx = self.app.app_context()
        self.ctx.push()

        self.msc_user = User.query.filter_by(email="admin@msc.demo").first()
        self.stellar_user = User.query.filter_by(email="admin@stellar.demo").first()
        self.platform_user = User.query.filter_by(email="chrisnat.admin@chrisnat.local").first()
        # The seeded MSC payroll run (+ an item) and an MSC employee — the
        # cross-tenant objects a Stellar user must be denied.
        self.msc_run = PayrollRun.query.filter_by(
            client_company_id=self.msc_user.client_company_id
        ).first()
        self.msc_item = PayrollItem.query.filter_by(payroll_run_id=self.msc_run.id).first()
        self.msc_emp = Employee.query.filter_by(
            client_company_id=self.msc_user.client_company_id
        ).first()
        self.stellar_company_id = self.stellar_user.client_company_id

    def tearDown(self):
        self.ctx.pop()

    def _login(self, email):
        resp = self.client.post(
            "/login", data={"email": email, "password": "password123"}
        )
        self.assertEqual(resp.status_code, 302)

    # --- route level: tenant user is bounced, never served -----------------
    def test_tenant_user_bounced_from_oversight_routes(self):
        self._login("admin@msc.demo")
        routes = OVERSIGHT_ROUTES + [
            f"/clients/{self.stellar_company_id}",      # another tenant's client page
            f"/payroll/runs/{self.msc_run.id}",         # operator run detail
        ]
        for route in routes:
            resp = self.client.get(route)
            self.assertEqual(resp.status_code, 302, f"{route} should redirect")
            self.assertTrue(
                resp.headers["Location"].endswith("/company"),
                f"{route} should redirect to /company, got {resp.headers['Location']}",
            )

    def test_tenant_user_bounced_from_operator_action_route(self):
        # A role_required operator route (GET on an edit view) also bounces.
        self._login("admin@msc.demo")
        resp = self.client.get(f"/payroll/runs/{self.msc_run.id}/items/edit")
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(resp.headers["Location"].endswith("/company"))

    def test_platform_user_still_gets_oversight_routes(self):
        self._login("chrisnat.admin@chrisnat.local")
        for route in OVERSIGHT_ROUTES:
            resp = self.client.get(route)
            self.assertEqual(resp.status_code, 200, f"platform user should get {route}")

    # --- object level: helpers deny cross-tenant, allow own ----------------
    def test_owns_object_cross_tenant(self):
        with self.app.test_request_context():
            login_user(self.msc_user)
            self.assertTrue(owns_object(self.msc_run))
            self.assertTrue(owns_object(self.msc_emp))
            self.assertTrue(owns_object(self.msc_item))  # child via run
        with self.app.test_request_context():
            login_user(self.stellar_user)
            self.assertFalse(owns_object(self.msc_run))
            self.assertFalse(owns_object(self.msc_emp))
            self.assertFalse(owns_object(self.msc_item))
        with self.app.test_request_context():
            login_user(self.platform_user)
            self.assertTrue(owns_object(self.msc_run))  # platform spans tenants

    def test_tenant_get_or_404_cross_tenant(self):
        # Owner resolves; other tenant gets NotFound (404), never the row.
        with self.app.test_request_context():
            login_user(self.msc_user)
            self.assertEqual(tenant_get_or_404(PayrollRun, self.msc_run.id).id, self.msc_run.id)
            self.assertEqual(tenant_get_or_404(PayrollItem, self.msc_item.id).id, self.msc_item.id)
        with self.app.test_request_context():
            login_user(self.stellar_user)
            for model, ident in [
                (PayrollRun, self.msc_run.id),
                (Employee, self.msc_emp.id),
                (PayrollItem, self.msc_item.id),  # child via run join
            ]:
                with self.assertRaises(NotFound):
                    tenant_get_or_404(model, ident)

    # --- write level: a mutation endpoint is isolated too (Phase 5) --------
    def test_tenant_user_cannot_write_another_tenants_resource(self):
        # A POST that mutates, not just a GET: a Stellar user deactivating an MSC
        # employee is denied (404) and the employee is left untouched — proving no
        # write leaks across tenants.
        self.msc_emp.status = "Active"
        db.session.commit()
        self._login("admin@stellar.demo")
        resp = self.client.post(
            f"/company/employees/{self.msc_emp.id}/deactivate"
        )
        self.assertEqual(resp.status_code, 404)
        db.session.refresh(self.msc_emp)
        self.assertEqual(self.msc_emp.status, "Active")  # unchanged

    def test_tenant_user_can_write_own_resource(self):
        # Positive control: the same endpoint works on the caller's own employee,
        # so the isolation above is denial, not a broken route.
        own = Employee.query.filter_by(
            client_company_id=self.stellar_user.client_company_id, status="Active"
        ).first()
        if own is None:
            self.skipTest("no active Stellar employee seeded")
        self._login("admin@stellar.demo")
        resp = self.client.post(f"/company/employees/{own.id}/deactivate")
        self.assertIn(resp.status_code, (302, 200))
        db.session.refresh(own)
        self.assertEqual(own.status, "Inactive")


if __name__ == "__main__":
    unittest.main()
