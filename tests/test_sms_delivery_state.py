"""SMS Phase 2: claim before send, and what each outcome becomes.

The property everything here protects: a payslip SMS is never sent twice by the
system on its own. A send is claimed with a conditional UPDATE before the
provider is called, so two workers cannot both send one row; a send that may
have gone out (`unknown`) is never re-sent by the retry sweep, by "Resend
failed" or by batch recovery; and only failures worth retrying are retried.
"""
import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

os.environ["SKIP_DOTENV"] = "true"
os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["SEED_DEMO_DATA"] = "true"
os.environ["PERSISTENCE_REQUIRED"] = "false"

from sqlalchemy.exc import IntegrityError  # noqa: E402

from app import create_app, db  # noqa: E402
from app.distribution.contacts import (  # noqa: E402
    SOURCE_INVALID,
    SOURCE_NONE,
    SOURCE_PAYROLL_MOMO,
    SOURCE_ROSTER_MOMO,
    SOURCE_ROSTER_PHONE,
    sms_contact,
)
from app.distribution.links import PayslipLink  # noqa: E402
from app.distribution.queue import (  # noqa: E402
    drain_once,
    process_all_queued,
    process_due_retries,
    reclaim_stale_batches,
)
from app.distribution.recovery import STALE_CLAIM_ERROR, recover_stale_claims  # noqa: E402
from app.distribution.service import (  # noqa: E402
    CLAIMABLE_FOR_RETRY,
    _attempt_send,
    claim_delivery,
    distribute_run,
)
from app.distribution.sasusync import SasuSyncSmsSender  # noqa: E402
from app.models import (  # noqa: E402
    BATCH_RUNNING,
    DistributionBatch,
    PayrollRun,
    PayslipDelivery,
    User,
)

OK_BODY = ('{"success": true, "balance": {"deducted": 1}, '
           '"data": {"status": "queued", "task_id": "task-1"}}')
SASUSYNC = dict(
    SMS_BACKEND="sasusync", SMS_SENDER_ID="Payrolla", SASUSYNC_API_KEY="key",
    SASUSYNC_BASE_URL="https://sms.sasusync.com", SASUSYNC_SANDBOX=True,
    PUBLIC_BASE_URL="https://pmvp-v1.onrender.com",
)


class _Obj:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def provider(*, status=200, body=OK_BODY, raises=None, only_for=None):
    """Patch SasuSync's HTTP call. ``only_for`` limits the scripted answer to one
    recipient; everyone else is accepted. Returns the patcher's mock."""
    def answer(url, *, headers=None, json=None, timeout=30):
        if only_for is None or json["recipients"] == [only_for]:
            if raises is not None:
                raise raises
            return status, body
        return 200, OK_BODY
    return mock.patch("app.distribution.sasusync._http_post", side_effect=answer)


class _SmsCase(unittest.TestCase):
    def setUp(self):
        self.app = create_app()
        self.app.config.update(
            TESTING=True, DISTRIBUTION_RETRY_BACKOFF_SECONDS=0,
            DISTRIBUTION_MAX_ATTEMPTS=3, **SASUSYNC,
        )
        self.ctx = self.app.app_context()
        self.ctx.push()
        self.run = PayrollRun.query.filter_by(status="Approved").first()
        self.assertIsNotNone(self.run, "expected a seeded Approved payroll run")
        self.items = list(self.run.items)
        self.item = self.items[0]
        self.number = sms_contact(self.item)[0]
        self.assertTrue(self.number, "the seeded worker should have a valid number")

    def tearDown(self):
        db.session.remove()
        self.ctx.pop()

    def delivery(self, item=None):
        return PayslipDelivery.query.filter_by(
            payroll_item_id=(item or self.item).id, channel="sms"
        ).one()

    def row(self, item, status, **extra):
        delivery = PayslipDelivery(payroll_item_id=item.id, payroll_run_id=self.run.id,
                                   channel="sms", status=status, attempts=1, **extra)
        db.session.add(delivery)
        db.session.commit()
        return delivery


class ClaimTests(_SmsCase):
    def test_two_claims_on_one_row_exactly_one_wins(self):
        delivery = self.row(self.item, "failed")
        outcomes = [claim_delivery(delivery.id, CLAIMABLE_FOR_RETRY) for _ in range(2)]
        self.assertEqual(outcomes, [True, False])
        db.session.refresh(delivery)
        self.assertEqual(delivery.status, "sending")
        self.assertEqual(delivery.attempts, 2)
        self.assertIsNotNone(delivery.claimed_at)

    def test_two_workers_holding_the_same_failed_row_send_it_once(self):
        """Both read the row as `failed` before either claims it: the real race."""
        from sqlalchemy.orm import Session

        delivery = self.row(self.item, "failed")
        other_worker = Session(db.engine)
        self.addCleanup(other_worker.close)
        worker_a = db.session.get(PayslipDelivery, delivery.id)
        worker_b = other_worker.get(PayslipDelivery, delivery.id)
        self.assertIsNot(worker_a, worker_b)
        self.assertEqual((worker_a.status, worker_b.status), ("failed", "failed"))
        with provider() as post:
            first = _attempt_send(worker_a, self.item, self.run, self.run.client_company,
                                  "sms", SasuSyncSmsSender(), 3, 0,
                                  claimable=CLAIMABLE_FOR_RETRY)
            second = _attempt_send(worker_b, self.item, self.run, self.run.client_company,
                                   "sms", SasuSyncSmsSender(), 3, 0,
                                   claimable=CLAIMABLE_FOR_RETRY)
        self.assertEqual((first, second), ("sent", None))
        self.assertEqual(post.call_count, 1)

    def test_the_unique_item_channel_constraint_holds(self):
        self.row(self.item, "failed")
        db.session.add(PayslipDelivery(payroll_item_id=self.item.id,
                                       payroll_run_id=self.run.id, channel="sms"))
        with self.assertRaises(IntegrityError):
            db.session.commit()
        db.session.rollback()


class TimeoutBecomesUnknownTests(_SmsCase):
    def test_a_timeout_is_unknown_and_nothing_resends_it(self):
        with provider(raises=TimeoutError("timed out"), only_for=self.number):
            distribute_run(self.run, channel="sms")
        delivery = self.delivery()
        self.assertEqual(delivery.status, "unknown")
        self.assertIsNone(delivery.next_retry_at)
        self.assertIn("may have been sent", delivery.error)

        operator = User.query.filter_by(email="admin@payrolla.com").first()
        with provider() as post:
            self.assertEqual(process_due_retries(), [])  # the sweep
            distribute_run(self.run, channel="sms", only_failed=True)  # Resend failed
            distribute_run(self.run, channel="sms")  # a re-run
            # Batch recovery: a batch stuck `running` is requeued and re-run.
            batch = DistributionBatch(
                payroll_run_id=self.run.id, client_company_id=self.run.client_company_id,
                channel="sms", status=BATCH_RUNNING, initiated_by_user_id=operator.id,
                total=len(self.items),
                started_at=datetime.now(timezone.utc) - timedelta(hours=2),
            )
            db.session.add(batch)
            db.session.commit()
            self.assertEqual(len(reclaim_stale_batches()), 1)
            process_all_queued()
            sent_to = [c.kwargs["json"]["recipients"] for c in post.call_args_list]
        self.assertNotIn([self.number], sent_to)
        db.session.refresh(delivery)
        self.assertEqual(delivery.status, "unknown")
        self.assertEqual(delivery.attempts, 1)


class StaleClaimTests(_SmsCase):
    def test_a_stale_sending_row_becomes_unknown_and_a_fresh_one_does_not(self):
        now = datetime.now(timezone.utc)
        stale = self.row(self.items[0], "sending", claimed_at=now - timedelta(minutes=11))
        fresh = self.row(self.items[1], "sending", claimed_at=now - timedelta(minutes=2))
        self.assertEqual(recover_stale_claims(), 1)
        db.session.refresh(stale)
        db.session.refresh(fresh)
        self.assertEqual(stale.status, "unknown")
        self.assertEqual(stale.error, STALE_CLAIM_ERROR)
        self.assertEqual(fresh.status, "sending")

    def test_the_worker_pass_runs_the_recovery(self):
        stale = self.row(self.item, "sending",
                         claimed_at=datetime.now(timezone.utc) - timedelta(minutes=30))
        drain_once()
        db.session.refresh(stale)
        self.assertEqual(stale.status, "unknown")


class RetryPolicyTests(_SmsCase):
    def test_429_retries_automatically_up_to_three_attempts_in_total(self):
        with provider(status=429, body='{"detail": "slow down"}', only_for=self.number):
            distribute_run(self.run, channel="sms")
            delivery = self.delivery()
            self.assertEqual((delivery.attempts, delivery.status), (1, "failed"))
            self.assertIsNotNone(delivery.next_retry_at)
            process_due_retries()
            db.session.refresh(delivery)
            self.assertEqual(delivery.attempts, 2)
            self.assertIsNotNone(delivery.next_retry_at)
            process_due_retries()
            db.session.refresh(delivery)
            self.assertEqual(delivery.attempts, 3)
            self.assertIsNone(delivery.next_retry_at)  # the third was the last
            self.assertEqual(process_due_retries(), [])
        db.session.refresh(delivery)
        self.assertEqual(delivery.attempts, 3)

    def test_a_refusal_never_retries_automatically(self):
        with provider(status=400, body='{"detail": "bad"}', only_for=self.number):
            distribute_run(self.run, channel="sms")
        delivery = self.delivery()
        self.assertEqual(delivery.status, "failed")
        self.assertIsNone(delivery.next_retry_at)
        self.assertEqual(process_due_retries(), [])

    def test_no_contact_and_an_invalid_number_never_retry(self):
        cases = {self.items[0]: (None, None, None), self.items[1]: ("0302123456", None, None)}
        for item, (phone, momo, row_momo) in cases.items():
            item.momo_number = row_momo
            item.employee.phone, item.employee.momo_number = phone, momo
        db.session.commit()
        with provider() as post:
            distribute_run(self.run, channel="sms")
            self.assertEqual(process_due_retries(), [])
        self.assertEqual(post.call_count, len(self.items) - 2)  # neither reached SasuSync
        no_contact, invalid = self.delivery(self.items[0]), self.delivery(self.items[1])
        self.assertIn("no contact", no_contact.error)
        self.assertIn("invalid number", invalid.error)
        for delivery in (no_contact, invalid):
            self.assertEqual(delivery.status, "failed")
            self.assertIsNone(delivery.next_retry_at)

    def test_a_manual_resend_still_reaches_a_permanent_failure(self):
        """Never automatic, but "Resend failed" is the operator's override."""
        with provider(status=400, body="{}", only_for=self.number):
            distribute_run(self.run, channel="sms")
        with provider():
            summary = distribute_run(self.run, channel="sms", only_failed=True)
        self.assertEqual(summary["sent"], 1)
        self.assertEqual(self.delivery().status, "sent")


class SkipRuleTests(_SmsCase):
    def test_a_rerun_skips_sent_sending_and_unknown(self):
        statuses = ("sent", "sending", "unknown")
        for item, status in zip(self.items, statuses):
            self.row(item, status)
        with provider() as post:
            summary = distribute_run(self.run, channel="sms")
        sent_to = [c.kwargs["json"]["recipients"][0] for c in post.call_args_list]
        for item in self.items[:len(statuses)]:
            self.assertNotIn(sms_contact(item)[0], sent_to)
        self.assertEqual(summary["skipped"], len(statuses))

    def test_a_cancelled_delivery_goes_out_on_the_next_send(self):
        self.row(self.item, "cancelled")
        with provider():
            distribute_run(self.run, channel="sms")
        self.assertEqual(self.delivery().status, "sent")


class WhatIsStoredTests(_SmsCase):
    def test_the_template_version_and_the_normalised_number_are_stored_not_the_body(self):
        self.item.employee.phone = None
        self.item.employee.momo_number = "241234567"  # Excel dropped the 0
        db.session.commit()
        with provider() as post:
            distribute_run(self.run, channel="sms")
        body = next(c.kwargs["json"]["message"] for c in post.call_args_list
                    if c.kwargs["json"]["recipients"] == ["233241234567"])
        delivery = self.delivery()
        self.assertEqual(delivery.template_version, "sms-link-v1")
        self.assertEqual(delivery.recipient, "233241234567")
        self.assertEqual((delivery.units, delivery.provider_message_id), (1, "task-1"))
        code = body.rsplit("/s/", 1)[1]
        stored = [str(getattr(delivery, c.name)) for c in PayslipDelivery.__table__.columns]
        for link in PayslipLink.query.all():
            stored += [str(getattr(link, c.name)) for c in PayslipLink.__table__.columns]
        self.assertFalse([v for v in stored if body in v or code in v])


class ContactSourceTests(unittest.TestCase):
    def _item(self, phone=None, momo=None, row_momo=None):
        employee = _Obj(phone=phone, momo_number=momo)
        return _Obj(employee=employee, momo_number=row_momo)

    def test_candidates_in_order_skipping_invalid_ones(self):
        self.assertEqual(sms_contact(self._item("0241234567", "0551234567")),
                         ("233241234567", SOURCE_ROSTER_PHONE))
        self.assertEqual(sms_contact(self._item("0302123456", "551234567")),
                         ("233551234567", SOURCE_ROSTER_MOMO))
        self.assertEqual(sms_contact(self._item(None, "12345", "0201234567")),
                         ("233201234567", SOURCE_PAYROLL_MOMO))

    def test_invalid_only_when_a_number_existed(self):
        self.assertEqual(sms_contact(self._item("0302123456", "0231234567")),
                         (None, SOURCE_INVALID))
        self.assertEqual(sms_contact(self._item(None, "  ", None)), (None, SOURCE_NONE))


if __name__ == "__main__":
    unittest.main()
