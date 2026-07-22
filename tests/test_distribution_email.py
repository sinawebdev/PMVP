"""Phase 3, Slice 9 — email improvements.

Branded HTML template with a plain-text fallback, configurable sender
name/reply-to, email validation before send, and validated optional PDF
attachments. Providers stay abstract (get_sender) so the sending service is
swappable without touching business logic.
"""

import os
import unittest

os.environ["SKIP_DOTENV"] = "true"
os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["SEED_DEMO_DATA"] = "true"
os.environ["PERSISTENCE_REQUIRED"] = "false"

from app import create_app, db  # noqa: E402
from app.distribution import channels as channels_mod  # noqa: E402
from app.distribution.channels import (  # noqa: E402
    Attachment,
    OutboundMessage,
    SmtpEmailSender,
    is_valid_email,
)
from app.distribution.render import render_payslip_email  # noqa: E402


class _Obj:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def _item():
    return _Obj(
        id=1, full_name="Ama Mensah", basic_salary=2000, transport_allowance=300,
        housing_allowance=0, overtime_pay=0, other_allowances=0, gross_pay=2300,
        paye=200, ssnit=120, tier_2_pension=0, loan_deduction=0,
        other_deductions=0, total_deductions=320, net_pay=1980,
    )


class EmailValidationTestCase(unittest.TestCase):
    def test_is_valid_email(self):
        self.assertTrue(is_valid_email("worker@example.com"))
        self.assertFalse(is_valid_email("not-an-email"))
        self.assertFalse(is_valid_email("no@domain"))
        self.assertFalse(is_valid_email(""))
        self.assertFalse(is_valid_email(None))


class EmailRenderTestCase(unittest.TestCase):
    def setUp(self):
        self.app = create_app()
        self.ctx = self.app.app_context()
        self.ctx.push()

    def tearDown(self):
        self.ctx.pop()

    def test_branded_html_and_plaintext_fallback(self):
        run = _Obj(month="June", year=2026)
        client = _Obj(name="MSC Ghana Ltd")
        subject, text, html = render_payslip_email(_item(), run, client, link="https://x/p/tok")
        self.assertIn("MSC Ghana Ltd", subject)
        # Plain-text fallback carries the numbers, no HTML.
        self.assertIn("NET PAY", text)
        self.assertNotIn("<", text)
        # HTML is a full branded document.
        self.assertIn("<!DOCTYPE html>", html)
        self.assertIn("1,980", html)
        self.assertIn("View payslip", html)


class SmtpSenderTestCase(unittest.TestCase):
    def setUp(self):
        self.app = create_app()
        self.ctx = self.app.app_context()
        self.ctx.push()

    def tearDown(self):
        self.ctx.pop()

    def test_invalid_recipient_short_circuits_before_connecting(self):
        # SMTP configured, but the recipient is invalid -> clear error, no network.
        self.app.config.update(EMAIL_BACKEND="smtp", SMTP_HOST="smtp.example.com")
        result = SmtpEmailSender().send(
            OutboundMessage("email", "bogus", "Subj", "body")
        )
        self.assertFalse(result.ok)
        self.assertIn("invalid recipient", result.error)

    def test_sender_name_reply_to_and_attachment_are_applied(self):
        # Capture the MIME message by faking the SMTP transport.
        self.app.config.update(
            EMAIL_BACKEND="smtp", SMTP_HOST="smtp.example.com", SMTP_USE_TLS=False,
            EMAIL_SENDER_NAME="Chrisnat Payroll", DEFAULT_FROM_EMAIL="payroll@chrisnat.io",
            EMAIL_REPLY_TO="replies@chrisnat.io",
        )
        captured = {}

        class _FakeSMTP:
            def __init__(self, host, port, timeout=30):
                captured["host"] = host

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def starttls(self):
                pass

            def login(self, *a):
                pass

            def send_message(self, mime):
                captured["mime"] = mime

        original = channels_mod.smtplib.SMTP
        channels_mod.smtplib.SMTP = _FakeSMTP
        try:
            result = SmtpEmailSender().send(
                OutboundMessage(
                    "email", "worker@example.com", "Subj", "body", "<p>body</p>",
                    attachments=[Attachment("payslip.pdf", b"%PDF-1.4 fake", "application/pdf")],
                )
            )
        finally:
            channels_mod.smtplib.SMTP = original

        self.assertTrue(result.ok)
        mime = captured["mime"]
        self.assertIn("Chrisnat Payroll", mime["From"])
        self.assertEqual(mime["Reply-To"], "replies@chrisnat.io")
        # The PDF attachment made it in.
        filenames = [p.get_filename() for p in mime.iter_attachments()]
        self.assertIn("payslip.pdf", filenames)

    def test_oversized_attachment_is_skipped_not_fatal(self):
        self.app.config.update(
            EMAIL_BACKEND="smtp", SMTP_HOST="smtp.example.com", SMTP_USE_TLS=False,
            EMAIL_MAX_ATTACHMENT_BYTES=10,
        )
        captured = {}

        class _FakeSMTP:
            def __init__(self, *a, **k):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def starttls(self):
                pass

            def send_message(self, mime):
                captured["mime"] = mime

        original = channels_mod.smtplib.SMTP
        channels_mod.smtplib.SMTP = _FakeSMTP
        try:
            result = SmtpEmailSender().send(
                OutboundMessage(
                    "email", "worker@example.com", "Subj", "body",
                    attachments=[Attachment("big.pdf", b"x" * 1000)],
                )
            )
        finally:
            channels_mod.smtplib.SMTP = original

        self.assertTrue(result.ok)  # still sent
        self.assertEqual(list(captured["mime"].iter_attachments()), [])  # attachment dropped


if __name__ == "__main__":
    unittest.main()
