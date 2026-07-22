"""Phase 3, Slice 1 — the payslip distribution queue.

Sending no longer runs distribute_run() inline in the request: it enqueues a
DistributionBatch (queued), and a worker (claim_next_batch + process_batch, or
the polling run_worker_loop) claims and runs it. These tests exercise the queue
module directly — the HTTP-level "does sending queue instead of block" behaviour
is covered in test_client_distribution.py and test_bulk_actions.py.
"""

import os
import unittest

os.environ["SKIP_DOTENV"] = "true"
os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["SEED_DEMO_DATA"] = "true"
os.environ["PERSISTENCE_REQUIRED"] = "false"

from app import create_app, db  # noqa: E402
from app.distribution.queue import (  # noqa: E402
    claim_next_batch,
    enqueue_distribution,
    process_all_queued,
    process_batch,
)
from app.models import (  # noqa: E402
    BATCH_COMPLETED,
    BATCH_QUEUED,
    BATCH_RUNNING,
    DistributionBatch,
    Notification,
    PayrollRun,
    PayslipDelivery,
    User,
)


class DistributionQueueTestCase(unittest.TestCase):
    def setUp(self):
        self.app = create_app()
        self.app.config["TESTING"] = True
        self.ctx = self.app.app_context()
        self.ctx.push()
        self.run = PayrollRun.query.filter_by(status="Approved").first()
        self.assertIsNotNone(self.run, "expected a seeded Approved payroll run")
        self.operator = User.query.filter_by(email="admin@chrisnat.local").first()
        self.tenant_admin = User.query.filter_by(email="admin@msc.demo").first()

    def tearDown(self):
        db.session.remove()
        self.ctx.pop()

    def test_enqueue_is_a_no_op_while_a_batch_is_already_in_flight(self):
        first = enqueue_distribution(self.run, "auto", False, self.operator)
        self.assertFalse(first["already_in_progress"])
        second = enqueue_distribution(self.run, "sms", True, self.operator)
        self.assertTrue(second["already_in_progress"])
        self.assertEqual(second["batch_id"], first["batch_id"])
        self.assertEqual(
            DistributionBatch.query.filter_by(payroll_run_id=self.run.id).count(), 1
        )

        # Once the in-flight batch finishes, a new enqueue creates a fresh one.
        process_all_queued()
        third = enqueue_distribution(self.run, "auto", False, self.operator)
        self.assertFalse(third["already_in_progress"])
        self.assertNotEqual(third["batch_id"], first["batch_id"])

    def test_enqueue_creates_queued_batch(self):
        summary = enqueue_distribution(self.run, "auto", False, self.operator)
        batch = db.session.get(DistributionBatch, summary["batch_id"])
        self.assertEqual(batch.status, BATCH_QUEUED)
        self.assertEqual(batch.payroll_run_id, self.run.id)
        self.assertEqual(batch.client_company_id, self.run.client_company_id)
        self.assertEqual(batch.total, len(self.run.items))
        self.assertEqual(batch.initiated_by_user_id, self.operator.id)
        # Nothing has been sent yet — enqueueing must not touch PayslipDelivery.
        self.assertEqual(
            PayslipDelivery.query.filter_by(payroll_run_id=self.run.id).count(), 0
        )

    def test_claim_next_batch_returns_none_when_empty(self):
        self.assertIsNone(claim_next_batch())

    def test_claim_next_batch_claims_oldest_first_and_marks_running(self):
        other_run = PayrollRun.query.filter(PayrollRun.id != self.run.id).first()
        first = enqueue_distribution(self.run, "auto", False, self.operator)
        if other_run is not None:
            enqueue_distribution(other_run, "auto", False, self.operator)

        claimed = claim_next_batch()
        self.assertEqual(claimed.id, first["batch_id"])
        self.assertEqual(claimed.status, BATCH_RUNNING)
        self.assertIsNotNone(claimed.started_at)
        # Claiming a batch never leaves a second one dangling in "running".
        self.assertEqual(
            DistributionBatch.query.filter_by(status=BATCH_RUNNING).count(), 1
        )

    def test_process_batch_delivers_payslips_like_distribute_run(self):
        summary = enqueue_distribution(self.run, "auto", False, self.operator)
        batch = claim_next_batch()
        self.assertEqual(batch.id, summary["batch_id"])

        processed = process_batch(batch)
        self.assertEqual(processed.status, BATCH_COMPLETED)
        self.assertEqual(processed.sent_count, len(self.run.items))
        self.assertEqual(processed.failed_count, 0)
        self.assertIsNotNone(processed.finished_at)
        rows = PayslipDelivery.query.filter_by(payroll_run_id=self.run.id).all()
        self.assertEqual(len(rows), len(self.run.items))
        self.assertTrue(all(r.status == "sent" for r in rows))

    def test_process_all_queued_drains_the_whole_queue(self):
        other_run = PayrollRun.query.filter(PayrollRun.id != self.run.id).first()
        enqueue_distribution(self.run, "auto", False, self.operator)
        if other_run is not None:
            enqueue_distribution(other_run, "auto", False, self.operator)

        processed = process_all_queued()
        self.assertGreaterEqual(len(processed), 1)
        self.assertTrue(all(b.status == BATCH_COMPLETED for b in processed))
        self.assertEqual(
            DistributionBatch.query.filter_by(status=BATCH_QUEUED).count(), 0
        )
        # A second drain is a no-op — nothing left to claim.
        self.assertEqual(process_all_queued(), [])

    def test_client_initiated_batch_notifies_platform_admins(self):
        self.assertIsNotNone(self.tenant_admin, "expected seeded admin@msc.demo")
        msc_run = PayrollRun.query.filter_by(
            client_company_id=self.tenant_admin.client_company_id
        ).first()
        self.assertIsNotNone(msc_run)
        before = Notification.query.count()

        enqueue_distribution(msc_run, "auto", False, self.tenant_admin)
        process_all_queued()

        self.assertGreater(Notification.query.count(), before)

    def test_operator_initiated_batch_does_not_notify(self):
        before = Notification.query.count()
        enqueue_distribution(self.run, "auto", False, self.operator)
        process_all_queued()
        self.assertEqual(Notification.query.count(), before)


if __name__ == "__main__":
    unittest.main()
