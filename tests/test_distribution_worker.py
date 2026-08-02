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
from app.distribution.notify import notify_worker_stopped  # noqa: E402
from app.distribution.queue import (  # noqa: E402
    drain_once,
    enqueue_distribution,
    record_heartbeat,
    run_worker,
    run_worker_loop,
    safe_batch_error,
    worker_last_poll,
    worker_statuses,
)
from app.models import (  # noqa: E402
    WORKER_STATUS_RUNNING,
    WORKER_STATUS_STOPPED,
    DistributionWorkerHeartbeat,
    DomainEvent,
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
        self.operator = User.query.filter_by(email="admin@payrolla.com").first()

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

    def test_worker_identity_is_stable_across_restarts(self):
        """The heartbeat table grew one dead row per deploy because identity was
        the pod hostname. Identity is per-role now, so a redeploy (a new host,
        a new pid) updates the same row instead of adding one."""
        import socket
        from unittest import mock

        from app.distribution.queue import default_worker_name

        self.assertNotIn(socket.gethostname(), default_worker_name())

        name = default_worker_name()
        record_heartbeat(name)
        with mock.patch.object(socket, "gethostname", return_value="pod-after-deploy"):
            with mock.patch("os.getpid", return_value=99999):
                record_heartbeat(name)

        rows = DistributionWorkerHeartbeat.query.filter_by(worker_name=name).all()
        self.assertEqual(len(rows), 1, "a redeploy must not add a heartbeat row")
        self.assertEqual(rows[0].host, "pod-after-deploy")  # forensics still recorded
        self.assertEqual(rows[0].pid, 99999)

    def test_inline_and_standalone_workers_stay_distinct(self):
        from app.distribution.queue import INLINE_WORKER_NAME, default_worker_name

        self.assertNotEqual(INLINE_WORKER_NAME, default_worker_name())
        record_heartbeat(default_worker_name())
        record_heartbeat(INLINE_WORKER_NAME)
        self.assertEqual(DistributionWorkerHeartbeat.query.count(), 2)

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
        # The dashboard reports liveness as a count, not as a list of process rows —
        # the per-process detail is engineering-only (see _worker_fleet).
        self.assertEqual(stats["worker_fleet"]["live"], 1)
        self.assertEqual(stats["worker_fleet"]["known"], 1)
        self.assertIsNotNone(stats["last_processed_at"] or worker_last_poll())

    def test_dashboard_does_not_expose_worker_hostnames(self):
        # Regression guard for the Notifications/Monitor internals leak: the stats
        # payload backing the operator dashboard must carry no per-process rows.
        record_heartbeat("external-worker")
        self.assertNotIn("workers", collect_dashboard_stats())


class InternalsStayOutOfUserFacingTextTests(unittest.TestCase):
    """Phase 0, Task 0.4 — a raw driver exception must never reach a surface a
    business user reads: the in-app Notifications inbox, or the distribution status
    panel the *tenant* sees (which renders ``DistributionBatch.error``). Detail
    belongs in the application log. Written against the shape of the exception that
    actually leaked, so it guards the class of bug, not the one instance."""

    LEAKY = (
        '(psycopg2.errors.UndefinedTable) relation "distribution_worker_heartbeat" '
        "does not exist\nLINE 2: FROM distribution_worker_heartbeat\n"
        "[SQL: SELECT max(distribution_worker_heartbeat.last_poll_at) AS max_1]\n"
        "[parameters: {'worker': 'srv-d9cvkbgk1i2s73cm6phg-hibernate-596c77cc6f'}]"
    )
    FORBIDDEN = ("psycopg2", "UndefinedTable", "[SQL:", "parameters:", "srv-", "LINE 2")

    def setUp(self):
        self.app = create_app()
        self.app.config["TESTING"] = True
        self.ctx = self.app.app_context()
        self.ctx.push()

    def tearDown(self):
        db.session.remove()
        self.ctx.pop()

    def _assert_clean(self, text):
        for token in self.FORBIDDEN:
            self.assertNotIn(token, text, f"{token!r} leaked into user-facing text")

    def test_worker_stopped_notification_carries_no_exception_detail(self):
        notify_worker_stopped(self.LEAKY)
        db.session.commit()
        event = DomainEvent.query.filter_by(
            event_type="distribution.worker_stopped"
        ).one()
        self._assert_clean(event.summary)

    def test_batch_error_carries_no_exception_detail(self):
        self._assert_clean(safe_batch_error(RuntimeError(self.LEAKY)))


class WorkerCliTestCase(unittest.TestCase):
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
