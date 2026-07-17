"""Phase 7 — end-to-end zero cross-visibility.

Complements the redirect/object-level checks in test_tenant_isolation.py with a
RESPONSE-level sweep: seed uniquely-marked data for two tenants, then assert that
across every rendered /company/* page (and notifications), one tenant never sees
the other tenant's markers — company name, employee, expense, or notification.
"""

import os
import unittest
from datetime import date

os.environ["SKIP_DOTENV"] = "true"
os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["SEED_DEMO_DATA"] = "true"
os.environ["PERSISTENCE_REQUIRED"] = "false"

from app import create_app, db  # noqa: E402
from app.models import Employee, Expense, Notification, PayrollRun, User  # noqa: E402

CLIENT_PAGES = [
    "/company", "/company/employees", "/company/runs", "/company/statutory",
    "/company/expenses", "/company/audit", "/notifications",
]


class CrossTenantVisibilityTestCase(unittest.TestCase):
    def setUp(self):
        self.app = create_app()
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()
        self.ctx = self.app.app_context()
        self.ctx.push()

        self.msc = User.query.filter_by(email="admin@msc.demo").first()
        self.stellar = User.query.filter_by(email="admin@stellar.demo").first()
        self.msc_cid = self.msc.client_company_id
        self.stellar_cid = self.stellar.client_company_id

        # Uniquely-tokenised markers per tenant.
        self._seed_markers("MSC", self.msc_cid, self.msc.id)
        self._seed_markers("STELLAR", self.stellar_cid, self.stellar.id)
        db.session.commit()

        # A token unique to each tenant that must never cross over.
        self.markers = {
            self.msc_cid: ["ZZMSCEMP", "ZZMSCEXP", "ZZMSCNOTE"],
            self.stellar_cid: ["ZZSTELLAREMP", "ZZSTELLAREXP", "ZZSTELLARNOTE"],
        }

    def tearDown(self):
        self.ctx.pop()

    def _seed_markers(self, tag, company_id, user_id):
        db.session.add(Employee(
            client_company_id=company_id, staff_id=f"ZZ{tag}1",
            full_name=f"ZZ{tag}EMP Worker", status="Active", basic_salary=1000,
        ))
        db.session.add(Expense(
            client_company_id=company_id, title=f"ZZ{tag}EXP marker expense",
            description=f"ZZ{tag}EXP", category="General", amount=12.34,
            expense_date=date(2024, 3, 1), status="Recorded", recorded_by=user_id,
        ))
        db.session.add(Notification(
            user_id=user_id, client_company_id=company_id,
            title="Marker", body=f"ZZ{tag}NOTE marker notification", level="info",
        ))

    def _login(self, email):
        self.assertEqual(
            self.client.post("/login", data={"email": email, "password": "password123"}).status_code,
            302,
        )

    def _sweep(self, email, own_cid, other_cid):
        self._login(email)
        for page in CLIENT_PAGES:
            resp = self.client.get(page)
            self.assertEqual(resp.status_code, 200, page)
            html = resp.get_data(as_text=True)
            for token in self.markers[other_cid]:
                self.assertNotIn(token, html, f"{token} leaked into {page} for {email}")
        self.client.get("/logout")

    def test_msc_never_sees_stellar_markers(self):
        self._sweep("admin@msc.demo", self.msc_cid, self.stellar_cid)

    def test_stellar_never_sees_msc_markers(self):
        self._sweep("admin@stellar.demo", self.stellar_cid, self.msc_cid)

    def test_each_tenant_sees_its_own_markers(self):
        # Guard the test: the markers ARE visible to their owner, so a passing
        # cross-visibility sweep is meaningful (not just empty pages).
        self._login("admin@msc.demo")
        emp_html = self.client.get("/company/employees").get_data(as_text=True)
        self.assertIn("ZZMSCEMP", emp_html)
        exp_html = self.client.get("/company/expenses").get_data(as_text=True)
        self.assertIn("ZZMSCEXP", exp_html)
        note_html = self.client.get("/notifications").get_data(as_text=True)
        self.assertIn("ZZMSCNOTE", note_html)


if __name__ == "__main__":
    unittest.main()
