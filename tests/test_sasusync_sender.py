"""SMS Phase 1: the SasuSync sender.

Every row of the plan's classification table, with no network: status-code rows
mock ``_http_post``; transport rows mock ``urllib.request.urlopen`` so they go
through the real ``_http_post`` and prove how its exceptions surface.

The distinction that matters most is *nothing was sent* (safe to retry) versus
*may have been sent* (never re-sent automatically, or the worker gets two SMS
and the account pays twice).
"""
import http.client
import logging
import os
import socket
import unittest
import urllib.error
from unittest import mock

os.environ["SKIP_DOTENV"] = "true"
os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["SEED_DEMO_DATA"] = "true"
os.environ["PERSISTENCE_REQUIRED"] = "false"

from app import create_app, db  # noqa: E402
from app.distribution.channels import (  # noqa: E402
    ConsoleSmsSender,
    HubtelSmsSender,
    OutboundMessage,
    get_sender,
    simulated_channels,
)
from app.distribution.sasusync import SasuSyncSmsSender  # noqa: E402
from app.distribution.service import distribute_run  # noqa: E402
from app.models import DELIVERY_FAILED, PayrollRun, PayslipDelivery  # noqa: E402

API_KEY = "sk_live_THIS-MUST-NEVER-BE-LOGGED"
BODY = "MSC Limited: your September 2026 payslip is ready."
OK_BODY = (
    '{"success": true, "balance": {"deducted": 1, "remaining": 1649}, '
    '"data": {"recipients_count": 1, "status": "queued", '
    '"task_id": "6f461b5a-7d35-4d55-97b0-00e420bf563b"}}'
)
SASUSYNC_CONFIG = dict(
    SMS_BACKEND="sasusync", SMS_SENDER_ID="Payrolla", SASUSYNC_API_KEY=API_KEY,
    SASUSYNC_BASE_URL="https://sms.sasusync.com", SASUSYNC_SANDBOX=True,
)


class _CaptureLog(logging.Handler):
    def __init__(self):
        super().__init__()
        self.messages = []

    def emit(self, record):
        self.messages.append(record.getMessage())


class _SenderCase(unittest.TestCase):
    def setUp(self):
        self.app = create_app()
        self.app.config.update(TESTING=True, **SASUSYNC_CONFIG)
        self.ctx = self.app.app_context()
        self.ctx.push()

    def tearDown(self):
        db.session.remove()
        self.ctx.pop()

    def _message(self, recipient="0241234567", delivery_id=7):
        return OutboundMessage("sms", recipient, "", BODY, item_id=42,
                               delivery_id=delivery_id)

    def _send_with_status(self, status, body=""):
        with mock.patch("app.distribution.sasusync._http_post",
                        return_value=(status, body)) as post:
            result = SasuSyncSmsSender().send(self._message())
        return result, post

    def _send_raising(self, exc):
        with mock.patch("urllib.request.urlopen", side_effect=exc):
            return SasuSyncSmsSender().send(self._message())


class ClassificationTableTests(_SenderCase):
    def test_2xx_queued_is_ok_with_the_task_id_and_units(self):
        for status in (200, 201, 202):
            with self.subTest(status=status):
                result, _ = self._send_with_status(status, OK_BODY)
                self.assertTrue(result.ok)
                self.assertEqual(result.provider, "sasusync")
                self.assertEqual(result.message_id, "6f461b5a-7d35-4d55-97b0-00e420bf563b")
                self.assertEqual(result.units, 1)
                self.assertFalse(result.ambiguous)

    def test_a_queued_true_acceptance_is_ok_not_an_error(self):
        result, _ = self._send_with_status(
            200, '{"success": true, "queued": true, "task_id": "t-123"}'
        )
        self.assertTrue(result.ok)
        self.assertEqual(result.message_id, "t-123")
        self.assertIsNone(result.units)

    def test_refusals_fail_and_never_retry(self):
        # 400, 402, 403, 422 are the plan's rows; 401, 404 and 409 are the other
        # answers the brief says never to retry. All mean "nothing was sent".
        for status in (400, 401, 402, 403, 404, 409, 422):
            with self.subTest(status=status):
                result, _ = self._send_with_status(status, '{"detail": "refused"}')
                self.assertFalse(result.ok)
                self.assertIs(result.retryable, False)
                self.assertFalse(result.ambiguous)
                self.assertIn(str(status), result.error)

    def test_429_and_5xx_fail_and_retry(self):
        for status in (429, 500, 502, 503, 504):
            with self.subTest(status=status):
                result, _ = self._send_with_status(status, '{"detail": "busy"}')
                self.assertFalse(result.ok)
                self.assertIs(result.retryable, True)
                self.assertFalse(result.ambiguous)

    def test_an_unexpected_status_is_ambiguous(self):
        result, _ = self._send_with_status(302, "")
        self.assertFalse(result.ok)
        self.assertTrue(result.ambiguous)

    def test_connection_refused_or_dns_failure_retries(self):
        for exc in (
            urllib.error.URLError(ConnectionRefusedError(10061, "refused")),
            urllib.error.URLError(socket.gaierror(11001, "getaddrinfo failed")),
        ):
            with self.subTest(exc=repr(exc)):
                result = self._send_raising(exc)
                self.assertFalse(result.ok)
                self.assertIs(result.retryable, True)
                self.assertFalse(result.ambiguous)

    def test_timeouts_resets_and_disconnects_are_ambiguous(self):
        for exc in (
            TimeoutError("The read operation timed out"),
            socket.timeout("timed out"),
            urllib.error.URLError(TimeoutError("timed out")),
            ConnectionResetError(10054, "reset by peer"),
            http.client.RemoteDisconnected("Remote end closed connection"),
            BrokenPipeError(32, "broken pipe"),
        ):
            with self.subTest(exc=repr(exc)):
                result = self._send_raising(exc)
                self.assertFalse(result.ok)
                self.assertTrue(result.ambiguous)
                self.assertIsNot(result.retryable, True)
                self.assertIn("may have been sent", result.error)

    def test_anything_unclassified_is_ambiguous(self):
        for exc in (ValueError("weird"), urllib.error.URLError("ssl handshake"), OSError("?")):
            with self.subTest(exc=repr(exc)):
                result = self._send_raising(exc)
                self.assertTrue(result.ambiguous)
                self.assertIsNot(result.retryable, True)


class RequestShapeTests(_SenderCase):
    def test_sandbox_request_matches_the_vendor_brief(self):
        _, post = self._send_with_status(200, OK_BODY)
        (url,), kwargs = post.call_args
        self.assertEqual(url, "https://sms.sasusync.com/smssandbox/v1/send")
        self.assertEqual(kwargs["headers"], {"X-API-Key": API_KEY})
        self.assertEqual(kwargs["timeout"], 30)
        self.assertEqual(kwargs["json"], {
            "sender": "Payrolla",
            "recipients": ["233241234567"],
            "message": BODY,
            "metadata": {"delivery_id": 7},
        })

    def test_live_endpoint_when_the_sandbox_is_off(self):
        self.app.config["SASUSYNC_SANDBOX"] = False
        _, post = self._send_with_status(200, OK_BODY)
        self.assertEqual(post.call_args.args[0], "https://sms.sasusync.com/api/v1/send")

    def test_a_nine_digit_number_is_sent_with_its_zero_restored(self):
        with mock.patch("app.distribution.sasusync._http_post",
                        return_value=(200, OK_BODY)) as post:
            SasuSyncSmsSender().send(self._message(recipient="241234567"))
        self.assertEqual(post.call_args.kwargs["json"]["recipients"], ["233241234567"])

    def test_no_metadata_without_a_delivery_id(self):
        with mock.patch("app.distribution.sasusync._http_post",
                        return_value=(200, OK_BODY)) as post:
            SasuSyncSmsSender().send(self._message(delivery_id=None))
        self.assertNotIn("metadata", post.call_args.kwargs["json"])

    def test_an_invalid_number_is_refused_without_a_request(self):
        for recipient in ("0302123456", "0231234567", "12345", ""):
            with self.subTest(recipient=recipient), mock.patch(
                "app.distribution.sasusync._http_post"
            ) as post:
                result = SasuSyncSmsSender().send(self._message(recipient=recipient))
                post.assert_not_called()
                self.assertFalse(result.ok)
                self.assertIs(result.retryable, False)

    def test_missing_configuration_fails_without_a_request(self):
        for key in ("SASUSYNC_API_KEY", "SASUSYNC_BASE_URL", "SMS_SENDER_ID"):
            with self.subTest(missing=key), mock.patch(
                "app.distribution.sasusync._http_post"
            ) as post, mock.patch.dict(self.app.config, {key: None}):
                result = SasuSyncSmsSender().send(self._message())
                post.assert_not_called()
                self.assertFalse(result.ok)
                self.assertIs(result.retryable, False)


class LogHygieneTests(_SenderCase):
    """Neither the API key nor the full number reaches a log line or the stored
    error text."""

    NUMBER_FORMS = ("0241234567", "233241234567", "241234567")

    def _capture(self, send):
        handler = _CaptureLog()
        logger = self.app.logger
        was_disabled, previous_level = logger.disabled, logger.level
        logger.disabled = False  # see test_security_findings: Alembic disables it
        logger.setLevel(logging.INFO)
        logger.addHandler(handler)
        try:
            result = send()
        finally:
            logger.removeHandler(handler)
            logger.disabled, logger.level = was_disabled, previous_level
        captured = "\n".join(handler.messages)
        self.assertTrue(captured.strip(), "captured nothing; the checks below would be vacuous")
        return captured, result

    def _assert_clean(self, text):
        self.assertNotIn(API_KEY, text)
        self.assertNotIn(BODY, text)
        for number in self.NUMBER_FORMS:
            self.assertNotIn(number, text)

    def test_a_successful_send(self):
        logged, result = self._capture(lambda: self._send_with_status(200, OK_BODY)[0])
        self._assert_clean(logged)
        self.assertIn("[sasusync]", logged)

    def test_a_refusal_that_quotes_the_number_back(self):
        echo = '{"detail": "Recipient 233241234567 is invalid"}'
        logged, result = self._capture(lambda: self._send_with_status(422, echo)[0])
        self._assert_clean(logged)
        self._assert_clean(result.error)

    def test_a_timeout(self):
        logged, result = self._capture(lambda: self._send_raising(TimeoutError("timed out")))
        self._assert_clean(logged)
        self._assert_clean(result.error)


class WiringTests(_SenderCase):
    def test_get_sender_picks_the_backend(self):
        self.assertIsInstance(get_sender("sms"), SasuSyncSmsSender)
        self.app.config["SMS_BACKEND"] = "hubtel"
        self.assertIsInstance(get_sender("sms"), HubtelSmsSender)
        self.app.config["SMS_BACKEND"] = "console"
        self.assertIsInstance(get_sender("sms"), ConsoleSmsSender)

    def test_real_sms_backends_are_not_reported_as_simulated(self):
        for backend in ("sasusync", "hubtel"):
            with self.subTest(backend=backend):
                self.app.config["SMS_BACKEND"] = backend
                self.assertNotIn("sms", simulated_channels())
        self.app.config["SMS_BACKEND"] = "console"
        self.assertIn("sms", simulated_channels())


class NoAutomaticResendTests(_SenderCase):
    """Until Phase 2 adds the `unknown` status, a refused or ambiguous SasuSync
    send is a failure with no retry scheduled, so the sweep cannot re-send it."""

    def setUp(self):
        super().setUp()
        self.run = PayrollRun.query.filter_by(status="Approved").first()
        self.item = self.run.items[0]
        self.item.momo_number = "241234567"
        if self.item.employee:
            self.item.employee.phone = None
            self.item.employee.momo_number = "241234567"
        db.session.commit()

    def _delivery_after(self, **patch_kwargs):
        with mock.patch("app.distribution.sasusync._http_post", **patch_kwargs):
            distribute_run(self.run, channel="sms")
        return PayslipDelivery.query.filter_by(
            payroll_item_id=self.item.id, channel="sms"
        ).one()

    def test_a_retryable_failure_is_scheduled_for_retry(self):
        delivery = self._delivery_after(return_value=(503, '{"detail": "busy"}'))
        self.assertEqual(delivery.status, DELIVERY_FAILED)
        self.assertIsNotNone(delivery.next_retry_at)

    def test_a_refusal_is_not(self):
        delivery = self._delivery_after(return_value=(400, '{"detail": "bad"}'))
        self.assertEqual(delivery.status, DELIVERY_FAILED)
        self.assertIsNone(delivery.next_retry_at)

    def test_an_ambiguous_send_is_not(self):
        delivery = self._delivery_after(side_effect=TimeoutError("timed out"))
        self.assertEqual(delivery.status, DELIVERY_FAILED)
        self.assertIsNone(delivery.next_retry_at)
        self.assertIn("may have been sent", delivery.error)

    def test_the_delivery_id_reaches_sasusync_as_metadata(self):
        first = self._delivery_after(return_value=(503, "{}"))
        with mock.patch("app.distribution.sasusync._http_post",
                        return_value=(200, OK_BODY)) as post:
            distribute_run(self.run, channel="sms", only_failed=True)
        payloads = [c.kwargs["json"] for c in post.call_args_list]
        mine = [p for p in payloads if p["recipients"] == ["233241234567"]]
        self.assertTrue(mine)
        self.assertEqual(mine[0]["metadata"], {"delivery_id": first.id})


if __name__ == "__main__":
    unittest.main()
