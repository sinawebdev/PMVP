"""chrisnat_admin (SaaS-era platform superuser) has full operator access.

Confirms the policy settled with Sina: chrisnat_admin sees the operator nav and
may reach operator routes gated by ``role_required`` (statutory, audit, …) — which
the legacy operator role lists did not grant. Runs on in-memory SQLite (never the
production Supabase DB).
"""

import os
import unittest

os.environ["SKIP_DOTENV"] = "true"
os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["SEED_DEMO_DATA"] = "true"
os.environ["PERSISTENCE_REQUIRED"] = "false"

from flask import url_for  # noqa: E402

from app import create_app, permissions  # noqa: E402

CHRISNAT_ADMIN = "chrisnat_admin"


class ChrisnatAdminAccessTestCase(unittest.TestCase):
    def setUp(self):
        self.app = create_app()
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()

    def _login_platform(self):
        resp = self.client.post(
            "/login",
            data={"email": "chrisnat.admin@chrisnat.local", "password": "password123"},
        )
        self.assertEqual(resp.status_code, 302)  # -> /dashboard

    def test_predicates_grant_every_operator_capability(self):
        self.assertTrue(permissions.can_operate_payroll(CHRISNAT_ADMIN))
        self.assertTrue(permissions.can_maintain_roster(CHRISNAT_ADMIN))
        self.assertTrue(permissions.can_view_audit(CHRISNAT_ADMIN))
        self.assertTrue(permissions.can_manage_statutory(CHRISNAT_ADMIN))

    def test_reaches_role_required_operator_routes(self):
        # statutory.index is @role_required("admin"); audit is
        # @role_required("admin", "md"). chrisnat_admin was in neither list, so
        # before the superuser pass these would 302-bounce to /dashboard.
        self._login_platform()
        with self.app.test_request_context():
            statutory_url = url_for("statutory.index")
            audit_url = url_for("audit.audit_trail")
        self.assertEqual(self.client.get(statutory_url).status_code, 200)
        self.assertEqual(self.client.get(audit_url).status_code, 200)

    def test_sees_operator_nav_links(self):
        self._login_platform()
        body = self.client.get("/dashboard").get_data(as_text=True)
        self.assertIn("Statutory Rates", body)   # can_manage_statutory
        self.assertIn("Expenses", body)          # "Expenses & Audit" (can_view_audit)


if __name__ == "__main__":
    unittest.main()
