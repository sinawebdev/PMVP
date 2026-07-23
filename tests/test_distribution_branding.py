"""Phase 4, Slice 5 — tenant-specific branding packs.

Each ClientCompany can brand its payslip emails (name, accent colour, sender
name, reply-to); unset fields fall back to the global config. A client_admin
edits their own pack; a preparer cannot; tenants are isolated.
"""

import os
import unittest

os.environ["SKIP_DOTENV"] = "true"
os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["SEED_DEMO_DATA"] = "true"
os.environ["PERSISTENCE_REQUIRED"] = "false"

from app import create_app, db  # noqa: E402
from app.distribution import channels as channels_mod  # noqa: E402
from app.distribution.channels import OutboundMessage, SmtpEmailSender  # noqa: E402
from app.distribution.render import render_payslip_email  # noqa: E402
from app.models import ClientCompany, PayrollRun, User  # noqa: E402


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


class BrandingRenderTestCase(unittest.TestCase):
    def setUp(self):
        self.app = create_app()
        self.ctx = self.app.app_context()
        self.ctx.push()

    def tearDown(self):
        self.ctx.pop()

    def test_tenant_brand_overrides_global(self):
        run = _Obj(month="June", year=2026)
        branded = _Obj(name="MSC Ghana Ltd", brand_name="MSC Payroll", brand_color="#123456")
        _, _, html = render_payslip_email(_item(), run, branded, link="https://x/p/t")
        self.assertIn("MSC Payroll", html)
        self.assertIn("#123456", html)

    def test_falls_back_to_global_when_unset(self):
        run = _Obj(month="June", year=2026)
        plain = _Obj(name="MSC Ghana Ltd", brand_name=None, brand_color=None)
        _, _, html = render_payslip_email(_item(), run, plain, link="https://x/p/t")
        # Default accent from config (Payrolla Deep Teal).
        self.assertIn("#0D4D4D", html)


class BrandingSenderTestCase(unittest.TestCase):
    def setUp(self):
        self.app = create_app()
        self.ctx = self.app.app_context()
        self.ctx.push()

    def tearDown(self):
        self.ctx.pop()

    def test_per_message_from_name_and_reply_to_win(self):
        self.app.config.update(
            EMAIL_BACKEND="smtp", SMTP_HOST="smtp.example.com", SMTP_USE_TLS=False,
            EMAIL_SENDER_NAME="Global Name", DEFAULT_FROM_EMAIL="payroll@chrisnat.io",
            EMAIL_REPLY_TO="global@chrisnat.io",
        )
        captured = {}

        class _FakeSMTP:
            def __init__(self, *a, **k):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def send_message(self, mime):
                captured["mime"] = mime

        original = channels_mod.smtplib.SMTP
        channels_mod.smtplib.SMTP = _FakeSMTP
        try:
            SmtpEmailSender().send(
                OutboundMessage(
                    "email", "worker@example.com", "Subj", "body", "<p>b</p>",
                    from_name="MSC Payroll", reply_to="hr@msc.example",
                )
            )
        finally:
            channels_mod.smtplib.SMTP = original
        mime = captured["mime"]
        self.assertIn("MSC Payroll", mime["From"])
        self.assertEqual(mime["Reply-To"], "hr@msc.example")


class BrandingRouteTestCase(unittest.TestCase):
    def setUp(self):
        self.app = create_app()
        self.app.config["TESTING"] = True
        self.http = self.app.test_client()
        self.ctx = self.app.app_context()
        self.ctx.push()
        self.msc = User.query.filter_by(email="admin@msc.demo").first()
        self.company_id = self.msc.client_company_id
        self.preparer = User(
            name="MSC Preparer", email="preparer2@msc.demo",
            role="client_preparer", client_company_id=self.company_id,
        )
        self.preparer.set_password("password123")
        db.session.add(self.preparer)
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        self.ctx.pop()

    def _login(self, email):
        self.http.post("/login", data={"email": email, "password": "password123"})

    def test_admin_saves_branding(self):
        self._login("admin@msc.demo")
        resp = self.http.post(
            "/company/branding",
            data={"brand_name": "MSC Payroll", "brand_color": "#123456",
                  "email_from_name": "MSC HR", "email_reply_to": "hr@msc.example"},
            follow_redirects=True,
        )
        self.assertEqual(resp.status_code, 200)
        company = db.session.get(ClientCompany, self.company_id)
        self.assertEqual(company.brand_name, "MSC Payroll")
        self.assertEqual(company.brand_color, "#123456")
        self.assertEqual(company.email_reply_to, "hr@msc.example")

    def test_invalid_color_is_rejected(self):
        self._login("admin@msc.demo")
        self.http.post(
            "/company/branding",
            data={"brand_color": "notacolor"}, follow_redirects=True,
        )
        company = db.session.get(ClientCompany, self.company_id)
        self.assertIsNone(company.brand_color)

    def test_invalid_reply_to_is_rejected(self):
        self._login("admin@msc.demo")
        self.http.post(
            "/company/branding",
            data={"email_reply_to": "not-an-email"}, follow_redirects=True,
        )
        company = db.session.get(ClientCompany, self.company_id)
        self.assertIsNone(company.email_reply_to)

    def test_preparer_cannot_edit_branding(self):
        self._login("preparer2@msc.demo")
        resp = self.http.post(
            "/company/branding", data={"brand_name": "Nope"}, follow_redirects=False
        )
        self.assertEqual(resp.status_code, 302)
        company = db.session.get(ClientCompany, self.company_id)
        self.assertIsNone(company.brand_name)


if __name__ == "__main__":
    unittest.main()
