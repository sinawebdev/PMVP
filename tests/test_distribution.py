import os
import unittest

os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["SEED_DEMO_DATA"] = "true"

from app import create_app, db
from app.models import DELIVERY_FAILED, DELIVERY_SENT, PayrollRun, PayslipDelivery
from app.distribution import channels as channels_mod
from app.distribution.channels import OutboundMessage, HubtelSmsSender
from app.distribution.render import render_payslip_text
from app.distribution.service import distribute_run, resolve_channel
from app.distribution.tokens import (
    issue_payslip_token,
    public_payslip_url,
    verify_payslip_token,
)


class _Obj:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class DistributionTestCase(unittest.TestCase):
    def setUp(self):
        self.app = create_app()
        self.app.config["TESTING"] = True
        self.ctx = self.app.app_context()
        self.ctx.push()
        # Seeded demo data includes one Approved payroll run with items linked to employees.
        self.run = PayrollRun.query.filter_by(status="Approved").first()

    def tearDown(self):
        db.session.remove()
        self.ctx.pop()

    # --- pure units -------------------------------------------------------

    def test_render_contains_net_and_breakdown(self):
        item = _Obj(full_name="Ama Mensah", basic_salary=2000, transport_allowance=300,
                    housing_allowance=0, overtime_pay=0, other_allowances=0, gross_pay=2300,
                    paye=200, ssnit=120, tier_2_pension=0, loan_deduction=0,
                    other_deductions=0, total_deductions=320, net_pay=1980)
        run = _Obj(month="June", year=2026)
        client = _Obj(name="MSC Ghana Ltd")
        text = render_payslip_text(item, run, client)
        self.assertIn("MSC Ghana Ltd", text)
        self.assertIn("Ama Mensah", text)
        self.assertIn("NET PAY", text)
        self.assertIn("1,980", text)
        self.assertNotIn("<", text)  # plain text

    def test_resolve_channel_falls_back_to_available_contact(self):
        # Worker prefers email but only has a phone -> routes to SMS.
        emp = _Obj(preferred_channel="email", phone="0241234567", momo_number=None, email=None)
        item = _Obj(employee=emp, momo_number=None, phone=None, email=None)
        self.assertEqual(resolve_channel(item), "sms")
        # Has email and prefers email -> email.
        emp2 = _Obj(preferred_channel="email", phone=None, momo_number=None, email="a@e.com")
        item2 = _Obj(employee=emp2, momo_number=None, email="a@e.com")
        self.assertEqual(resolve_channel(item2), "email")

    def test_hubtel_request_built_without_network(self):
        captured = {}

        def fake_post(url, *, headers=None, json=None, timeout=30):
            captured["url"] = url
            captured["headers"] = headers
            captured["json"] = json
            return 201, '{"status":"ok"}'

        original = channels_mod._http_post
        channels_mod._http_post = fake_post
        try:
            self.app.config.update(
                SMS_BACKEND="hubtel", SMS_SENDER_ID="Chrisnat",
                SMS_HUBTEL_CLIENT_ID="cid", SMS_HUBTEL_CLIENT_SECRET="secret",
            )
            result = HubtelSmsSender().send(
                OutboundMessage("sms", "+233241234567", "", "hello")
            )
        finally:
            channels_mod._http_post = original
        self.assertTrue(result.ok)
        self.assertEqual(captured["json"], {"From": "Chrisnat", "To": "+233241234567", "Content": "hello"})
        self.assertTrue(captured["headers"]["Authorization"].startswith("Basic "))

    # --- DB-backed end to end (console backends) --------------------------

    def test_distribute_seeded_run_records_sent_deliveries(self):
        self.assertIsNotNone(self.run, "expected a seeded Approved payroll run")
        summary = distribute_run(self.run, channel="auto")
        self.assertEqual(summary["total"], len(self.run.items))
        self.assertGreater(summary["sent"], 0)
        self.assertEqual(summary["failed"], 0)
        rows = PayslipDelivery.query.filter_by(payroll_run_id=self.run.id).all()
        self.assertEqual(len(rows), len(self.run.items))
        self.assertTrue(all(r.status == DELIVERY_SENT for r in rows))

    def test_send_skips_already_sent(self):
        distribute_run(self.run, channel="auto")
        again = distribute_run(self.run, channel="auto")
        self.assertEqual(again["sent"], 0)
        self.assertEqual(again["skipped"], again["total"])

    def test_failed_then_resend_succeeds(self):
        item = self.run.items[0]
        # Strip all contacts -> a failed delivery, not a crash.
        item.momo_number = None
        if item.employee:
            item.employee.phone = None
            item.employee.momo_number = None
            item.employee.email = None
        db.session.commit()

        first = distribute_run(self.run, channel="sms")
        self.assertGreaterEqual(first["failed"], 1)

        failed = PayslipDelivery.query.filter_by(
            payroll_item_id=item.id, status=DELIVERY_FAILED
        ).first()
        self.assertIsNotNone(failed)
        attempts_before = failed.attempts

        # Give the worker a phone, resend only failures.
        item.momo_number = "0241234567"
        db.session.commit()
        resend = distribute_run(self.run, channel="sms", only_failed=True)
        self.assertEqual(resend["sent"], 1)
        refreshed = db.session.get(PayslipDelivery, failed.id)
        self.assertEqual(refreshed.status, DELIVERY_SENT)
        self.assertEqual(refreshed.attempts, attempts_before + 1)


    # --- no-login payslip link (Phase A) ----------------------------------

    def test_payslip_token_roundtrip_and_tamper(self):
        token = issue_payslip_token(4242)
        self.assertEqual(verify_payslip_token(token), 4242)
        # Tampered or garbage tokens never resolve.
        self.assertIsNone(verify_payslip_token(token + "x"))
        self.assertIsNone(verify_payslip_token("not-a-real-token"))

    def test_public_payslip_url_uses_configured_base(self):
        self.app.config["PUBLIC_BASE_URL"] = "https://pay.example.com"
        url = public_payslip_url(7)
        self.assertTrue(url.startswith("https://pay.example.com/p/"))
        token = url.rsplit("/p/", 1)[1]
        self.assertEqual(verify_payslip_token(token), 7)

    def test_public_payslip_page_no_login(self):
        item = self.run.items[0]
        token = issue_payslip_token(item.id)
        client = self.app.test_client()
        ok = client.get(f"/p/{token}")
        self.assertEqual(ok.status_code, 200)
        self.assertIn(b"Net pay", ok.data)
        # A bad token shows the friendly expired page, not the payslip.
        bad = client.get("/p/garbage-token")
        self.assertEqual(bad.status_code, 404)
        self.assertIn(b"no longer valid", bad.data)


if __name__ == "__main__":
    unittest.main()
