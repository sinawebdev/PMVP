"""Phase 3 — end-to-end worker-loop integration.

The per-slice suites call the worker's steps (process_all_queued,
process_due_retries, activate_due_scheduled) directly. These tests drive the real
run_worker_loop / run_worker entrypoints so the whole worker lifecycle —
activate scheduled -> claim -> run -> retry, plus crash notification — is covered
as one flow. The loop is run synchronously (one iteration at a time) to stay
compatible with in-memory SQLite's per-connection isolation.
"""

import os
import unittest
from datetime import datetime, timedelta, timezone

os.environ["SKIP_DOTENV"] = "true"
os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["SEED_DEMO_DATA"] = "true"
os.environ["PERSISTENCE_REQUIRED"] = "false"

from app import create_app, db  # noqa: E402
from app.distribution import queue as queue_mod  # noqa: E402
from app.distribution.queue import enqueue_distribution, run_worker, run_worker_loop  # noqa: E402
from app.models import (  # noqa: E402
    BATCH_COMPLETED,
    BATCH_SCHEDULED,
    DomainEvent,
    PayrollRun,
    PayslipDelivery,
    User,
)


class _StopAfter:
    """A stop_event that lets the worker loop run exactly `n` iterations, in the
    calling thread (no real sleeping, no second DB connection)."""

    def __init__(self, n):
        self.n = n
        self.calls = 0

    def is_set(self):
        self.calls += 1
        return self.calls > self.n

    def wait(self, _timeout):
        pass


class WorkerLoopIntegrationTestCase(unittest.TestCase):
    def setUp(self):
        self.app = create_app()
        self.app.config["TESTING"] = True
        self.app.config["DISTRIBUTION_RETRY_BACKOFF_SECONDS"] = 0
        self.app.config["DISTRIBUTION_MAX_ATTEMPTS"] = 2
        self.ctx = self.app.app_context()
        self.ctx.push()
        self.run = PayrollRun.query.filter_by(status="Approved").first()
        self.operator = User.query.filter_by(email="admin@chrisnat.local").first()

    def tearDown(self):
        db.session.remove()
        self.ctx.pop()

    def test_loop_processes_a_queued_batch(self):
        enqueue_distribution(self.run, "auto", False, self.operator)
        run_worker_loop(poll_interval=0, stop_event=_StopAfter(1))
        batches = self.run  # readability
        from app.models import DistributionBatch

        batch = DistributionBatch.query.filter_by(payroll_run_id=self.run.id).first()
        self.assertEqual(batch.status, BATCH_COMPLETED)
        self.assertGreater(
            PayslipDelivery.query.filter_by(payroll_run_id=self.run.id).count(), 0
        )

    def test_loop_activates_and_runs_a_due_scheduled_batch(self):
        from app.models import DistributionBatch

        summary = enqueue_distribution(
            self.run, "auto", False, self.operator,
            scheduled_for=datetime.now(timezone.utc) + timedelta(minutes=30),
        )
        batch = db.session.get(DistributionBatch, summary["batch_id"])
        self.assertEqual(batch.status, BATCH_SCHEDULED)
        # Its time arrives.
        batch.scheduled_for = datetime.now(timezone.utc) - timedelta(minutes=1)
        db.session.commit()

        # One loop pass: activate (scheduled -> queued) then run (queued -> completed).
        run_worker_loop(poll_interval=0, stop_event=_StopAfter(1))
        db.session.refresh(batch)
        self.assertEqual(batch.status, BATCH_COMPLETED)

    def test_loop_retries_a_failed_delivery_to_recovery(self):
        # Backoff 0 means a scheduled retry is due within the same loop pass, so a
        # single pass can consume more than one attempt; a higher cap keeps the
        # delivery retryable (not yet exhausted) after the first pass.
        self.app.config["DISTRIBUTION_MAX_ATTEMPTS"] = 5
        item = self.run.items[0]
        item.momo_number = None
        item.email = None
        if item.employee:
            item.employee.phone = None
            item.employee.momo_number = None
            item.employee.email = None
        db.session.commit()

        enqueue_distribution(self.run, "sms", False, self.operator)
        run_worker_loop(poll_interval=0, stop_event=_StopAfter(1))  # send: item fails
        d = PayslipDelivery.query.filter_by(payroll_item_id=item.id).first()
        self.assertEqual(d.status, "failed")
        self.assertIsNotNone(d.next_retry_at)

        # Operator fixes the roster; next loop pass runs the due retry.
        item.momo_number = "0241234567"
        db.session.commit()
        run_worker_loop(poll_interval=0, stop_event=_StopAfter(1))
        db.session.refresh(d)
        self.assertEqual(d.status, "sent")

    def test_run_worker_notifies_on_unexpected_crash(self):
        original = queue_mod.process_all_queued

        def boom():
            raise RuntimeError("kaboom")

        queue_mod.process_all_queued = boom
        try:
            with self.assertRaises(RuntimeError):
                run_worker(poll_interval=0, stop_event=_StopAfter(1))
        finally:
            queue_mod.process_all_queued = original

        self.assertIsNotNone(
            DomainEvent.query.filter_by(event_type="distribution.worker_stopped").first()
        )


if __name__ == "__main__":
    unittest.main()
