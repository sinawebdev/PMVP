"""SasuSync delivery reports, and the delivery page catching up by itself.

The webhook is what tells Payrolla a text reached the phone: without it a delivery
stops at `sent`, which only means SasuSync took it.
"""
import hashlib
import hmac
import json
import os
import unittest
from datetime import datetime, timedelta, timezone

os.environ["SKIP_DOTENV"] = "true"
os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["SEED_DEMO_DATA"] = "true"
os.environ["PERSISTENCE_REQUIRED"] = "false"

from app import create_app, db  # noqa: E402
from app.distribution import request_limit  # noqa: E402
from app.distribution.service import _mark_sent  # noqa: E402
from app.models import (  # noqa: E402
    DELIVERY_FAILED,
    DELIVERY_SENT,
    DELIVERY_UNKNOWN,
    DistributionBatch,
    PayrollItem,
    PayrollRun,
    PayslipDelivery,
    User,
)
from app.payroll_status import APPROVED  # noqa: E402

SECRET = "whsec_test_secret"
URL = "/distribution/webhooks/sasusync"
NUMBER = "233241234567"
TASK_ID = "6f461b5a-7d35-4d55-97b0-00e420bf563b"


def stamp(minutes=0):
    return (datetime(2026, 11, 9, 12, 0) + timedelta(minutes=minutes)).isoformat()


class _WebhookCase(unittest.TestCase):
    def setUp(self):
        self.app = create_app()
        self.app.config.update(TESTING=True, WTF_CSRF_ENABLED=False,
                               SASUSYNC_WEBHOOK_SECRET=SECRET)
        self.client = self.app.test_client()
        self.ctx = self.app.app_context()
        self.ctx.push()
        request_limit.reset()
        tenant = User.query.filter_by(email="admin@contoso.com").first()
        self.run = PayrollRun.query.filter_by(client_company_id=tenant.client_company_id).first()
        self.run.status = APPROVED
        PayslipDelivery.query.filter_by(payroll_run_id=self.run.id).delete()
        DistributionBatch.query.filter_by(payroll_run_id=self.run.id).delete()
        self.item = PayrollItem.query.filter_by(payroll_run_id=self.run.id).first()
        self.delivery = self._delivery(self.item)
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        self.ctx.pop()

    def _delivery(self, item, *, status=DELIVERY_SENT, recipient=NUMBER, message_id=TASK_ID):
        now = datetime.now(timezone.utc)
        delivery = PayslipDelivery(
            payroll_item_id=item.id, payroll_run_id=self.run.id, channel="sms",
            recipient=recipient, status=status, provider="sasusync", attempts=1,
            provider_message_id=message_id, sent_at=now, claimed_at=now,
        )
        db.session.add(delivery)
        db.session.flush()
        return delivery

    def _post(self, body, signature=None):
        raw = body if isinstance(body, bytes) else json.dumps(body).encode()
        signature = signature or hmac.new(SECRET.encode(), raw, hashlib.sha256).hexdigest()
        return self.client.post(URL, data=raw, content_type="application/json",
                                headers={"X-Webhook-Signature": signature})

    def _report(self, event, *, minutes=0, data=None, metadata=None):
        """A report in the documented shape: event, timestamp, data."""
        body = {"event": event, "timestamp": stamp(minutes),
                "data": {"message_id": "per-message-id", "recipient": NUMBER, "status": event,
                         **(data or {})}}
        if metadata is not None:
            body["metadata"] = metadata
        response = self._post(body)
        self.assertEqual(response.status_code, 200)
        db.session.expire_all()
        return response.get_json()["applied"]

    def _by_id(self):
        return {"delivery_id": self.delivery.id}


class WebhookSecurityTests(_WebhookCase):
    def test_no_secret_means_the_endpoint_is_off(self):
        self.app.config["SASUSYNC_WEBHOOK_SECRET"] = None
        self.assertEqual(self._post({"event": "delivered"}).status_code, 404)

    def test_a_tampered_body_is_refused_and_changes_nothing(self):
        raw = json.dumps({"event": "delivered", "metadata": self._by_id(), "data": {}}).encode()
        signature = hmac.new(SECRET.encode(), raw, hashlib.sha256).hexdigest()
        response = self._post(raw.replace(b"delivered", b"failed"), signature=signature)
        self.assertEqual(response.status_code, 401)
        db.session.expire_all()
        self.assertEqual(self.delivery.status, DELIVERY_SENT)

    def test_a_missing_or_foreign_signature_is_refused(self):
        response = self.client.post(URL, data=b"{}", content_type="application/json")
        self.assertEqual(response.status_code, 401)
        self.assertEqual(self._post({"event": "delivered"}, signature="café").status_code, 401)

    def test_the_signature_covers_the_raw_bytes_as_received(self):
        # Spaces json.dumps would never produce: re-serialising would break this.
        raw = b'{ "event" : "delivered",  "metadata": {"delivery_id": %d}, "data": {} }' % self.delivery.id
        self.assertEqual(self._post(raw).status_code, 200)
        db.session.expire_all()
        self.assertEqual(self.delivery.provider_status, "delivered")

    def test_it_is_rate_limited(self):
        self.app.config["SASUSYNC_WEBHOOK_RATE_PER_MIN"] = 1
        self.assertEqual(self._post({"event": "delivered"}).status_code, 200)
        self.assertEqual(self._post({"event": "delivered"}).status_code, 429)

    def test_a_verified_body_that_is_not_json_is_acknowledged_and_ignored(self):
        response = self._post(b"not json")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["applied"], 0)


class WebhookMatchingTests(_WebhookCase):
    def test_by_the_delivery_id_we_sent_at_either_level(self):
        for metadata_at_top in (True, False):
            with self.subTest(metadata_at_top=metadata_at_top):
                self.delivery.provider_status = None
                self.delivery.provider_status_at = None
                db.session.commit()
                body = {"event": "delivered", "timestamp": stamp(),
                        "data": {"recipient": "0200000000"}}
                if metadata_at_top:
                    body["metadata"] = self._by_id()
                else:
                    body["data"]["metadata"] = self._by_id()
                self.assertEqual(self._post(body).get_json()["applied"], 1)

    def test_by_the_task_id_the_send_returned(self):
        self.assertEqual(self._report("delivered", data={"task_id": TASK_ID, "recipient": "x"}), 1)

    def test_by_the_one_recent_send_to_that_number(self):
        # The documented example: only a per-message id and the recipient.
        self.assertEqual(self._report("delivered"), 1)
        self.assertEqual(self.delivery.provider_status, "delivered")

    def test_two_recent_sends_to_a_number_are_not_guessed_between(self):
        other = PayrollItem(payroll_run_id=self.run.id, staff_id="Z9", full_name="Second Payslip")
        db.session.add(other)
        db.session.flush()
        self._delivery(other, message_id="another-task")
        db.session.commit()
        self.assertEqual(self._report("delivered"), 0)
        self.assertIsNone(self.delivery.provider_status)

    def test_an_old_send_is_not_matched_on_the_number_alone(self):
        self.delivery.claimed_at = datetime.now(timezone.utc) - timedelta(days=4)
        db.session.commit()
        self.assertEqual(self._report("delivered"), 0)

    def test_an_email_delivery_is_never_touched(self):
        self.delivery.channel = "email"
        db.session.commit()
        self.assertEqual(self._report("delivered", metadata=self._by_id()), 0)
        self.assertIsNone(self.delivery.provider_status)


class WebhookOutcomeTests(_WebhookCase):
    def test_delivered_is_recorded_and_status_stays_sent(self):
        self.assertEqual(self._report("delivered", metadata=self._by_id()), 1)
        self.assertEqual(self.delivery.status, DELIVERY_SENT)
        self.assertEqual(self.delivery.provider_status, "delivered")
        self.assertEqual(self.delivery.delivered_at, datetime(2026, 11, 9, 12, 0))

    def test_sent_only_means_handed_over_so_it_is_not_delivered(self):
        self.assertEqual(self._report("sent", metadata=self._by_id()), 0)
        self.assertIsNone(self.delivery.provider_status)
        self.assertIsNone(self.delivery.delivered_at)

    def test_failed_and_expired_become_failed_with_no_automatic_retry(self):
        for event in ("failed", "expired"):
            with self.subTest(event=event):
                self.delivery.status = DELIVERY_SENT
                self.delivery.provider_status = None
                self.delivery.provider_status_at = None
                db.session.commit()
                self.assertEqual(self._report(event, metadata=self._by_id()), 1)
                self.assertEqual(self.delivery.status, DELIVERY_FAILED)
                self.assertIsNone(self.delivery.next_retry_at)
                self.assertIn(event, self.delivery.error)
                self.assertEqual(self.delivery.provider, "sasusync")

    def test_a_reported_reason_is_kept_without_the_phone_number(self):
        self._report("failed", metadata=self._by_id(),
                     data={"reason": "Blocked: 233241234567 is on a do-not-send list"})
        self.assertIn("do-not-send", self.delivery.error)
        self.assertNotIn(NUMBER, self.delivery.error)

    def test_an_unknown_row_is_settled_by_any_report(self):
        cases = {"sent": (DELIVERY_SENT, None), "delivered": (DELIVERY_SENT, "delivered"),
                 "failed": (DELIVERY_FAILED, "failed")}
        for event, (status, provider_status) in cases.items():
            with self.subTest(event=event):
                self.delivery.status = DELIVERY_UNKNOWN
                self.delivery.error = "SasuSync timed out; this message may have been sent"
                self.delivery.provider_status = None
                self.delivery.provider_status_at = None
                self.delivery.delivered_at = None
                db.session.commit()
                self.assertEqual(self._report(event, metadata=self._by_id()), 1)
                self.assertEqual(self.delivery.status, status)
                self.assertEqual(self.delivery.provider_status, provider_status)

    def test_delivered_after_failed_corrects_it_when_it_is_newer(self):
        self._report("failed", minutes=1, metadata=self._by_id())
        self.assertEqual(self.delivery.status, DELIVERY_FAILED)
        self._report("delivered", minutes=2, metadata=self._by_id())
        self.assertEqual(self.delivery.status, DELIVERY_SENT)
        self.assertEqual(self.delivery.provider_status, "delivered")
        self.assertIsNone(self.delivery.error)

    def test_an_older_report_is_ignored(self):
        self._report("delivered", minutes=5, metadata=self._by_id())
        self.assertEqual(self._report("failed", minutes=1, metadata=self._by_id()), 0)
        self.assertEqual(self.delivery.status, DELIVERY_SENT)
        self.assertEqual(self.delivery.provider_status, "delivered")

    def test_a_repeat_does_nothing(self):
        self._report("delivered", minutes=5, metadata=self._by_id())
        first = self.delivery.delivered_at
        self.assertEqual(self._report("delivered", minutes=5, metadata=self._by_id()), 0)
        self.assertEqual(self.delivery.delivered_at, first)

    def test_a_cancelled_delivery_stays_cancelled(self):
        self.delivery.status = "cancelled"
        db.session.commit()
        self.assertEqual(self._report("delivered", metadata=self._by_id()), 0)
        self.assertEqual(self.delivery.status, "cancelled")

    def test_account_events_are_acknowledged_and_change_nothing(self):
        for event, data in (("low_balance", {"sms_credits": 12}), ("topup_succeeded", {"amount": "50"})):
            with self.subTest(event=event):
                response = self._post({"event": event, "data": data})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.get_json()["applied"], 0)

    def test_a_fresh_send_clears_the_last_report(self):
        self._report("delivered", minutes=5, metadata=self._by_id())
        _mark_sent(self.delivery, "sasusync", message_id="new-task")
        db.session.commit()
        self.assertIsNone(self.delivery.provider_status)
        self.assertIsNone(self.delivery.provider_status_at)
        self.assertIsNone(self.delivery.delivered_at)


class DeliveredOnScreenTests(_WebhookCase):
    def _page(self, email, path):
        self.client.post("/login", data={"email": email, "password": "password123"})
        return self.client.get(path).get_data(as_text=True)

    def test_both_portals_show_what_was_confirmed(self):
        self._report("delivered", metadata=self._by_id())
        for email, path in (("admin@payrolla.com", f"/distribution/run/{self.run.id}"),
                            ("admin@contoso.com", f"/company/runs/{self.run.id}/distribute")):
            with self.subTest(path=path):
                html = self._page(email, path)
                self.assertIn("<dt>Delivered</dt><dd>1</dd>", html)
                self.client.get("/logout")

    def test_nothing_about_delivered_shows_before_a_report(self):
        html = self._page("admin@payrolla.com", f"/distribution/run/{self.run.id}")
        self.assertNotIn("<dt>Delivered</dt>", html)


class PageCatchesUpTests(_WebhookCase):
    """The send controls and "Distribution in progress" sit outside the polled
    region, so swapping the region alone left them stale until a manual reload."""

    def _fragment(self, email, path, **headers):
        self.client.post("/login", data={"email": email, "password": "password123"})
        return self.client.get(path, headers=headers)

    def _portals(self):
        return (("admin@payrolla.com", f"/distribution/run/{self.run.id}/status-fragment"),
                ("admin@contoso.com", f"/company/runs/{self.run.id}/distribute/status-fragment"))

    def test_the_poll_that_finds_nothing_live_reloads_the_page(self):
        for email, path in self._portals():
            with self.subTest(path=path):
                response = self._fragment(email, path, **{"HX-Request": "true"})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.headers.get("HX-Refresh"), "true")
                self.client.get("/logout")

    def test_while_a_send_is_live_it_keeps_polling_without_reloading(self):
        db.session.add(DistributionBatch(payroll_run_id=self.run.id, status="running",
                                         client_company_id=self.run.client_company_id,
                                         channel="sms", total=1))
        db.session.commit()
        for email, path in self._portals():
            with self.subTest(path=path):
                response = self._fragment(email, path, **{"HX-Request": "true"})
                self.assertNotIn("HX-Refresh", response.headers)
                self.assertIn('hx-trigger="every 3s"', response.get_data(as_text=True))
                self.client.get("/logout")

    def test_an_ordinary_request_is_never_told_to_refresh(self):
        response = self._fragment(*self._portals()[0])
        self.assertNotIn("HX-Refresh", response.headers)


if __name__ == "__main__":
    unittest.main()
