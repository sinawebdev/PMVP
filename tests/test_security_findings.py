"""Regression tests for the F1-F9 security remediation pass.

One class per finding. Each pins the *property* the fix establishes rather than
its current implementation, so a refactor stays free but a regression does not:
these are the checks that would have failed before the fix and must keep failing
if it is ever undone.
"""
import logging
import os
import unittest

os.environ["SKIP_DOTENV"] = "true"
os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["PERSISTENCE_REQUIRED"] = "false"
# Deliberately NOT setting AUTO_INIT_DB here. A module-level assignment leaks to
# every other test module for the rest of the session, and turning it off stops
# create_app() initialising the schema — which the seeded suites
# (test_mvp, test_tenant_isolation, ...) depend on. The two tests that need it
# off set it themselves, inside the _EnvGuard save/restore.

from app import create_app  # noqa: E402
from app.distribution.channels import (  # noqa: E402
    ConsoleEmailSender,
    ConsoleSmsSender,
    ConsoleWhatsAppSender,
    OutboundMessage,
)


def _app(**config):
    application = create_app()
    application.config.update(TESTING=True, WTF_CSRF_ENABLED=False, **config)
    return application


class _EnvGuard(unittest.TestCase):
    """Saves and restores the environment variables a test rewrites."""

    ENV_KEYS = (
        "RENDER", "RAILWAY_ENVIRONMENT", "FLASK_ENV", "APP_ENV", "SECRET_KEY",
        "DATABASE_URL", "PERSISTENCE_REQUIRED", "LOG_MESSAGE_BODIES", "AUTO_INIT_DB",
    )

    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in self.ENV_KEYS}

    def tearDown(self):
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


class _CaptureLog(logging.Handler):
    def __init__(self):
        super().__init__()
        self.messages = []

    def emit(self, record):
        self.messages.append(record.getMessage())


class F1MessageBodiesNotLoggedTests(unittest.TestCase):
    """F1 — payslip bodies and raw recipients must never reach the log."""

    SECRET_BODY = "Net pay GHS 4,231.55 for Ama Mensah"
    PHONE = "+233241234567"
    EMAIL = "ama.mensah@example.com"

    def _send_all(self, application):
        """Send one message per console channel and return what was logged.

        Asserts the capture is non-empty before returning. That guard is the
        whole reason this helper exists: the interesting assertions here are
        *negative* ("the body is not in the log"), and a negative assertion over
        an empty string passes no matter what the code does — it would go on
        passing with the fix reverted. So an empty capture must be a failure,
        not a silent success.

        The way it used to go empty was ``migrations/env.py`` calling
        ``fileConfig()``, which disables every pre-existing logger by default —
        so any suite that had already run migrations left ``app``'s logger
        switched off for the rest of the process. That is fixed at the source
        now; the explicit re-enable below stays as cheap insurance, and the
        non-empty assertion stays because it is what would catch the next such
        cause rather than this one.
        """
        handler = _CaptureLog()
        logger = application.logger
        with application.app_context():
            was_disabled, previous_level = logger.disabled, logger.level
            logger.disabled = False
            logger.setLevel(logging.INFO)
            logger.addHandler(handler)
            try:
                ConsoleSmsSender().send(
                    OutboundMessage("sms", self.PHONE, "S", self.SECRET_BODY, item_id=42)
                )
                ConsoleWhatsAppSender().send(
                    OutboundMessage("whatsapp", self.PHONE, "S", self.SECRET_BODY,
                                    item_id=43)
                )
                ConsoleEmailSender().send(
                    OutboundMessage("email", self.EMAIL, "Payslip for Ama Mensah",
                                    self.SECRET_BODY, item_id=44)
                )
            finally:
                logger.removeHandler(handler)
                logger.disabled, logger.level = was_disabled, previous_level
        captured = "\n".join(handler.messages)
        self.assertTrue(
            captured.strip(),
            "captured nothing — the negative assertions below would pass vacuously",
        )
        return captured

    def test_body_recipient_and_subject_are_withheld_by_default(self):
        logged = self._send_all(_app())
        # Positive control first: prove the sends really were logged, so the
        # absences below mean "redacted", not "nothing happened".
        self.assertIn("console-sms", logged)
        self.assertIn("console-email", logged)
        self.assertNotIn(self.SECRET_BODY, logged)
        self.assertNotIn(self.PHONE, logged)
        self.assertNotIn(self.EMAIL, logged)
        self.assertNotIn("Ama Mensah", logged)

    def test_provider_and_item_id_are_still_logged(self):
        logged = self._send_all(_app())
        for expected in ("console-sms", "console-whatsapp", "console-email",
                         "item=42", "item=43", "item=44"):
            self.assertIn(expected, logged)

    def test_recipient_fingerprint_is_stable_and_not_the_address(self):
        from app.distribution.channels import recipient_fingerprint

        with _app().app_context():
            first = recipient_fingerprint(self.PHONE)
            self.assertEqual(first, recipient_fingerprint(f"  {self.PHONE}  "))
            self.assertNotIn(self.PHONE, first)
            self.assertNotEqual(first, recipient_fingerprint("+233209999999"))

    def test_opt_in_flag_restores_bodies_for_local_debugging(self):
        logged = self._send_all(_app(LOG_MESSAGE_BODIES=True))
        self.assertIn(self.SECRET_BODY, logged)


class F1ProductionRefusesBodyLoggingTests(_EnvGuard):
    """F1 — the flag must be impossible to enable on a real deployment."""

    ENV_KEYS = _EnvGuard.ENV_KEYS + ("DISTRIBUTION_WORKER_INLINE",)

    def _production_env(self):
        os.environ["SKIP_DOTENV"] = "true"
        os.environ["FLASK_ENV"] = "production"
        os.environ["DATABASE_URL"] = "postgresql://u:p@localhost/db"
        os.environ["AUTO_INIT_DB"] = "false"
        os.environ["SECRET_KEY"] = "x" * 64
        # Otherwise the app factory starts the inline distribution worker, which
        # immediately tries to reach the (nonexistent) Postgres above and fills
        # the run with connection-refused thread warnings.
        os.environ["DISTRIBUTION_WORKER_INLINE"] = "false"
        os.environ.pop("PERSISTENCE_REQUIRED", None)
        os.environ.pop("RENDER", None)

    def test_production_refuses_to_boot_with_body_logging_enabled(self):
        self._production_env()
        os.environ["LOG_MESSAGE_BODIES"] = "true"
        with self.assertRaises(RuntimeError) as ctx:
            create_app()
        self.assertIn("LOG_MESSAGE_BODIES", str(ctx.exception))

    def test_production_boots_with_the_flag_off_and_keeps_it_off(self):
        self._production_env()
        os.environ["LOG_MESSAGE_BODIES"] = "false"
        self.assertFalse(create_app().config["LOG_MESSAGE_BODIES"])


if __name__ == "__main__":
    unittest.main()
