"""Phase 1 — roles, tenant resolution, and login routing.

Runs against a fresh in-memory SQLite DB (never the production Supabase DB).
SKIP_DOTENV keeps the repo .env (PERSISTENCE_REQUIRED=true, pooler URL) out of
the test process so the SQLite persistence assertion doesn't trip.
"""

import os
import unittest

os.environ["SKIP_DOTENV"] = "true"
os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["SEED_DEMO_DATA"] = "true"  # seed employees + one MSC run for scoping tests
os.environ["PERSISTENCE_REQUIRED"] = "false"

from flask_login import login_user  # noqa: E402

from app import create_app, db  # noqa: E402
from app.models import Employee, PayrollItem, User  # noqa: E402
from app import roles  # noqa: E402
from app.tenancy import active_tenant_id, tenant_query  # noqa: E402


class TenancyTestCase(unittest.TestCase):
    def setUp(self):
        self.app = create_app()
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()
        self.ctx = self.app.app_context()
        self.ctx.push()
        self.platform = User.query.filter_by(email="chrisnat.admin@chrisnat.local").first()
        self.msc = User.query.filter_by(email="admin@msc.demo").first()
        self.stellar = User.query.filter_by(email="admin@stellar.demo").first()

    def tearDown(self):
        self.ctx.pop()

    # --- seed / roles -------------------------------------------------------
    def test_seed_creates_platform_and_tenant_admins(self):
        self.assertIsNotNone(self.platform)
        self.assertEqual(self.platform.role, "chrisnat_admin")
        self.assertIsNone(self.platform.client_company_id)
        self.assertTrue(roles.is_platform_user(self.platform))
        self.assertFalse(roles.is_tenant_user(self.platform))

        for admin in (self.msc, self.stellar):
            self.assertIsNotNone(admin)
            self.assertEqual(admin.role, "client_admin")
            self.assertIsNotNone(admin.client_company_id)
            self.assertTrue(roles.is_tenant_user(admin))
            self.assertFalse(roles.is_platform_user(admin))
        self.assertNotEqual(self.msc.client_company_id, self.stellar.client_company_id)

    # --- tenant resolution --------------------------------------------------
    def test_active_tenant_resolution(self):
        with self.app.test_request_context():
            login_user(self.platform)
            self.assertIsNone(active_tenant_id())
        with self.app.test_request_context():
            login_user(self.msc)
            self.assertEqual(active_tenant_id(), self.msc.client_company_id)

    def test_tenant_query_scopes_direct_model(self):
        # Platform sees all employees; each tenant sees only its own; disjoint.
        with self.app.test_request_context():
            login_user(self.platform)
            all_ids = {e.id for e in tenant_query(Employee).all()}
        with self.app.test_request_context():
            login_user(self.msc)
            msc = tenant_query(Employee).all()
            msc_ids = {e.id for e in msc}
        with self.app.test_request_context():
            login_user(self.stellar)
            stellar_ids = {e.id for e in tenant_query(Employee).all()}

        self.assertTrue(msc_ids)
        self.assertTrue(stellar_ids)
        self.assertTrue(msc_ids.isdisjoint(stellar_ids))
        self.assertTrue(msc_ids <= all_ids and stellar_ids <= all_ids)
        for e in msc:
            self.assertEqual(e.client_company_id, self.msc.client_company_id)

    def test_child_table_scoped_via_run(self):
        # Seed creates one MSC payroll run with items; Stellar has none.
        with self.app.test_request_context():
            login_user(self.msc)
            msc_items = tenant_query(PayrollItem).count()
        with self.app.test_request_context():
            login_user(self.stellar)
            stellar_items = tenant_query(PayrollItem).count()
        with self.app.test_request_context():
            login_user(self.platform)
            all_items = tenant_query(PayrollItem).count()
        self.assertGreater(msc_items, 0)
        self.assertEqual(stellar_items, 0)
        self.assertEqual(all_items, msc_items)

    # --- login routing ------------------------------------------------------
    def test_login_routes_tenant_to_company_dashboard(self):
        resp = self.client.post(
            "/login", data={"email": "admin@msc.demo", "password": "password123"}
        )
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(resp.headers["Location"].endswith("/company"))

    def test_login_routes_platform_to_oversight(self):
        resp = self.client.post(
            "/login",
            data={"email": "chrisnat.admin@chrisnat.local", "password": "password123"},
        )
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(resp.headers["Location"].endswith("/dashboard"))


if __name__ == "__main__":
    unittest.main()
