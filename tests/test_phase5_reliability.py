"""Phase 5 — reliability fixes.

Covers the three behaviour bugs closed in Workstream A:
  * A1 — a worker crash mid-distribution never causes a duplicate send, because
    each delivery is persisted as it is sent (the skip-if-sent guard then makes a
    re-run idempotent).
  * A2 — a batch left `running` by a dead worker is reclaimed (requeued) and, past
    a cap, failed rather than stuck forever.
  * A3 — a failure during a raw-engine confirm rolls the run back, so a Draft run
    with zero items can never be orphaned.
"""
import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

os.environ["SKIP_DOTENV"] = "true"
os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["SEED_DEMO_DATA"] = "true"
os.environ["PERSISTENCE_REQUIRED"] = "false"

from app import create_app, db  # noqa: E402
from app.distribution.channels import SendResult  # noqa: E402
from app.distribution.queue import (  # noqa: E402
    claim_next_batch,
    drain_once,
    enqueue_distribution,
    reclaim_stale_batches,
)
from app.distribution.service import distribute_run  # noqa: E402
from app.models import (  # noqa: E402
    BATCH_COMPLETED,
    BATCH_FAILED,
    BATCH_QUEUED,
    BATCH_RUNNING,
    DELIVERY_SENT,
    ClientCompany,
    DistributionBatch,
    PayrollRun,
    PayslipDelivery,
    User,
)
from app.raw_engine.store import write_payroll_items  # noqa: E402


class _CrashingSender:
    """Succeeds until its ``crash_on``-th call, then raises — a worker crash
    mid-loop (an unhandled exception), not a handled send failure."""

    provider = "test-crash"

    def __init__(self, crash_on):
        self.crash_on = crash_on
        self.calls = 0

    def send(self, message):
        self.calls += 1
        if self.calls >= self.crash_on:
            raise RuntimeError("simulated worker crash mid-send")
        return SendResult(ok=True, provider=self.provider)


class DistributeCrashIdempotencyTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app()
        self.app.config["TESTING"] = True
        self.ctx = self.app.app_context()
        self.ctx.push()
        self.run = PayrollRun.query.filter_by(status="Approved").first()

    def tearDown(self):
        db.session.remove()
        self.ctx.pop()

    def test_crash_midway_persists_sent_and_never_duplicates_on_rerun(self):
        items = list(self.run.items)
        if len(items) < 2:
            self.skipTest("need a seeded run with >= 2 items")
        # Guarantee an email contact so every item would send over the email channel.
        for item in items:
            item.email = f"worker{item.id}@example.com"
        db.session.commit()

        # Crash on the 2nd send: item[0] is sent + committed, item[1] blows up.
        with mock.patch(
            "app.distribution.service.get_sender", return_value=_CrashingSender(crash_on=2)
        ):
            with self.assertRaises(RuntimeError):
                distribute_run(self.run, channel="email")
        # A real worker (process_batch) rolls back the failed transaction.
        db.session.rollback()

        rows = PayslipDelivery.query.filter_by(payroll_run_id=self.run.id).all()
        self.assertEqual(len(rows), 1, "only the already-sent delivery should persist")
        self.assertEqual(rows[0].status, DELIVERY_SENT)
        self.assertEqual(rows[0].attempts, 1)
        sent_item_id = rows[0].payroll_item_id

        # Re-run with a healthy sender: the already-sent item must be SKIPPED, not
        # resent (no duplicate), and the rest complete.
        summary = distribute_run(self.run, channel="email")
        self.assertGreaterEqual(summary["skipped"], 1)

        all_rows = PayslipDelivery.query.filter_by(payroll_run_id=self.run.id).all()
        # Exactly one delivery per item — no duplicate for the crash-time item.
        self.assertEqual(len(all_rows), len(items))
        by_item = {}
        for r in all_rows:
            by_item.setdefault(r.payroll_item_id, []).append(r)
        self.assertTrue(all(len(v) == 1 for v in by_item.values()))
        # The originally-sent item was not attempted again.
        self.assertEqual(by_item[sent_item_id][0].attempts, 1)
        self.assertTrue(all(r.status == DELIVERY_SENT for r in all_rows))


class StaleBatchReclaimTests(unittest.TestCase):
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

    def _claim_and_age(self, worker="dead-worker", age_seconds=3600, reclaim_count=0):
        enqueue_distribution(self.run, "auto", False, self.operator)
        batch = claim_next_batch(worker)
        self.assertEqual(batch.status, BATCH_RUNNING)
        self.assertEqual(batch.claimed_by_worker, worker)
        batch.started_at = datetime.now(timezone.utc) - timedelta(seconds=age_seconds)
        batch.reclaim_count = reclaim_count
        db.session.commit()
        return batch

    def test_fresh_running_batch_is_not_reclaimed(self):
        enqueue_distribution(self.run, "auto", False, self.operator)
        batch = claim_next_batch("live-worker")  # started_at = now
        self.assertEqual(reclaim_stale_batches(), [])
        self.assertEqual(db.session.get(DistributionBatch, batch.id).status, BATCH_RUNNING)

    def test_stale_batch_is_requeued_then_completes_without_duplicates(self):
        batch = self._claim_and_age()
        acted = reclaim_stale_batches()
        self.assertEqual([b.id for b in acted], [batch.id])
        batch = db.session.get(DistributionBatch, batch.id)
        self.assertEqual(batch.status, BATCH_QUEUED)
        self.assertEqual(batch.reclaim_count, 1)
        self.assertIsNone(batch.started_at)

        # A subsequent drain processes the requeued batch to completion.
        drain_once()
        batch = db.session.get(DistributionBatch, batch.id)
        self.assertEqual(batch.status, BATCH_COMPLETED)
        rows = PayslipDelivery.query.filter_by(payroll_run_id=self.run.id).all()
        self.assertEqual(len(rows), len(list(self.run.items)))  # one per item, no dupes

    def test_poison_batch_fails_after_max_reclaims(self):
        cap = self.app.config["DISTRIBUTION_BATCH_MAX_RECLAIMS"]
        batch = self._claim_and_age(reclaim_count=cap)
        acted = reclaim_stale_batches()
        self.assertEqual([b.id for b in acted], [batch.id])
        batch = db.session.get(DistributionBatch, batch.id)
        self.assertEqual(batch.status, BATCH_FAILED)
        self.assertIn("Abandoned", batch.error or "")


class ConfirmTransactionAtomicityTests(unittest.TestCase):
    """A3 mechanism: the store functions defer their commit so the confirm route
    can persist the run and its items in one transaction — a failure rolls the run
    back, never leaving an orphaned Draft run."""

    def setUp(self):
        self.app = create_app()
        self.app.config["TESTING"] = True
        self.ctx = self.app.app_context()
        self.ctx.push()
        company = ClientCompany(name="Atomicity Co", status="Active")
        db.session.add(company)
        db.session.commit()
        self.cid = company.id

    def tearDown(self):
        db.session.remove()
        self.ctx.pop()

    def _make_run(self):
        run = PayrollRun(
            client_company_id=self.cid, month="January", year=2026,
            status="Draft", upload_type="raw",
        )
        db.session.add(run)
        db.session.flush()
        return run

    def test_deferred_commit_persists_run_and_items_together(self):
        before = PayrollRun.query.count()
        run = self._make_run()
        write_payroll_items(run, {}, commit=False)  # flush only, no commit
        db.session.commit()  # the caller (confirm) owns the single commit
        self.assertEqual(PayrollRun.query.count(), before + 1)
        self.assertEqual(db.session.get(PayrollRun, run.id).total_workers, 0)

    def test_failure_after_run_created_leaves_no_orphan(self):
        before = PayrollRun.query.count()
        with self.assertRaises(RuntimeError):
            try:
                run = self._make_run()
                write_payroll_items(run, {}, commit=False)  # deferred — not committed
                raise RuntimeError("boom after items, before the confirm commit")
            except Exception:
                db.session.rollback()
                raise
        # The run was never committed — no orphaned Draft run with zero items.
        self.assertEqual(PayrollRun.query.count(), before)


if __name__ == "__main__":
    unittest.main()
