"""Phase 3, Slice 4 — the distribution monitoring dashboard.

Operator-plane, cross-tenant, read-only. Exercises the aggregate stats service
and the route (auth, live fragment) rather than pixel layout.
"""

import os
import unittest

os.environ["SKIP_DOTENV"] = "true"
os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["SEED_DEMO_DATA"] = "true"
os.environ["PERSISTENCE_REQUIRED"] = "false"

from app import create_app, db  # noqa: E402
from app.distribution.dashboard import collect_dashboard_stats  # noqa: E402
from app.distribution.queue import enqueue_distribution, process_all_queued  # noqa: E402
from app.models import PayrollRun, User  # noqa: E402


class DashboardStatsTestCase(unittest.TestCase):
    def setUp(self):
        self.app = create_app()
        self.app.config["TESTING"] = True
        self.ctx = self.app.app_context()
        self.ctx.push()
        self.run = PayrollRun.query.filter_by(status="Approved").first()
        self.operator = User.query.filter_by(email="admin@chrisnat.local").first()

    def tearDown(self):
        db.session.remove()
        self.ctx.pop()

    def test_stats_reflect_a_queued_then_completed_batch(self):
        empty = collect_dashboard_stats()
        self.assertEqual(empty["batches"]["total"], 0)

        enqueue_distribution(self.run, "auto", False, self.operator)
        queued = collect_dashboard_stats()
        self.assertEqual(queued["batches"]["queued"], 1)
        self.assertEqual(queued["backlog"]["queued_batches"], 1)
        self.assertGreater(queued["backlog"]["queued_payslips"], 0)

        process_all_queued()
        done = collect_dashboard_stats()
        self.assertEqual(done["batches"]["completed"], 1)
        self.assertEqual(done["batches"]["queued"], 0)
        self.assertGreater(done["deliveries"]["sent"], 0)
        self.assertEqual(done["deliveries"]["success_rate"], 100.0)
        self.assertEqual(len(done["recent_batches"]), 1)
        self.assertIsNotNone(done["last_processed_at"])

    def test_worker_health_flags_stalled_backlog(self):
        # A queued batch with no worker heartbeat and no recent processing reads
        # as a stall — the signal the dashboard raises.
        enqueue_distribution(self.run, "auto", False, self.operator)
        stats = collect_dashboard_stats()
        self.assertEqual(stats["worker"]["status"], "stalled")

    def test_success_and_failure_rates_are_computed(self):
        # Strip one item's contact so the batch has a mix of sent + failed.
        item = self.run.items[0]
        item.momo_number = None
        item.email = None
        if item.employee:
            item.employee.phone = None
            item.employee.momo_number = None
            item.employee.email = None
        db.session.commit()
        enqueue_distribution(self.run, "sms", False, self.operator)
        process_all_queued()
        d = collect_dashboard_stats()["deliveries"]
        self.assertGreaterEqual(d["failed"], 1)
        self.assertEqual(round(d["success_rate"] + d["failure_rate"], 1), 100.0)


class DashboardRouteTestCase(unittest.TestCase):
    def setUp(self):
        self.app = create_app()
        self.app.config["TESTING"] = True
        self.http = self.app.test_client()
        self.ctx = self.app.app_context()
        self.ctx.push()

    def tearDown(self):
        db.session.remove()
        self.ctx.pop()

    def _login(self, email):
        self.http.post("/login", data={"email": email, "password": "password123"})

    def test_operator_sees_dashboard_and_fragment(self):
        self._login("admin@chrisnat.local")
        page = self.http.get("/distribution/dashboard")
        self.assertEqual(page.status_code, 200)
        self.assertIn("Distribution Monitor", page.get_data(as_text=True))
        frag = self.http.get("/distribution/dashboard/fragment")
        self.assertEqual(frag.status_code, 200)
        self.assertIn("distribution-monitor", frag.get_data(as_text=True))

    def test_tenant_user_is_blocked_from_the_operator_dashboard(self):
        self._login("admin@msc.demo")  # a client_admin
        resp = self.http.get("/distribution/dashboard", follow_redirects=False)
        self.assertEqual(resp.status_code, 302)
        self.assertNotIn("/distribution/dashboard", resp.headers["Location"])


if __name__ == "__main__":
    unittest.main()
