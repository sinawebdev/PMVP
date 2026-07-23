"""Phase 4, Slice 1 — worker deployment hardening.

A DB-persisted heartbeat (so an external worker process is visible on the
dashboard), a graceful stop that marks the worker stopped, a --once cron drain,
and the CLI command wiring.
"""

import os
import unittest

os.environ["SKIP_DOTENV"] = "true"
os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["SEED_DEMO_DATA"] = "true"
os.environ["PERSISTENCE_REQUIRED"] = "false"

from app import create_app, db  # noqa: E402
from app.distribution.dashboard import collect_dashboard_stats  # noqa: E402
from app.distribution.queue import (  # noqa: E402
    drain_once,
    enqueue_distribution,
    record_heartbeat,
    run_worker,
    run_worker_loop,
    worker_last_poll,
    worker_statuses,
)
from app.models import (  # noqa: E402
    WORKER_STATUS_RUNNING,
    WORKER_STATUS_STOPPED,
    DistributionWorkerHeartbeat,
    PayrollRun,
    PayslipDelivery,
    User,
)


class _StopAfter:
    def __init__(self, n):
        self.n = n
        self.calls = 0

    def is_set(self):
        self.calls += 1
        return self.calls > self.n

    def wait(self, _timeout):
        pass


class WorkerHardeningTestCase(unittest.TestCase):
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

    def test_heartbeat_is_upserted_not_duplicated(self):
        record_heartbeat("worker-a")
        first = DistributionWorkerHeartbeat.query.filter_by(worker_name="worker-a").one()
        first_poll = first.last_poll_at
        record_heartbeat("worker-a")  # same name -> updates the same row
        rows = DistributionWorkerHeartbeat.query.filter_by(worker_name="worker-a").all()
        self.assertEqual(len(rows), 1)
        self.assertGreaterEqual(rows[0].last_poll_at, first_poll)
        self.assertEqual(rows[0].status, WORKER_STATUS_RUNNING)

    def test_worker_last_poll_is_max_across_workers(self):
        self.assertIsNone(worker_last_poll())
        record_heartbeat("w1")
        record_heartbeat("w2")
        self.assertIsNotNone(worker_last_poll())
        self.assertEqual(len(worker_statuses()), 2)

    def test_loop_records_a_running_heartbeat_each_poll(self):
        run_worker_loop(poll_interval=0, stop_event=_StopAfter(1), worker_name="loop-w")
        hb = DistributionWorkerHeartbeat.query.filter_by(worker_name="loop-w").one()
        self.assertEqual(hb.status, WORKER_STATUS_RUNNING)

    def test_run_worker_marks_stopped_on_clean_exit(self):
        run_worker(poll_interval=0, stop_event=_StopAfter(1), worker_name="graceful-w")
        hb = DistributionWorkerHeartbeat.query.filter_by(worker_name="graceful-w").one()
        self.assertEqual(hb.status, WORKER_STATUS_STOPPED)

    def test_drain_once_processes_the_queue(self):
        enqueue_distribution(self.run, "auto", False, self.operator)
        self.assertTrue(drain_once())
        self.assertGreater(
            PayslipDelivery.query.filter_by(payroll_run_id=self.run.id).count(), 0
        )
        # A second drain has nothing to do.
        self.assertFalse(drain_once())

    def test_dashboard_sees_external_worker_via_heartbeat(self):
        # No inline worker running in the test, but an external worker's heartbeat
        # makes the dashboard's worker health live (not blind).
        record_heartbeat("external-worker")
        stats = collect_dashboard_stats()
        self.assertTrue(any(w.worker_name == "external-worker" for w in stats["workers"]))
        self.assertIsNotNone(stats["last_processed_at"] or worker_last_poll())


class WorkerCliTestCase(unittest.TestCase):
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

    def test_worker_once_cli_drains_and_exits(self):
        enqueue_distribution(self.run, "auto", False, self.operator)
        runner = self.app.test_cli_runner()
        result = runner.invoke(args=["distribution-worker", "--once"])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("drained once", result.output)
        self.assertGreater(
            PayslipDelivery.query.filter_by(payroll_run_id=self.run.id).count(), 0
        )


if __name__ == "__main__":
    unittest.main()
