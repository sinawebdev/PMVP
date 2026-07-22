"""Phase 3, Slice 6 — searchable delivery history + batch detail pages.

Operator-plane, cross-tenant, read-only. Exercises the filtered/paginated query
service and the routes.
"""

import os
import unittest

os.environ["SKIP_DOTENV"] = "true"
os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["SEED_DEMO_DATA"] = "true"
os.environ["PERSISTENCE_REQUIRED"] = "false"

from app import create_app, db  # noqa: E402
from app.distribution.history import search_deliveries  # noqa: E402
from app.distribution.queue import enqueue_distribution, process_all_queued  # noqa: E402
from app.models import DistributionBatch, PayrollRun, PayslipDelivery, User  # noqa: E402


class HistorySearchTestCase(unittest.TestCase):
    def setUp(self):
        self.app = create_app()
        self.app.config["TESTING"] = True
        self.ctx = self.app.app_context()
        self.ctx.push()
        self.run = PayrollRun.query.filter_by(status="Approved").first()
        self.operator = User.query.filter_by(email="admin@chrisnat.local").first()
        # One completed distribution so there is history to search.
        enqueue_distribution(self.run, "auto", False, self.operator)
        process_all_queued()

    def tearDown(self):
        db.session.remove()
        self.ctx.pop()

    def test_deliveries_are_attributed_to_the_initiating_batch(self):
        d = PayslipDelivery.query.filter_by(payroll_run_id=self.run.id).first()
        self.assertIsNotNone(d.distribution_batch_id)
        self.assertEqual(d.distribution_batch.initiated_by_user_id, self.operator.id)

    def test_search_filters_by_company_and_status(self):
        page = search_deliveries(
            {"company_id": str(self.run.client_company_id), "status": "sent"}
        )
        self.assertGreater(page.total, 0)
        self.assertTrue(all(d.status == "sent" for d in page.items))
        self.assertTrue(
            all(d.payroll_run.client_company_id == self.run.client_company_id
                for d in page.items)
        )

    def test_search_filters_by_operator(self):
        page = search_deliveries({"operator_id": str(self.operator.id)})
        self.assertGreater(page.total, 0)
        page_none = search_deliveries({"operator_id": "999999"})
        self.assertEqual(page_none.total, 0)

    def test_search_by_text_matches_staff_id(self):
        item = self.run.items[0]
        page = search_deliveries({"q": item.staff_id})
        self.assertGreaterEqual(page.total, 1)

    def test_pagination_limits_page_size(self):
        page = search_deliveries({}, page=1)
        self.assertLessEqual(len(page.items), 25)
        self.assertEqual(page.page, 1)

    def test_status_filter_rejects_unknown_value(self):
        # An unknown status is ignored (not injected into SQL) — returns all.
        page = search_deliveries({"status": "bogus"})
        self.assertGreater(page.total, 0)


class HistoryRouteTestCase(unittest.TestCase):
    def setUp(self):
        self.app = create_app()
        self.app.config["TESTING"] = True
        self.http = self.app.test_client()
        self.ctx = self.app.app_context()
        self.ctx.push()
        self.run = PayrollRun.query.filter_by(status="Approved").first()
        self.operator = User.query.filter_by(email="admin@chrisnat.local").first()
        enqueue_distribution(self.run, "auto", False, self.operator)
        process_all_queued()

    def tearDown(self):
        db.session.remove()
        self.ctx.pop()

    def _login(self, email):
        self.http.post("/login", data={"email": email, "password": "password123"})

    def test_operator_sees_history_and_batch_detail(self):
        self._login("admin@chrisnat.local")
        hist = self.http.get("/distribution/history")
        self.assertEqual(hist.status_code, 200)
        self.assertIn("Distribution History", hist.get_data(as_text=True))

        batch = DistributionBatch.query.filter_by(payroll_run_id=self.run.id).first()
        detail = self.http.get(f"/distribution/batch/{batch.id}")
        self.assertEqual(detail.status_code, 200)
        self.assertIn("Initiated by", detail.get_data(as_text=True))

    def test_tenant_user_is_blocked_from_history(self):
        self._login("admin@msc.demo")
        resp = self.http.get("/distribution/history", follow_redirects=False)
        self.assertEqual(resp.status_code, 302)
        self.assertNotIn("/distribution/history", resp.headers["Location"])


if __name__ == "__main__":
    unittest.main()
