"""Phase 3, Slice 5 — batch cancellation + the Cancelled state.

An operator (or client_admin) can cancel a run's not-yet-sent distribution: a
queued batch before the worker claims it, and any pending automatic retries. A
running batch is never cancelled mid-flight, already-sent deliveries are never
touched, and cancelled deliveries leave the retry pool.
"""

import os
import unittest

os.environ["SKIP_DOTENV"] = "true"
os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["SEED_DEMO_DATA"] = "true"
os.environ["PERSISTENCE_REQUIRED"] = "false"

from app import create_app, db  # noqa: E402
from app.distribution.queue import (  # noqa: E402
    cancel_distribution,
    enqueue_distribution,
    process_all_queued,
    process_due_retries,
)
from app.distribution.service import distribute_run  # noqa: E402
from app.models import (  # noqa: E402
    BATCH_CANCELLED,
    BATCH_QUEUED,
    BATCH_RUNNING,
    DELIVERY_CANCELLED,
    DELIVERY_SENT,
    AuditTrail,
    DistributionBatch,
    DomainEvent,
    PayrollRun,
    PayslipDelivery,
    User,
)


class CancelDistributionTestCase(unittest.TestCase):
    def setUp(self):
        self.app = create_app()
        self.app.config["TESTING"] = True
        self.app.config["DISTRIBUTION_RETRY_BACKOFF_SECONDS"] = 0
        self.app.config["DISTRIBUTION_MAX_ATTEMPTS"] = 3
        self.ctx = self.app.app_context()
        self.ctx.push()
        self.run = PayrollRun.query.filter_by(status="Approved").first()
        self.item = self.run.items[0]
        self.operator = User.query.filter_by(email="admin@chrisnat.local").first()

    def tearDown(self):
        db.session.remove()
        self.ctx.pop()

    def _strip(self, item):
        item.momo_number = None
        item.email = None
        if item.employee:
            item.employee.phone = None
            item.employee.momo_number = None
            item.employee.email = None
        db.session.commit()

    def test_cancel_a_queued_batch_stops_it_being_processed(self):
        summary = enqueue_distribution(self.run, "auto", False, self.operator)
        batch = db.session.get(DistributionBatch, summary["batch_id"])
        self.assertEqual(batch.status, BATCH_QUEUED)

        result = cancel_distribution(self.run, self.operator)
        self.assertTrue(result["cancelled_batch"])
        self.assertFalse(result["blocked"])
        db.session.refresh(batch)
        self.assertEqual(batch.status, BATCH_CANCELLED)

        # The worker never claims a cancelled batch — nothing gets sent.
        self.assertEqual(process_all_queued(), [])
        self.assertEqual(
            PayslipDelivery.query.filter_by(payroll_run_id=self.run.id).count(), 0
        )

    def test_cancel_stops_pending_retries_but_not_sent_deliveries(self):
        self._strip(self.item)  # this one will fail and schedule a retry
        distribute_run(self.run, channel="sms")
        failed = PayslipDelivery.query.filter_by(payroll_item_id=self.item.id).first()
        self.assertIsNotNone(failed.next_retry_at)
        sent = PayslipDelivery.query.filter_by(
            payroll_run_id=self.run.id, status=DELIVERY_SENT
        ).first()
        self.assertIsNotNone(sent)

        result = cancel_distribution(self.run, self.operator)
        self.assertEqual(result["cancelled_retries"], 1)

        db.session.refresh(failed)
        self.assertEqual(failed.status, DELIVERY_CANCELLED)
        self.assertIsNone(failed.next_retry_at)
        # The retry sweep now skips it entirely.
        self.assertEqual(process_due_retries(), [])

        # A previously-sent delivery is untouched.
        db.session.refresh(sent)
        self.assertEqual(sent.status, DELIVERY_SENT)

    def test_running_batch_is_never_cancelled(self):
        summary = enqueue_distribution(self.run, "auto", False, self.operator)
        batch = db.session.get(DistributionBatch, summary["batch_id"])
        batch.status = BATCH_RUNNING  # simulate the worker having claimed it
        db.session.commit()

        result = cancel_distribution(self.run, self.operator)
        self.assertTrue(result["blocked"])
        self.assertFalse(result["cancelled_batch"])
        db.session.refresh(batch)
        self.assertEqual(batch.status, BATCH_RUNNING)

    def test_cancel_writes_audit_and_domain_event(self):
        enqueue_distribution(self.run, "auto", False, self.operator)
        cancel_distribution(self.run, self.operator)
        self.assertIsNotNone(
            AuditTrail.query.filter_by(action="Distribution cancelled").first()
        )
        self.assertIsNotNone(
            DomainEvent.query.filter_by(event_type="distribution.cancelled").first()
        )

    def test_cancel_with_nothing_to_cancel_is_a_noop(self):
        result = cancel_distribution(self.run, self.operator)
        self.assertFalse(result["cancelled_batch"])
        self.assertEqual(result["cancelled_retries"], 0)
        self.assertFalse(result["blocked"])


class CancelRouteTestCase(unittest.TestCase):
    def setUp(self):
        self.app = create_app()
        self.app.config["TESTING"] = True
        self.http = self.app.test_client()
        self.ctx = self.app.app_context()
        self.ctx.push()
        self.run = PayrollRun.query.filter_by(status="Approved").first()
        self.operator = User.query.filter_by(email="admin@chrisnat.local").first()

    def tearDown(self):
        db.session.remove()
        self.ctx.pop()

    def _login(self, email):
        self.http.post("/login", data={"email": email, "password": "password123"})

    def test_operator_cancel_route_cancels_queued_batch(self):
        self._login("admin@chrisnat.local")
        enqueue_distribution(self.run, "auto", False, self.operator)
        resp = self.http.post(
            f"/distribution/run/{self.run.id}/cancel", follow_redirects=True
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(
            DistributionBatch.query.filter_by(
                payroll_run_id=self.run.id, status=BATCH_CANCELLED
            ).count(),
            1,
        )

    def test_cancel_requires_operator_role(self):
        self._login("operations@chrisnat.local")  # not in PAYROLL_ROLES
        enqueue_distribution(self.run, "auto", False, self.operator)
        resp = self.http.post(
            f"/distribution/run/{self.run.id}/cancel", follow_redirects=True
        )
        self.assertIn("do not have permission", resp.get_data(as_text=True))
        self.assertEqual(
            DistributionBatch.query.filter_by(
                payroll_run_id=self.run.id, status=BATCH_QUEUED
            ).count(),
            1,
        )


if __name__ == "__main__":
    unittest.main()
