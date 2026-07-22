"""Phase 3, Slice 8 — operator notifications.

Distribution lifecycle events fan out through the existing notification system
(app.events.record_event) to the initiating operator and/or platform admins:
completion (full/partial/failed), high failure rate, batch failure, retry
exhaustion, scheduled start, and worker stop.
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
    enqueue_distribution,
    process_all_queued,
    process_due_retries,
)
from app.distribution.service import distribute_run  # noqa: E402
from app.models import (  # noqa: E402
    BATCH_SCHEDULED,
    DistributionBatch,
    DomainEvent,
    Notification,
    PayrollRun,
    User,
)


class NotificationTestCase(unittest.TestCase):
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

    def _notes(self, user, event_prefix):
        return (
            Notification.query.join(DomainEvent, Notification.event_id == DomainEvent.id)
            .filter(
                Notification.user_id == user.id,
                DomainEvent.event_type.like(f"{event_prefix}%"),
            )
            .all()
        )

    def _strip(self, item):
        item.momo_number = None
        item.email = None
        if item.employee:
            item.employee.phone = None
            item.employee.momo_number = None
            item.employee.email = None
        db.session.commit()

    def test_completion_notifies_the_initiator(self):
        enqueue_distribution(self.run, "auto", False, self.operator)
        process_all_queued()
        self.assertTrue(
            DomainEvent.query.filter_by(event_type="distribution.completed").count() >= 1
        )
        self.assertTrue(self._notes(self.operator, "distribution.completed"))

    def test_high_failure_rate_alerts_platform_admins(self):
        # Strip every item's contact so the whole run fails -> 100% failure rate.
        for item in self.run.items:
            self._strip(item)
        enqueue_distribution(self.run, "sms", False, self.operator)
        process_all_queued()
        # All-failed => distribution.failed, and (>= threshold) platform admins notified.
        event = DomainEvent.query.filter_by(event_type="distribution.failed").first()
        self.assertIsNotNone(event)
        admins = [
            u for u in User.query.filter_by(client_company_id=None).all()
            if u.role in ("admin", "md", "chrisnat_admin")
        ]
        self.assertTrue(
            any(self._notes(a, "distribution.failed") for a in admins)
        )

    def test_retry_exhaustion_notifies(self):
        item = self.run.items[0]
        self._strip(item)  # permanent failure, max_attempts=2
        enqueue_distribution(self.run, "sms", False, self.operator)
        process_all_queued()  # attempt 1 -> scheduled retry
        process_due_retries()  # attempt 2 -> exhausted
        self.assertIsNotNone(
            DomainEvent.query.filter_by(event_type="distribution.retry_exhausted").first()
        )

    def test_scheduled_start_notifies_initiator(self):
        summary = enqueue_distribution(
            self.run, "auto", False, self.operator,
            scheduled_for=datetime.now(timezone.utc) + timedelta(minutes=30),
        )
        batch = db.session.get(DistributionBatch, summary["batch_id"])
        batch.scheduled_for = datetime.now(timezone.utc) - timedelta(minutes=1)
        db.session.commit()
        activate_due_scheduled()
        self.assertTrue(self._notes(self.operator, "distribution.scheduled_started"))

    def test_worker_stop_notifies_platform_admins(self):
        from app.distribution.notify import notify_worker_stopped

        notify_worker_stopped("boom")
        db.session.commit()
        self.assertIsNotNone(
            DomainEvent.query.filter_by(event_type="distribution.worker_stopped").first()
        )


if __name__ == "__main__":
    unittest.main()
