"""Phase 3, Slice 3 — the retry system.

A failed delivery is retried automatically (bounded by DISTRIBUTION_MAX_ATTEMPTS,
with exponential backoff) until it succeeds or the limit is spent (a "final"
failure). A manual "resend failed" is the operator override and is NOT bounded by
the limit. Successful deliveries are never re-sent, and no attempt ever creates a
duplicate delivery row.
"""

import os
import unittest

os.environ["SKIP_DOTENV"] = "true"
os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["SEED_DEMO_DATA"] = "true"
os.environ["PERSISTENCE_REQUIRED"] = "false"

from app import create_app, db  # noqa: E402
from app.distribution.queue import process_due_retries  # noqa: E402
from app.distribution.service import distribute_run, retry_state  # noqa: E402
from app.models import (  # noqa: E402
    DELIVERY_FAILED,
    DELIVERY_SENT,
    AuditTrail,
    PayrollRun,
    PayslipDelivery,
)


class RetrySystemTestCase(unittest.TestCase):
    def setUp(self):
        self.app = create_app()
        self.app.config["TESTING"] = True
        # Deterministic retry policy: no backoff wait (so a scheduled retry is
        # immediately due) and a small cap.
        self.app.config["DISTRIBUTION_RETRY_BACKOFF_SECONDS"] = 0
        self.app.config["DISTRIBUTION_MAX_ATTEMPTS"] = 3
        self.ctx = self.app.app_context()
        self.ctx.push()
        self.run = PayrollRun.query.filter_by(status="Approved").first()
        self.assertIsNotNone(self.run, "expected a seeded Approved payroll run")
        self.item = self.run.items[0]

    def tearDown(self):
        db.session.remove()
        self.ctx.pop()

    def _strip_contacts(self, item):
        item.momo_number = None
        item.email = None
        if item.employee:
            item.employee.phone = None
            item.employee.momo_number = None
            item.employee.email = None
        db.session.commit()

    def _delivery(self):
        return PayslipDelivery.query.filter_by(payroll_item_id=self.item.id).first()

    # --- failure schedules a retry -----------------------------------------

    def test_failed_delivery_schedules_a_bounded_retry(self):
        self._strip_contacts(self.item)
        distribute_run(self.run, channel="sms")
        d = self._delivery()
        self.assertEqual(d.status, DELIVERY_FAILED)
        self.assertEqual(d.attempts, 1)
        self.assertIsNotNone(d.next_retry_at)  # scheduled for auto-retry
        state = retry_state(d)
        self.assertTrue(state["will_retry"])
        self.assertFalse(state["final"])
        self.assertEqual(state["remaining"], 2)  # 3 max - 1 used

    # --- automatic retry recovers ------------------------------------------

    def test_auto_retry_recovers_once_contact_is_fixed(self):
        self._strip_contacts(self.item)
        distribute_run(self.run, channel="sms")
        d = self._delivery()
        self.assertEqual(d.status, DELIVERY_FAILED)

        # Operator fixes the roster; the next sweep re-attempts and succeeds.
        self.item.momo_number = "0241234567"
        db.session.commit()
        processed = process_due_retries()
        self.assertIn(d, processed)
        db.session.refresh(d)
        self.assertEqual(d.status, DELIVERY_SENT)
        self.assertEqual(d.attempts, 2)
        self.assertIsNone(d.next_retry_at)  # cleared on success

    # --- retry limit is enforced -------------------------------------------

    def test_auto_retry_stops_at_the_limit(self):
        self.app.config["DISTRIBUTION_MAX_ATTEMPTS"] = 2
        self._strip_contacts(self.item)  # permanent failure
        distribute_run(self.run, channel="sms")
        d = self._delivery()
        self.assertEqual(d.attempts, 1)
        self.assertIsNotNone(d.next_retry_at)

        # Second (and final) automatic attempt exhausts the limit.
        process_due_retries()
        db.session.refresh(d)
        self.assertEqual(d.attempts, 2)
        self.assertIsNone(d.next_retry_at)  # no further retry scheduled
        self.assertTrue(retry_state(d)["final"])
        self.assertEqual(retry_state(d)["remaining"], 0)

        # A further sweep does nothing — the delivery is a final failure.
        self.assertEqual(process_due_retries(), [])
        db.session.refresh(d)
        self.assertEqual(d.attempts, 2)

    # --- never resend a success --------------------------------------------

    def test_retry_sweep_never_touches_sent_deliveries(self):
        distribute_run(self.run, channel="auto")
        sent = PayslipDelivery.query.filter_by(
            payroll_run_id=self.run.id, status=DELIVERY_SENT
        ).first()
        self.assertIsNotNone(sent)
        attempts_before = sent.attempts
        # Nothing is due (no failed-with-next_retry rows), so the sweep is a no-op.
        self.assertEqual(process_due_retries(), [])
        db.session.refresh(sent)
        self.assertEqual(sent.status, DELIVERY_SENT)
        self.assertEqual(sent.attempts, attempts_before)

    def test_retry_never_creates_a_duplicate_delivery(self):
        self._strip_contacts(self.item)
        distribute_run(self.run, channel="sms")
        count_before = PayslipDelivery.query.filter_by(
            payroll_item_id=self.item.id
        ).count()
        self.item.momo_number = "0241234567"
        db.session.commit()
        process_due_retries()
        count_after = PayslipDelivery.query.filter_by(
            payroll_item_id=self.item.id
        ).count()
        self.assertEqual(count_before, count_after)  # same row re-attempted

    # --- audit preserved ----------------------------------------------------

    def test_auto_retry_writes_a_system_audit_entry(self):
        self._strip_contacts(self.item)
        distribute_run(self.run, channel="sms")
        process_due_retries()
        entry = (
            AuditTrail.query.filter_by(action="Payslip delivery auto-retry")
            .order_by(AuditTrail.id.desc())
            .first()
        )
        self.assertIsNotNone(entry)
        self.assertEqual(entry.user_role, "system")  # ran in the worker, no user
        self.assertEqual(entry.related_record_id, self.run.id)

    # --- manual override beyond the limit ----------------------------------

    def test_manual_resend_overrides_an_exhausted_delivery(self):
        self.app.config["DISTRIBUTION_MAX_ATTEMPTS"] = 1
        self._strip_contacts(self.item)
        distribute_run(self.run, channel="sms")
        d = self._delivery()
        self.assertEqual(d.attempts, 1)
        self.assertIsNone(d.next_retry_at)  # already exhausted (limit 1)
        self.assertTrue(retry_state(d)["final"])

        # Operator fixes contact and manually resends — not bounded by the limit.
        self.item.momo_number = "0241234567"
        db.session.commit()
        distribute_run(self.run, channel="sms", only_failed=True)
        db.session.refresh(d)
        self.assertEqual(d.status, DELIVERY_SENT)
        self.assertEqual(d.attempts, 2)


class RetryVisibilityTestCase(unittest.TestCase):
    """The operator status page surfaces the retry position of each delivery."""

    def setUp(self):
        self.app = create_app()
        self.app.config["TESTING"] = True
        self.app.config["DISTRIBUTION_RETRY_BACKOFF_SECONDS"] = 0
        self.app.config["DISTRIBUTION_MAX_ATTEMPTS"] = 2
        self.http = self.app.test_client()
        self.ctx = self.app.app_context()
        self.ctx.push()
        self.run = PayrollRun.query.filter_by(status="Approved").first()
        self.item = self.run.items[0]
        self.http.post(
            "/login", data={"email": "admin@chrisnat.local", "password": "password123"}
        )

    def tearDown(self):
        db.session.remove()
        self.ctx.pop()

    def test_status_page_shows_retries_left_then_final(self):
        # Force a failure that still has a retry left.
        self.item.momo_number = None
        self.item.email = None
        if self.item.employee:
            self.item.employee.phone = None
            self.item.employee.momo_number = None
            self.item.employee.email = None
        db.session.commit()
        distribute_run(self.run, channel="sms")

        body = self.http.get(f"/distribution/run/{self.run.id}").get_data(as_text=True)
        self.assertIn("retr", body.lower())  # "1 retry left"

        # Exhaust it; now the page shows the final-failure marker.
        process_due_retries()
        body = self.http.get(f"/distribution/run/{self.run.id}").get_data(as_text=True)
        self.assertIn("final", body.lower())


if __name__ == "__main__":
    unittest.main()
