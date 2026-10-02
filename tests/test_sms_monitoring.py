"""SMS Phase 2: watching `unknown`, the sends that may have gone out.

Before this, the monitor knew only `sent` and `failed`, so a timed-out send
showed as a failure in the completion notice while "Resend failed" skipped it,
and no alert ever fired for it. Now it is counted on its own everywhere a
person looks: the run summary, the batch, the completion notice, an SLA breach
that repeats until it is settled, and its own dashboard tile.
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
from app.distribution import sla  # noqa: E402
from app.distribution.contacts import sms_contact  # noqa: E402
from app.distribution.dashboard import collect_dashboard_stats  # noqa: E402
from app.distribution.queue import enqueue_distribution, process_all_queued  # noqa: E402
from app.distribution.service import distribute_run  # noqa: E402
from app.models import (  # noqa: E402
    DistributionBatch,
    DomainEvent,
    Notification,
    PayrollRun,
    PayslipDelivery,
    User,
)

OK_BODY = '{"success": true, "data": {"status": "queued", "task_id": "t"}}'
SASUSYNC = dict(
    SMS_BACKEND="sasusync", SMS_SENDER_ID="Payrolla", SASUSYNC_API_KEY="key",
    SASUSYNC_BASE_URL="https://sms.sasusync.com", SASUSYNC_SANDBOX=True,
    PUBLIC_BASE_URL="https://pmvp-v1.onrender.com",
)


class _MonitorCase(unittest.TestCase):
    def setUp(self):
        self.app = create_app()
        self.app.config.update(TESTING=True, WTF_CSRF_ENABLED=False, **SASUSYNC)
        self.http = self.app.test_client()
        self.ctx = self.app.app_context()
        self.ctx.push()
        sla.reset()
        self.operator = User.query.filter_by(email="admin@payrolla.com").first()
        self.tenant = User.query.filter_by(email="admin@msc.com").first()
        self.run = PayrollRun.query.filter_by(
            client_company_id=self.tenant.client_company_id, status="Approved"
        ).first()
        self.items = list(self.run.items)
        self.numbers = [sms_contact(item)[0] for item in self.items]

    def tearDown(self):
        sla.reset()
        db.session.remove()
        self.ctx.pop()

    def provider(self, timeout_for=(), refuse_for=()):
        """SasuSync accepts everyone except these numbers."""
        def answer(url, *, headers=None, json=None, timeout=30):
            number = json["recipients"][0]
            if number in timeout_for:
                raise TimeoutError("timed out")
            if number in refuse_for:
                return 400, '{"detail": "refused"}'
            return 200, OK_BODY
        return mock.patch("app.distribution.sasusync._http_post", side_effect=answer)

    def login(self, email):
        response = self.http.post("/login", data={"email": email, "password": "password123"})
        self.assertEqual(response.status_code, 302)

    def unknown_row(self, item, minutes_ago, *, created_days_ago=0):
        now = datetime.now(timezone.utc)
        row = PayslipDelivery(
            payroll_item_id=item.id, payroll_run_id=self.run.id, channel="sms",
            status="unknown", attempts=1, error="timed out",
            claimed_at=now - timedelta(minutes=minutes_ago),
            created_at=now - timedelta(days=created_days_ago),
        )
        db.session.add(row)
        db.session.commit()
        return row


class RunSummaryTests(_MonitorCase):
    def test_unknown_is_counted_apart_from_failed(self):
        with self.provider(timeout_for={self.numbers[0]}, refuse_for={self.numbers[1]}):
            summary = distribute_run(self.run, channel="sms")
        self.assertEqual(summary["unknown"], 1)
        self.assertEqual(summary["unknown_workers"], [self.items[0].staff_id])
        self.assertEqual(summary["failed"], 1)
        self.assertEqual(summary["failed_workers"], [self.items[1].staff_id])
        self.assertEqual(summary["sent"], len(self.items) - 2)

    def test_the_batch_records_its_unknown_count(self):
        enqueue_distribution(self.run, "sms", False, self.operator)
        with self.provider(timeout_for={self.numbers[0]}):
            process_all_queued()
        batch = DistributionBatch.query.filter_by(payroll_run_id=self.run.id).one()
        self.assertEqual(
            (batch.sent_count, batch.failed_count, batch.unknown_count),
            (len(self.items) - 1, 0, 1),
        )


class CompletionNoticeTests(_MonitorCase):
    def _notice(self, initiator):
        enqueue_distribution(self.run, "sms", False, initiator)
        with self.provider(timeout_for={self.numbers[0]}):
            process_all_queued()
        return DomainEvent.query.filter(DomainEvent.event_type.like("distribution.%")).filter(
            DomainEvent.event_type != "distribution.sla_breach"
        ).order_by(DomainEvent.id.desc()).first()

    def test_an_operator_is_warned_and_told_to_check_sasusync(self):
        event = self._notice(self.operator)
        self.assertEqual(event.event_type, "distribution.unconfirmed")
        notice = Notification.query.filter_by(event_id=event.id, user_id=self.operator.id).one()
        self.assertEqual(notice.level, "warning")
        self.assertIn("1 unconfirmed", event.summary)
        self.assertIn("1 may have been sent. Check SasuSync before resending.", event.summary)
        self.assertNotIn("completed", event.summary)

    def test_unknown_does_not_escalate_to_platform_admins(self):
        """They hear through the SLA breach instead, so not twice."""
        event = self._notice(self.operator)
        self.assertEqual(Notification.query.filter_by(event_id=event.id).count(), 1)

    def test_a_tenant_is_not_sent_to_a_portal_it_cannot_log_in_to(self):
        event = self._notice(self.tenant)
        self.assertIn("may have been sent; Payrolla is checking.", event.summary)
        self.assertNotIn("SasuSync", event.summary)


class SlaUnknownBreachTests(_MonitorCase):
    def _unknown_breaches(self):
        return [b for b in sla.evaluate_sla()["breaches"] if b["type"] == "unknown"]

    def test_fires_for_an_unknown_row_older_than_the_threshold(self):
        self.unknown_row(self.items[0], minutes_ago=31)
        self.unknown_row(self.items[1], minutes_ago=5)
        breaches = self._unknown_breaches()
        self.assertEqual(len(breaches), 1)
        self.assertEqual(breaches[0]["count"], 1)
        self.assertIn("unconfirmed after 30 min", breaches[0]["detail"])

    def test_not_for_a_younger_row(self):
        self.unknown_row(self.items[0], minutes_ago=29)
        self.assertEqual(self._unknown_breaches(), [])

    def test_not_when_turned_off(self):
        self.app.config["SLA_UNKNOWN_MINUTES"] = 0
        self.unknown_row(self.items[0], minutes_ago=600)
        self.assertEqual(self._unknown_breaches(), [])

    def test_it_alerts_once_per_cooldown_and_clears_once_settled(self):
        self.app.config.update(SLA_CHECK_INTERVAL_SECONDS=0, SLA_ALERT_COOLDOWN_SECONDS=3600)
        row = self.unknown_row(self.items[0], minutes_ago=45)
        sla.maybe_check_sla()
        sla.maybe_check_sla()
        alerts = DomainEvent.query.filter_by(event_type="distribution.sla_breach").all()
        self.assertEqual(len([a for a in alerts if "unconfirmed" in a.summary]), 1)
        row.status = "sent"
        db.session.commit()
        self.assertEqual(self._unknown_breaches(), [])


class DashboardTests(_MonitorCase):
    def test_the_unconfirmed_count_is_standing_state_not_windowed(self):
        self.unknown_row(self.items[0], minutes_ago=5)
        self.unknown_row(self.items[1], minutes_ago=5, created_days_ago=100)
        stats = collect_dashboard_stats(window_days=30)
        self.assertEqual(stats["deliveries"]["unknown"], 2)

    def test_the_tile_links_to_the_rows_and_each_row_to_its_run(self):
        self.unknown_row(self.items[0], minutes_ago=5)
        self.login("admin@payrolla.com")
        page = self.http.get("/distribution/dashboard").get_data(as_text=True)
        self.assertIn("Unconfirmed", page)
        self.assertIn("/distribution/history?status=unknown", page)
        history = self.http.get("/distribution/history?status=unknown").get_data(as_text=True)
        self.assertIn(self.items[0].staff_id, history)
        self.assertIn(f"/distribution/run/{self.run.id}", history)


class TenantIsolationTests(_MonitorCase):
    """Tenant A's client_admin gets 404 on tenant B's run for every action that
    exists so far. (The confirm step joins this list in Phase 3.)"""

    def test_another_tenants_run_is_404_for_view_send_resend_and_cancel(self):
        self.login("admin@acme.com")
        base = f"/company/runs/{self.run.id}/distribute"
        self.assertEqual(self.http.get(base).status_code, 404)
        self.assertEqual(self.http.get(f"{base}/status-fragment").status_code, 404)
        for path in (f"{base}/send", f"{base}/resend-failed"):
            response = self.http.post(path, data={"channel": "sms", "nonce": path})
            self.assertEqual(response.status_code, 404, path)
        self.assertEqual(self.http.post(f"{base}/cancel").status_code, 404)
        self.assertEqual(DistributionBatch.query.filter_by(payroll_run_id=self.run.id).count(), 0)


if __name__ == "__main__":
    unittest.main()
