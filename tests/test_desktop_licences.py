"""Desktop trial licences: signed exactly as the desktop verifies, issued from an
access request, recorded, emailed; and the Apps Script mail relay sender."""

import base64
import json
import os
import unittest
import urllib.error
from datetime import date
from unittest import mock

os.environ["SKIP_DOTENV"] = "true"
os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["PERSISTENCE_REQUIRED"] = "false"

from app import access_requests, create_app, db, licensing  # noqa: E402
from app.distribution import channels  # noqa: E402
from app.distribution.channels import Attachment, OutboundMessage, SendResult  # noqa: E402
from app.models import AccessRequest, DesktopLicence, User  # noqa: E402

# A throwaway key: 32 bytes of 0x07. Never a real key.
TEST_KEY = base64.urlsafe_b64encode(bytes([7] * 32)).decode().rstrip("=")
GOLDEN_TERMS = {
    "install_id": "acme-trading-x1y2z3", "license_expiry": "2027-01-31",
    "firm_name": "Acme Trading Ltd", "license_id": "TR-2026-ABCDEF", "plan": "Trial",
    "issued": "2026-11-06", "max_employees": 500, "features": list(licensing.TRIAL_FEATURES),
}
# Produced by payrolla-desktop's own desktop.licensing.signing_payload / sign()
# on 2026-11-06. If either changes here, desktop installs will refuse the
# licences this server issues.
GOLDEN_PAYLOAD = (
    b"payrolla-desktop|v2|acme-trading-x1y2z3|2027-01-31|Acme Trading Ltd|TR-2026-ABCDEF|Trial|"
    b"2026-11-06|500|approvals,audit_log,bank_listing,employee_management,exports,notifications,"
    b"payroll,payslips,statutory_filings"
)
GOLDEN_SIGNATURE = ("eHCdQiqYlfnXlM6zCtcelrsC1GP13X2ns-6txHsfEaorijYOMjRvSJTTRmkZ28ldmOkga6L75jo1meS8qJ4NAw")


class SignatureFormatTests(unittest.TestCase):
    def test_payload_and_signature_match_the_desktop_byte_for_byte(self):
        self.assertEqual(licensing.signing_payload(GOLDEN_TERMS), GOLDEN_PAYLOAD)
        self.assertEqual(licensing.sign(TEST_KEY, GOLDEN_TERMS), GOLDEN_SIGNATURE)

    def test_issued_trial_verifies_and_carries_trial_terms(self):
        doc = licensing.issue_trial(TEST_KEY, "Volta | Logistics\n", date(2027, 1, 31), 500,
                                    issued=date(2026, 11, 6))
        self.assertTrue(licensing.verify(licensing.public_key_of(TEST_KEY), doc))
        self.assertEqual(doc["plan"], "Trial")
        self.assertEqual(doc["firm_name"], "Volta Logistics")
        self.assertTrue(doc["install_id"].startswith("volta-logistics-"))
        self.assertTrue(doc["license_id"].startswith("TR-2026-"))
        self.assertEqual(doc["license_expiry"], "2027-01-31")

    def test_tampered_terms_do_not_verify(self):
        doc = licensing.issue_trial(TEST_KEY, "Volta", date(2027, 1, 31), 500, issued=date(2026, 11, 6))
        doc["license_expiry"] = "2099-01-01"
        self.assertFalse(licensing.verify(licensing.public_key_of(TEST_KEY), doc))

    def test_expiry_must_follow_issue(self):
        with self.assertRaises(licensing.LicenseError):
            licensing.issue_trial(TEST_KEY, "Volta", date(2026, 11, 1), 500, issued=date(2026, 11, 6))


class _Capture:
    def __init__(self, ok=True, error=None):
        self.sent, self.ok, self.error = [], ok, error

    def send(self, message):
        self.sent.append(message)
        return SendResult(ok=self.ok, provider="test", error=self.error)


class IssueFromRequestTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app()
        self.app.config.update(TESTING=True, TRIAL_LICENCE_SIGNING_KEY=TEST_KEY,
                               DESKTOP_DOWNLOAD_URL="https://example.test/Payrolla-Desktop-Setup.exe")
        self.client = self.app.test_client()
        self.ctx = self.app.app_context()
        self.ctx.push()
        access_requests.reset()
        admin = User.query.filter(User.role == "admin", User.client_company_id.is_(None)).first()
        self.client.post("/login", data={"email": admin.email, "password": "password123"})

    def tearDown(self):
        access_requests.reset()
        self.ctx.pop()

    def _request(self, interest="desktop", company="Volta Logistics"):
        entry = AccessRequest(company_name=company, contact_name="Kofi Asante",
                              email="kofi@volta.test", interest=interest, status="new")
        db.session.add(entry)
        db.session.commit()
        return entry

    def _issue(self, entry, sender):
        with mock.patch("app.desktop_licences.get_sender", return_value=sender):
            return self.client.post(f"/clients/requests/{entry.id}/licence")

    def test_desktop_request_gets_one_primary_action_to_send_the_licence(self):
        entry = self._request()
        html = self.client.get(f"/clients/requests/{entry.id}").get_data(as_text=True)
        self.assertIn("Approve and send trial licence", html)
        self.assertEqual(html.count("btn btn-primary"), 1)

    def test_issuing_records_approves_and_emails_a_verifiable_licence(self):
        entry = self._request()
        sender = _Capture()
        resp = self._issue(entry, sender)
        self.assertEqual(resp.status_code, 302)

        record = DesktopLicence.query.one()
        self.assertEqual(record.access_request_id, entry.id)
        self.assertIsNotNone(record.emailed_at)
        self.assertEqual(db.session.get(AccessRequest, entry.id).status, "approved")

        message = sender.sent[0]
        self.assertEqual(message.recipient, "kofi@volta.test")
        self.assertIn("https://example.test/Payrolla-Desktop-Setup.exe", message.body_text)
        self.assertIn("31 January 2027", message.body_text)
        attachment = message.attachments[0]
        self.assertEqual(attachment.filename, f"{record.install_id}.license.json")
        document = json.loads(attachment.content)
        self.assertTrue(licensing.verify(licensing.public_key_of(TEST_KEY), document))

    def test_second_issue_is_refused_and_resend_reuses_the_same_licence(self):
        entry = self._request()
        self._issue(entry, _Capture())
        self._issue(entry, _Capture())
        self.assertEqual(DesktopLicence.query.count(), 1)

        record = DesktopLicence.query.one()
        sender = _Capture()
        with mock.patch("app.desktop_licences.get_sender", return_value=sender):
            self.client.post(f"/clients/licences/{record.id}/resend")
        self.assertEqual(json.loads(sender.sent[0].attachments[0].content)["license_id"], record.license_id)

    def test_failed_email_is_recorded_and_shown(self):
        entry = self._request()
        self._issue(entry, _Capture(ok=False, error="daily email quota reached"))
        record = DesktopLicence.query.one()
        self.assertIsNone(record.emailed_at)
        self.assertEqual(record.email_error, "daily email quota reached")
        ledger = self.client.get("/clients/licences").get_data(as_text=True)
        self.assertIn(record.license_id, ledger)
        self.assertIn("Not sent", ledger)

    def test_both_keeps_the_request_open_for_the_web_company(self):
        entry = self._request(interest="both")
        html = self.client.get(f"/clients/requests/{entry.id}").get_data(as_text=True)
        self.assertIn("Approve and create company", html)
        self.assertIn("Send desktop trial licence", html)
        self._issue(entry, _Capture())
        self.assertEqual(db.session.get(AccessRequest, entry.id).status, "new")

    def test_web_only_request_and_unconfigured_server_issue_nothing(self):
        web = self._request(interest="web")
        self._issue(web, _Capture())
        self.app.config["TRIAL_LICENCE_SIGNING_KEY"] = ""
        desktop = self._request(company="Other Co")
        html = self.client.get(f"/clients/requests/{desktop.id}").get_data(as_text=True)
        self.assertNotIn("Approve and send trial licence", html)
        self._issue(desktop, _Capture())
        self.assertEqual(DesktopLicence.query.count(), 0)


class LandingOfferTests(unittest.TestCase):
    """Signed out: the landing page redirects anyone already signed in."""

    def setUp(self):
        self.app = create_app()
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()
        self.ctx = self.app.app_context()
        self.ctx.push()

    def tearDown(self):
        self.ctx.pop()

    def test_landing_advertises_the_offer_until_it_ends(self):
        self.app.config["TRIAL_OFFER_ENDS"] = "2999-12-31"
        self.assertIn("Free for any business until 31 December 2999",
                      self.client.get("/").get_data(as_text=True))
        self.app.config["TRIAL_OFFER_ENDS"] = "2000-01-01"
        self.assertNotIn("Free for any business", self.client.get("/").get_data(as_text=True))


class AppsScriptSenderTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app()
        self.app.config.update(EMAIL_BACKEND="apps_script", APPS_SCRIPT_MAIL_URL="https://relay.test/exec",
                               APPS_SCRIPT_MAIL_SECRET="s3cret", EMAIL_SENDER_NAME="Payrolla")
        self.ctx = self.app.app_context()
        self.ctx.push()
        self.message = OutboundMessage(channel="email", recipient="ama@example.test", subject="Hi\nthere",
                                       body_text="Hello", attachments=[Attachment("x.json", b"{}", "application/json")])

    def tearDown(self):
        self.ctx.pop()

    def test_backend_selects_the_relay_and_counts_as_live(self):
        self.assertIsInstance(channels.get_sender("email"), channels.AppsScriptEmailSender)
        self.assertNotIn("email", channels.simulated_channels())

    def test_posts_secret_message_and_base64_attachment(self):
        with mock.patch("app.distribution.channels._http_post",
                        return_value=(200, '{"ok": true, "remaining": 97}')) as post:
            result = channels.get_sender("email").send(self.message)
        self.assertTrue(result.ok)
        url, payload = post.call_args.args[0], post.call_args.kwargs["json"]
        self.assertEqual(url, "https://relay.test/exec")
        self.assertEqual(payload["secret"], "s3cret")
        self.assertEqual(payload["subject"], "Hi there")
        self.assertEqual(base64.b64decode(payload["attachments"][0]["content"]), b"{}")

    def test_refusal_and_non_json_answers_fail(self):
        sender = channels.get_sender("email")
        with mock.patch("app.distribution.channels._http_post",
                        return_value=(200, '{"ok": false, "error": "daily email quota reached"}')):
            result = sender.send(self.message)
        self.assertFalse(result.ok)
        self.assertIn("quota", result.error)
        with mock.patch("app.distribution.channels._http_post",
                        return_value=(200, "<html>Sign in</html>")):
            self.assertFalse(sender.send(self.message).ok)

    def test_transport_failure_is_ambiguous_not_raised(self):
        with mock.patch("app.distribution.channels._http_post",
                        side_effect=urllib.error.URLError("timed out")):
            result = channels.get_sender("email").send(self.message)
        self.assertFalse(result.ok)
        self.assertTrue(result.ambiguous)

    def test_missing_config_fails_cleanly(self):
        self.app.config["APPS_SCRIPT_MAIL_URL"] = None
        self.assertFalse(channels.get_sender("email").send(self.message).ok)


if __name__ == "__main__":
    unittest.main()
