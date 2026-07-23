"""Phase 4, Slice 2 — per-channel rate limiting / provider throttling.

The limiter paces sends per channel to a configured rate. Default (unset) is
unlimited, so existing sends are unchanged. A provider 429 is surfaced as a clear
rate-limited failure.
"""

import os
import unittest

os.environ["SKIP_DOTENV"] = "true"
os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["SEED_DEMO_DATA"] = "true"
os.environ["PERSISTENCE_REQUIRED"] = "false"

from app import create_app, db  # noqa: E402
from app.distribution import throttle as throttle_mod  # noqa: E402
from app.distribution.throttle import rate_per_sec, reset, throttle  # noqa: E402
from app.distribution.channels import OutboundMessage  # noqa: E402
from app.distribution.service import distribute_run  # noqa: E402
from app.models import PayrollRun  # noqa: E402


class _Clock:
    """Deterministic monotonic clock; records sleeps instead of sleeping."""

    def __init__(self):
        self.t = 1000.0
        self.slept = []

    def now(self):
        return self.t

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.t += seconds  # time advances by the wait


class ThrottleUnitTestCase(unittest.TestCase):
    def setUp(self):
        self.app = create_app()
        self.ctx = self.app.app_context()
        self.ctx.push()
        reset()

    def tearDown(self):
        reset()
        self.ctx.pop()

    def test_unlimited_by_default(self):
        self.assertEqual(rate_per_sec("sms"), 0.0)
        self.assertEqual(throttle("sms"), 0.0)

    def test_paces_to_configured_rate(self):
        self.app.config["RATE_LIMIT_SMS_PER_SEC"] = 10.0  # min 0.1s between sends
        clock = _Clock()
        # First send: no wait (nothing sent yet).
        w1 = throttle("sms", sleep=clock.sleep, now=clock.now)
        # Second send immediately after: must wait ~0.1s.
        w2 = throttle("sms", sleep=clock.sleep, now=clock.now)
        self.assertEqual(w1, 0.0)
        self.assertAlmostEqual(w2, 0.1, places=6)

    def test_channels_are_independent(self):
        self.app.config["RATE_LIMIT_SMS_PER_SEC"] = 1.0
        self.app.config["RATE_LIMIT_EMAIL_PER_SEC"] = 1.0
        clock = _Clock()
        throttle("sms", sleep=clock.sleep, now=clock.now)
        # A different channel is not blocked by the sms slot.
        self.assertEqual(throttle("email", sleep=clock.sleep, now=clock.now), 0.0)

    def test_rate_recovers_after_enough_time(self):
        self.app.config["RATE_LIMIT_SMS_PER_SEC"] = 10.0
        clock = _Clock()
        throttle("sms", sleep=clock.sleep, now=clock.now)
        clock.t += 5.0  # plenty of time passes
        self.assertEqual(throttle("sms", sleep=clock.sleep, now=clock.now), 0.0)


class ThrottleIntegrationTestCase(unittest.TestCase):
    def setUp(self):
        self.app = create_app()
        self.app.config["TESTING"] = True
        self.ctx = self.app.app_context()
        self.ctx.push()
        reset()
        self.run = PayrollRun.query.filter_by(status="Approved").first()

    def tearDown(self):
        reset()
        db.session.remove()
        self.ctx.pop()

    def test_distribute_run_calls_throttle_per_send(self):
        calls = []
        original = throttle_mod.throttle

        def spy(channel, **kw):
            calls.append(channel)
            return 0.0

        throttle_mod.throttle = spy
        try:
            summary = distribute_run(self.run, channel="sms")
        finally:
            throttle_mod.throttle = original
        # One throttle call per delivery actually attempted (sent + failed via a
        # real send; no-contact failures short-circuit before the send).
        self.assertGreater(len(calls), 0)
        self.assertTrue(all(c == "sms" for c in calls))
        self.assertLessEqual(len(calls), summary["total"])


class Provider429TestCase(unittest.TestCase):
    def setUp(self):
        self.app = create_app()
        self.ctx = self.app.app_context()
        self.ctx.push()

    def tearDown(self):
        self.ctx.pop()

    def test_hubtel_429_is_labelled_rate_limited(self):
        from app.distribution import channels as channels_mod
        from app.distribution.channels import HubtelSmsSender

        def fake_post(url, *, headers=None, json=None, timeout=30):
            return 429, "too many requests"

        original = channels_mod._http_post
        channels_mod._http_post = fake_post
        self.app.config.update(
            SMS_BACKEND="hubtel", SMS_SENDER_ID="Chrisnat",
            SMS_HUBTEL_CLIENT_ID="cid", SMS_HUBTEL_CLIENT_SECRET="secret",
        )
        try:
            result = HubtelSmsSender().send(OutboundMessage("sms", "+233241234567", "", "hi"))
        finally:
            channels_mod._http_post = original
        self.assertFalse(result.ok)
        self.assertIn("rate limited", result.error)


if __name__ == "__main__":
    unittest.main()
