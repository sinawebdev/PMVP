"""Phase 5 — security hardening (Workstream D2/D3/D4).

Email header-injection defence, and the production SECRET_KEY boot guard.
Webhook fail-closed (D2) is covered in tests/test_distribution_receipts.py.
"""
import os
import unittest
from unittest import mock

os.environ["SKIP_DOTENV"] = "true"
os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["PERSISTENCE_REQUIRED"] = "false"

from app import create_app  # noqa: E402
from app.distribution.channels import (  # noqa: E402
    OutboundMessage,
    SmtpEmailSender,
    _header_safe,
)


class _FakeSMTP:
    captured = {}

    def __init__(self, host, port, timeout=None):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False

    def starttls(self):
        pass

    def login(self, _u, _p):
        pass

    def send_message(self, mime):
        _FakeSMTP.captured["mime"] = mime


class EmailHeaderInjectionTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app()
        self.ctx = self.app.app_context()
        self.ctx.push()

    def tearDown(self):
        self.ctx.pop()

    def test_header_safe_strips_crlf(self):
        self.assertEqual(_header_safe("Payslip\r\nBcc: evil@x.com"),
                         "Payslip  Bcc: evil@x.com")
        self.assertIsNone(_header_safe(None))

    def test_smtp_send_neutralises_subject_injection(self):
        self.app.config.update(
            EMAIL_BACKEND="smtp", SMTP_HOST="smtp.test", SMTP_USE_TLS=False,
            DEFAULT_FROM_EMAIL="from@test.com",
        )
        _FakeSMTP.captured = {}
        with mock.patch("app.distribution.channels.smtplib.SMTP", _FakeSMTP):
            result = SmtpEmailSender().send(OutboundMessage(
                "email", "to@test.com", "Payslip\r\nBcc: evil@x.com", "body",
            ))
        self.assertTrue(result.ok)
        mime = _FakeSMTP.captured["mime"]
        self.assertNotIn("\n", mime["Subject"])
        self.assertNotIn("\r", mime["Subject"])
        self.assertIsNone(mime["Bcc"])  # no header was injected


class SecretKeyBootGuardTests(unittest.TestCase):
    _ENV_KEYS = ["RENDER", "DATABASE_URL", "SECRET_KEY", "FLASK_ENV",
                 "PERSISTENCE_REQUIRED", "AUTO_INIT_DB", "SKIP_DOTENV"]

    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in self._ENV_KEYS}

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_production_refuses_insecure_secret_key(self):
        os.environ["SKIP_DOTENV"] = "true"
        os.environ["RENDER"] = "true"  # -> production
        os.environ["DATABASE_URL"] = "postgresql://u:p@localhost/db"  # passes persistence
        os.environ["AUTO_INIT_DB"] = "false"  # never connects
        os.environ.pop("SECRET_KEY", None)  # -> insecure fallback
        os.environ.pop("FLASK_ENV", None)
        os.environ.pop("PERSISTENCE_REQUIRED", None)
        with self.assertRaises(RuntimeError) as ctx:
            create_app()
        self.assertIn("SECRET_KEY", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
