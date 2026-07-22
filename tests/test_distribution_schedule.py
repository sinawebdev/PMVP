"""Phase 3, Slice 7 — scheduled distribution.

An operator schedules a distribution for a future time; it sits in the new
`scheduled` state until the worker activates it (scheduled -> queued -> run) when
due. Scheduling is timezone-safe (times stored UTC), editable and cancellable
before execution, and never runs twice.
"""

import os
import unittest
from datetime import datetime, timedelta, timezone

os.environ["SKIP_DOTENV"] = "true"
os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["SEED_DEMO_DATA"] = "true"
os.environ["PERSISTENCE_REQUIRED"] = "false"

from app import create_app, db  # noqa: E402
from app.distribution.queue import (  # noqa: E402
    activate_due_scheduled,
    cancel_distribution,
    enqueue_distribution,
    process_all_queued,
    reschedule_distribution,
)
from app.models import (  # noqa: E402
    BATCH_CANCELLED,
    BATCH_COMPLETED,
    BATCH_QUEUED,
    BATCH_SCHEDULED,
    AuditTrail,
    DistributionBatch,
    PayrollRun,
    PayslipDelivery,
    User,
)


class ScheduleTestCase(unittest.TestCase):
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

    def _future(self, minutes=30):
        return datetime.now(timezone.utc) + timedelta(minutes=minutes)

    def _past(self, minutes=5):
        return datetime.now(timezone.utc) - timedelta(minutes=minutes)

    def test_scheduling_creates_a_scheduled_batch_and_audits(self):
        summary = enqueue_distribution(
            self.run, "auto", False, self.operator, scheduled_for=self._future()
        )
        batch = db.session.get(DistributionBatch, summary["batch_id"])
        self.assertEqual(batch.status, BATCH_SCHEDULED)
        self.assertIsNotNone(batch.scheduled_for)
        self.assertIsNotNone(
            AuditTrail.query.filter_by(action="Distribution scheduled").first()
        )
        # Nothing is sent yet and the claim path ignores a scheduled batch.
        self.assertEqual(process_all_queued(), [])
        self.assertEqual(
            PayslipDelivery.query.filter_by(payroll_run_id=self.run.id).count(), 0
        )

    def test_a_past_time_queues_immediately(self):
        summary = enqueue_distribution(
            self.run, "auto", False, self.operator, scheduled_for=self._past()
        )
        batch = db.session.get(DistributionBatch, summary["batch_id"])
        self.assertEqual(batch.status, BATCH_QUEUED)

    def test_worker_activates_a_due_batch_then_runs_it(self):
        summary = enqueue_distribution(
            self.run, "auto", False, self.operator, scheduled_for=self._future()
        )
        batch = db.session.get(DistributionBatch, summary["batch_id"])
        # Not due yet.
        self.assertEqual(activate_due_scheduled(), [])
        self.assertEqual(batch.status, BATCH_SCHEDULED)

        # Time arrives: backdate and let the worker activate + run it.
        batch.scheduled_for = self._past()
        db.session.commit()
        activated = activate_due_scheduled()
        self.assertEqual(len(activated), 1)
        db.session.refresh(batch)
        self.assertEqual(batch.status, BATCH_QUEUED)
        self.assertIsNotNone(
            AuditTrail.query.filter_by(action="Scheduled distribution activated").first()
        )

        process_all_queued()
        db.session.refresh(batch)
        self.assertEqual(batch.status, BATCH_COMPLETED)
        self.assertGreater(
            PayslipDelivery.query.filter_by(payroll_run_id=self.run.id).count(), 0
        )

    def test_activation_never_runs_a_batch_twice(self):
        summary = enqueue_distribution(
            self.run, "auto", False, self.operator, scheduled_for=self._past()
        )
        # _past() actually queues immediately (status queued), so force a due
        # scheduled batch to exercise the activation guard directly.
        batch = db.session.get(DistributionBatch, summary["batch_id"])
        batch.status = BATCH_SCHEDULED
        batch.scheduled_for = self._past()
        db.session.commit()

        first = activate_due_scheduled()
        self.assertEqual(len(first), 1)
        # Second sweep sees nothing scheduled — no duplicate activation.
        self.assertEqual(activate_due_scheduled(), [])

    def test_reschedule_changes_time_only_while_scheduled(self):
        enqueue_distribution(
            self.run, "auto", False, self.operator, scheduled_for=self._future(30)
        )
        new_time = self._future(120)
        result = reschedule_distribution(self.run, new_time, self.operator)
        self.assertTrue(result["ok"])
        batch = DistributionBatch.query.filter_by(payroll_run_id=self.run.id).first()
        self.assertEqual(
            batch.scheduled_for.replace(tzinfo=timezone.utc).replace(microsecond=0),
            new_time.replace(microsecond=0),
        )
        self.assertIsNotNone(
            AuditTrail.query.filter_by(action="Distribution rescheduled").first()
        )
        # A past time is rejected.
        self.assertFalse(reschedule_distribution(self.run, self._past(), self.operator)["ok"])

    def test_cancel_a_scheduled_batch(self):
        enqueue_distribution(
            self.run, "auto", False, self.operator, scheduled_for=self._future()
        )
        result = cancel_distribution(self.run, self.operator)
        self.assertTrue(result["cancelled_batch"])
        batch = DistributionBatch.query.filter_by(payroll_run_id=self.run.id).first()
        self.assertEqual(batch.status, BATCH_CANCELLED)
        # A cancelled schedule never activates.
        self.assertEqual(activate_due_scheduled(), [])

    def test_scheduling_dedups_against_an_existing_pending_batch(self):
        enqueue_distribution(
            self.run, "auto", False, self.operator, scheduled_for=self._future()
        )
        again = enqueue_distribution(
            self.run, "auto", False, self.operator, scheduled_for=self._future(90)
        )
        self.assertTrue(again["already_in_progress"])
        self.assertEqual(
            DistributionBatch.query.filter_by(payroll_run_id=self.run.id).count(), 1
        )


class ScheduleRouteTestCase(unittest.TestCase):
    def setUp(self):
        self.app = create_app()
        self.app.config["TESTING"] = True
        self.http = self.app.test_client()
        self.ctx = self.app.app_context()
        self.ctx.push()
        self.run = PayrollRun.query.filter_by(status="Approved").first()

    def tearDown(self):
        db.session.remove()
        self.ctx.pop()

    def _login(self, email):
        self.http.post("/login", data={"email": email, "password": "password123"})

    def test_operator_schedule_route_creates_scheduled_batch(self):
        self._login("admin@chrisnat.local")
        when = (datetime.now(timezone.utc) + timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M")
        resp = self.http.post(
            f"/distribution/run/{self.run.id}/schedule",
            data={"channel": "auto", "scheduled_for": when},
            follow_redirects=True,
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(
            DistributionBatch.query.filter_by(
                payroll_run_id=self.run.id, status=BATCH_SCHEDULED
            ).count(),
            1,
        )

    def test_schedule_rejects_a_past_time(self):
        self._login("admin@chrisnat.local")
        when = (datetime.now(timezone.utc) - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M")
        resp = self.http.post(
            f"/distribution/run/{self.run.id}/schedule",
            data={"channel": "auto", "scheduled_for": when},
            follow_redirects=True,
        )
        self.assertIn("valid future date", resp.get_data(as_text=True))
        self.assertEqual(
            DistributionBatch.query.filter_by(payroll_run_id=self.run.id).count(), 0
        )


if __name__ == "__main__":
    unittest.main()
