"""Phase 3, Slice 4 — the distribution monitoring dashboard.

Operator-plane, cross-tenant, read-only. Exercises the aggregate stats service
and the route (auth, live fragment) rather than pixel layout.
"""

import os
import unittest
from datetime import datetime, timedelta, timezone

os.environ["SKIP_DOTENV"] = "true"
os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["SEED_DEMO_DATA"] = "true"
os.environ["PERSISTENCE_REQUIRED"] = "false"

from app import create_app, db  # noqa: E402
from app.distribution.dashboard import (  # noqa: E402
    collect_dashboard_stats,
    resolve_window,
)
from app.distribution.queue import enqueue_distribution, process_all_queued  # noqa: E402
from app.models import (  # noqa: E402
    DistributionBatch,
    PayrollRun,
    PayslipDelivery,
    User,
)


class DashboardStatsTestCase(unittest.TestCase):
    def setUp(self):
        self.app = create_app()
        self.app.config["TESTING"] = True
        self.ctx = self.app.app_context()
        self.ctx.push()
        self.run = PayrollRun.query.filter_by(status="Approved").first()
        self.operator = User.query.filter_by(email="admin@payrolla.com").first()

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

    def test_outcomes_are_windowed_but_state_is_not(self):
        """The distinction the whole module was reshaped around (DDEP Phase 2).

        A success rate accumulated since first deploy never moves again and is
        therefore not a measurement of anything an operator can act on; a batch
        queued six weeks ago is still queued and must not vanish behind a
        30-day filter. Outcomes take the window; state ignores it."""
        enqueue_distribution(self.run, "auto", False, self.operator)
        process_all_queued()

        # Age the completed batch and its deliveries out of a 7-day window,
        # then queue fresh work that is unambiguously current.
        old = datetime.now(timezone.utc) - timedelta(days=45)
        for batch in DistributionBatch.query.all():
            batch.created_at = old
        for delivery in PayslipDelivery.query.all():
            delivery.created_at = old
        db.session.commit()
        enqueue_distribution(self.run, "auto", True, self.operator)

        recent = collect_dashboard_stats(window_days=7)
        self.assertEqual(recent["deliveries"]["sent"], 0, "outcome must be windowed")
        self.assertEqual(recent["batches"]["completed"], 0, "outcome must be windowed")
        self.assertEqual(recent["batches"]["queued"], 1, "state must NOT be windowed")

        everything = collect_dashboard_stats(window_days=0)
        self.assertGreater(everything["deliveries"]["sent"], 0)
        self.assertEqual(everything["batches"]["completed"], 1)
        self.assertEqual(everything["batches"]["queued"], 1)

    def test_final_failures_are_counted_not_subtracted(self):
        """`failed - active_retries` mixed a windowed total with an unwindowed
        one, so it could go negative and was clamped at zero to hide it. "No
        retry is coming" is a property of the row."""
        enqueue_distribution(self.run, "auto", False, self.operator)
        process_all_queued()
        delivery = PayslipDelivery.query.first()
        delivery.status = "failed"
        delivery.next_retry_at = None  # retries exhausted
        db.session.commit()

        stats = collect_dashboard_stats()
        self.assertEqual(stats["deliveries"]["final_failures"], 1)
        self.assertEqual(stats["deliveries"]["active_retries"], 0)

        delivery.next_retry_at = datetime.now(timezone.utc) + timedelta(minutes=5)
        db.session.commit()
        stats = collect_dashboard_stats()
        self.assertEqual(stats["deliveries"]["final_failures"], 0)
        self.assertEqual(stats["deliveries"]["active_retries"], 1)

    def test_an_unknown_window_falls_back_rather_than_scanning_everything(self):
        for value in (None, "", "nonsense", "999999", "-1"):
            self.assertEqual(resolve_window(value), 30)
        self.assertEqual(resolve_window("7"), 7)
        self.assertEqual(resolve_window(0), 0)  # all time is a real option

    def test_in_flight_covers_every_unfinished_state(self):
        """Running, queued and scheduled in ONE list. The page previously showed
        a running-only panel beside a six-row status legend, and a scheduled
        batch appeared in neither."""
        enqueue_distribution(self.run, "auto", False, self.operator)
        rows = collect_dashboard_stats()["in_flight"]
        self.assertEqual([r["batch"].status for r in rows], ["queued"])
        rows[0]["batch"].status = "scheduled"
        db.session.commit()
        self.assertEqual(
            [r["batch"].status for r in collect_dashboard_stats()["in_flight"]],
            ["scheduled"],
        )

    def test_attention_always_renders_at_least_one_signal(self):
        """A panel that draws nothing when all is well reads as broken."""
        signals = collect_dashboard_stats()["attention"]
        self.assertEqual(len(signals), 1)
        self.assertEqual(signals[0]["tone"], "ok")

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
        self._login("admin@payrolla.com")
        page = self.http.get("/distribution/dashboard")
        self.assertEqual(page.status_code, 200)
        self.assertIn("Distribution Monitor", page.get_data(as_text=True))
        frag = self.http.get("/distribution/dashboard/fragment")
        self.assertEqual(frag.status_code, 200)
        self.assertIn("distribution-monitor", frag.get_data(as_text=True))

    def test_tenant_user_is_blocked_from_the_operator_dashboard(self):
        self._login("admin@msc.com")  # a client_admin
        resp = self.http.get("/distribution/dashboard", follow_redirects=False)
        self.assertEqual(resp.status_code, 302)
        self.assertNotIn("/distribution/dashboard", resp.headers["Location"])

    def test_every_batch_state_renders_a_status(self):
        """Recent distributions ran a five-branch if/elif with no `scheduled`
        arm, so a scheduled batch rendered an EMPTY status cell. Both the table
        and the queue-status panel now go through macros/distribution.html's one
        batch_pill, which owns all six states."""
        self._login("admin@payrolla.com")
        run = PayrollRun.query.filter_by(status="Approved").first()
        operator = User.query.filter_by(email="admin@payrolla.com").first()
        enqueue_distribution(run, "auto", False, operator)
        batch = DistributionBatch.query.first()

        for status, label in (
            ("scheduled", "Scheduled"),
            ("queued", "Queued"),
            ("running", "Sending"),
            ("completed", "Completed"),
            ("failed", "Failed"),
            ("cancelled", "Cancelled"),
        ):
            batch.status = status
            db.session.commit()
            html = self.http.get("/distribution/dashboard").get_data(as_text=True)
            self.assertIn(label, html, f"{status} rendered no status label")

    def test_the_window_travels_with_the_polling_fragment(self):
        """Without it the first auto-refresh would silently snap the page back
        to the default period the operator did not choose."""
        self._login("admin@payrolla.com")
        run = PayrollRun.query.filter_by(status="Approved").first()
        operator = User.query.filter_by(email="admin@payrolla.com").first()
        enqueue_distribution(run, "auto", False, operator)  # makes stats.live true

        html = self.http.get("/distribution/dashboard?window=7").get_data(as_text=True)
        self.assertIn("window=7", html)
        self.assertIn("last 7 days", html)
        frag = self.http.get("/distribution/dashboard/fragment?window=7")
        self.assertIn("last 7 days", frag.get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()
