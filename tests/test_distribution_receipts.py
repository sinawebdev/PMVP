"""Phase 4, Slice 4 — WhatsApp/SMS delivery receipts behind the queue.

The senders capture the provider message id; provider callbacks (webhooks) map to
the matching delivery: a delivered/read receipt records provider_status +
delivered_at; a failed receipt flips the delivery back to failed for retry. The
webhook endpoints verify the provider secret/token and stay disabled until
configured.
"""

import hashlib
import hmac
import json
import os
import unittest

os.environ["SKIP_DOTENV"] = "true"
os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["SEED_DEMO_DATA"] = "true"
os.environ["PERSISTENCE_REQUIRED"] = "false"

from app import create_app, db  # noqa: E402
from app.distribution import channels as channels_mod  # noqa: E402
from app.distribution.channels import (  # noqa: E402
    CloudWhatsAppSender,
    HubtelSmsSender,
    OutboundMessage,
    _extract_message_id,
)
from app.distribution.receipts import (  # noqa: E402
    apply_receipt,
    parse_hubtel_status,
    parse_whatsapp_statuses,
)
from app.models import DELIVERY_SENT, PayrollRun, PayslipDelivery  # noqa: E402


class MessageIdCaptureTestCase(unittest.TestCase):
    def setUp(self):
        self.app = create_app()
        self.ctx = self.app.app_context()
        self.ctx.push()

    def tearDown(self):
        self.ctx.pop()

    def test_extract_message_id_shapes(self):
        self.assertEqual(
            _extract_message_id('{"messages":[{"id":"wamid.ABC"}]}'), "wamid.ABC"
        )
        self.assertEqual(_extract_message_id('{"messageId":"HB123"}'), "HB123")
        self.assertEqual(_extract_message_id('{"data":{"id":"D9"}}'), "D9")
        self.assertIsNone(_extract_message_id("not json"))

    def test_whatsapp_send_captures_message_id(self):
        self.app.config.update(
            WHATSAPP_BACKEND="cloud", WHATSAPP_TOKEN="t", WHATSAPP_PHONE_NUMBER_ID="p"
        )

        def fake_post(url, *, headers=None, json=None, timeout=30):
            return 200, '{"messages":[{"id":"wamid.XYZ"}]}'

        original = channels_mod._http_post
        channels_mod._http_post = fake_post
        try:
            result = CloudWhatsAppSender().send(
                OutboundMessage("whatsapp", "+233241234567", "", "hi")
            )
        finally:
            channels_mod._http_post = original
        self.assertTrue(result.ok)
        self.assertEqual(result.message_id, "wamid.XYZ")


class ReceiptParsingTestCase(unittest.TestCase):
    def test_parse_whatsapp_statuses(self):
        payload = {
            "entry": [{"changes": [{"value": {"statuses": [
                {"id": "wamid.1", "status": "delivered"},
                {"id": "wamid.2", "status": "failed",
                 "errors": [{"title": "Undeliverable"}]},
            ]}}]}]
        }
        parsed = parse_whatsapp_statuses(payload)
        self.assertEqual(len(parsed), 2)
        self.assertEqual(parsed[0], {"message_id": "wamid.1", "status": "delivered", "reason": None})
        self.assertEqual(parsed[1]["reason"], "Undeliverable")

    def test_parse_hubtel_status_case_insensitive(self):
        parsed = parse_hubtel_status({"MessageId": "HB1", "Status": "Delivered"})
        self.assertEqual(parsed, [{"message_id": "HB1", "status": "Delivered", "reason": None}])


class ApplyReceiptTestCase(unittest.TestCase):
    def setUp(self):
        self.app = create_app()
        self.app.config["TESTING"] = True
        self.app.config["DISTRIBUTION_RETRY_BACKOFF_SECONDS"] = 0
        self.app.config["DISTRIBUTION_MAX_ATTEMPTS"] = 3
        self.ctx = self.app.app_context()
        self.ctx.push()
        self.run = PayrollRun.query.filter_by(status="Approved").first()
        self.item = self.run.items[0]
        self.delivery = PayslipDelivery(
            payroll_item_id=self.item.id, payroll_run_id=self.run.id,
            channel="whatsapp", status=DELIVERY_SENT, attempts=1,
            provider_message_id="wamid.TRACK",
        )
        db.session.add(self.delivery)
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        self.ctx.pop()

    def test_delivered_receipt_records_confirmation(self):
        d = apply_receipt("wamid.TRACK", "delivered")
        self.assertIsNotNone(d)
        self.assertEqual(d.provider_status, "delivered")
        self.assertIsNotNone(d.delivered_at)
        self.assertEqual(d.status, DELIVERY_SENT)  # still sent, now confirmed

    def test_failed_receipt_flips_to_failed_for_retry(self):
        d = apply_receipt("wamid.TRACK", "undelivered", reason="no route")
        self.assertEqual(d.status, "failed")
        self.assertIsNotNone(d.next_retry_at)  # re-enters the retry system
        self.assertIn("undelivered", d.error)

    def test_unknown_status_and_unknown_id_are_ignored(self):
        self.assertIsNone(apply_receipt("wamid.TRACK", "some-weird-status"))
        self.assertIsNone(apply_receipt("no-such-id", "delivered"))


class WebhookRouteTestCase(unittest.TestCase):
    def setUp(self):
        self.app = create_app()
        self.app.config["TESTING"] = True
        self.http = self.app.test_client()
        self.ctx = self.app.app_context()
        self.ctx.push()
        run = PayrollRun.query.filter_by(status="Approved").first()
        self.delivery = PayslipDelivery(
            payroll_item_id=run.items[0].id, payroll_run_id=run.id,
            channel="whatsapp", status=DELIVERY_SENT, attempts=1,
            provider_message_id="wamid.HOOK",
        )
        db.session.add(self.delivery)
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        self.ctx.pop()

    def test_webhooks_disabled_until_configured(self):
        # No tokens set -> 404 (can't be spoofed on an unconfigured deployment).
        self.assertEqual(self.http.get("/distribution/webhooks/whatsapp").status_code, 404)
        self.assertEqual(
            self.http.post("/distribution/webhooks/hubtel", json={}).status_code, 404
        )

    def test_whatsapp_verify_handshake(self):
        self.app.config["WHATSAPP_VERIFY_TOKEN"] = "verify-me"
        ok = self.http.get(
            "/distribution/webhooks/whatsapp",
            query_string={"hub.mode": "subscribe", "hub.verify_token": "verify-me",
                          "hub.challenge": "12345"},
        )
        self.assertEqual(ok.status_code, 200)
        self.assertEqual(ok.get_data(as_text=True), "12345")
        bad = self.http.get(
            "/distribution/webhooks/whatsapp",
            query_string={"hub.mode": "subscribe", "hub.verify_token": "wrong",
                          "hub.challenge": "x"},
        )
        self.assertEqual(bad.status_code, 403)

    def test_whatsapp_callback_updates_delivery(self):
        self.app.config["WHATSAPP_VERIFY_TOKEN"] = "verify-me"  # no app secret -> no signature needed
        payload = {"entry": [{"changes": [{"value": {"statuses": [
            {"id": "wamid.HOOK", "status": "delivered"}]}}]}]}
        resp = self.http.post("/distribution/webhooks/whatsapp", json=payload)
        self.assertEqual(resp.status_code, 200)
        db.session.refresh(self.delivery)
        self.assertEqual(self.delivery.provider_status, "delivered")

    def test_whatsapp_signature_enforced_when_app_secret_set(self):
        self.app.config["WHATSAPP_VERIFY_TOKEN"] = "verify-me"
        self.app.config["WHATSAPP_APP_SECRET"] = "s3cr3t"
        payload = {"entry": [{"changes": [{"value": {"statuses": [
            {"id": "wamid.HOOK", "status": "read"}]}}]}]}
        raw = json.dumps(payload).encode()
        # Wrong/missing signature -> 403.
        self.assertEqual(
            self.http.post("/distribution/webhooks/whatsapp", data=raw,
                           content_type="application/json").status_code,
            403,
        )
        # Correct signature -> 200.
        sig = hmac.new(b"s3cr3t", raw, hashlib.sha256).hexdigest()
        ok = self.http.post(
            "/distribution/webhooks/whatsapp", data=raw, content_type="application/json",
            headers={"X-Hub-Signature-256": f"sha256={sig}"},
        )
        self.assertEqual(ok.status_code, 200)
        db.session.refresh(self.delivery)
        self.assertEqual(self.delivery.provider_status, "read")

    def test_hubtel_callback_requires_secret(self):
        self.app.config["HUBTEL_WEBHOOK_SECRET"] = "hub-secret"
        # Wrong secret -> 403.
        self.assertEqual(
            self.http.post("/distribution/webhooks/hubtel?secret=nope",
                           json={"MessageId": "wamid.HOOK", "Status": "Delivered"}).status_code,
            403,
        )
        # Right secret -> applied.
        ok = self.http.post(
            "/distribution/webhooks/hubtel?secret=hub-secret",
            json={"MessageId": "wamid.HOOK", "Status": "Delivered"},
        )
        self.assertEqual(ok.status_code, 200)
        db.session.refresh(self.delivery)
        self.assertEqual(self.delivery.provider_status, "delivered")


if __name__ == "__main__":
    unittest.main()
