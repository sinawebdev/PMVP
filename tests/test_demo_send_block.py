"""A demo company's payslips never reach a real provider (app.distribution.demo).

On the live site SMS (SasuSync) and email (the Apps Script relay) are real, and
the demo's made-up numbers may belong to real people. So these tests switch both
live backends on, make every network call fail the test, and send demo payslips
every way the app can: a queued batch on auto, SMS and email, the retry sweep,
and _attempt_send handed a live sender directly. A real company's run in the
same setup still goes to its provider.
"""

import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

os.environ["SKIP_DOTENV"] = "true"
os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["SEED_DEMO_DATA"] = "true"
os.environ["PERSISTENCE_REQUIRED"] = "false"

from app import create_app, db  # noqa: E402
from app.demo_workspace import set_up, status  # noqa: E402
from app.distribution import service  # noqa: E402
from app.distribution.channels import SendResult, delivery_is_simulated  # noqa: E402
from app.distribution.queue import enqueue_distribution, process_all_queued, process_due_retries  # noqa: E402
from app.models import (  # noqa: E402
    DELIVERY_FAILED,
    DELIVERY_SENT,
    ClientCompany,
    PayrollItem,
    PayrollRun,
    PayslipDelivery,
    User,
)
from app.payroll_status import APPROVED  # noqa: E402

LOGINS = {
    "bureau_admin": ("Bea Admin", "bureau@demo-send.example.com"),
    "client_admin": ("Cal Admin", "client@demo-send.example.com"),
    "client_preparer": ("Pat Preparer", "preparer@demo-send.example.com"),
}
LIVE = {
    "SMS_BACKEND": "sasusync", "SASUSYNC_API_KEY": "test-key", "SMS_SENDER_ID": "PAYROLLA",
    "SASUSYNC_BASE_URL": "https://sasusync.invalid", "EMAIL_BACKEND": "apps_script",
    "APPS_SCRIPT_MAIL_URL": "https://script.invalid/exec", "APPS_SCRIPT_MAIL_SECRET": "test-secret",
    "PUBLIC_BASE_URL": "https://payroll.example.com",
}


class Recorder:
    provider = "recorder"

    def __init__(self):
        self.sent = []

    def send(self, message):
        self.sent.append(message)
        return SendResult(ok=True, provider=self.provider)


class DemoSendBlockTestCase(unittest.TestCase):
    def setUp(self):
        self.app = create_app()
        self.app.config.update(TESTING=True, **LIVE)
        self.ctx = self.app.app_context()
        self.ctx.push()
        self.network = mock.patch("urllib.request.urlopen", side_effect=AssertionError("network call"))
        self.network_mock = self.network.start()
        self.addCleanup(self.network.stop)
        smtp = mock.patch("smtplib.SMTP", side_effect=AssertionError("smtp call"))
        smtp.start()
        self.addCleanup(smtp.stop)
        self.staff = User.query.filter_by(email="admin@payrolla.com").one()
        set_up(LOGINS)
        portal_id = status()["portal_company_id"]
        self.run = PayrollRun.query.filter_by(client_company_id=portal_id, status=APPROVED).one()
        self.run_id = self.run.id

    def tearDown(self):
        db.session.remove()
        self.ctx.pop()

    def _deliveries(self, run_id=None):
        return PayslipDelivery.query.filter_by(payroll_run_id=run_id or self.run_id).all()

    def test_the_live_backends_are_really_on(self):
        self.assertFalse(delivery_is_simulated())

    def test_queued_sends_on_every_channel_record_but_never_call_out(self):
        for channel in ("auto", "sms", "email"):
            with self.subTest(channel=channel):
                PayslipDelivery.query.filter_by(payroll_run_id=self.run_id).delete()
                db.session.commit()
                run = db.session.get(PayrollRun, self.run_id)
                enqueue_distribution(run, channel, False, self.staff)
                process_all_queued()
                deliveries = self._deliveries()
                sendable = [d for d in deliveries if d.recipient]
                self.assertTrue(sendable)
                self.assertTrue(all(d.status == DELIVERY_SENT for d in sendable))
                self.assertTrue(all(d.provider.startswith("demo-") for d in sendable), {d.provider for d in sendable})
        self.network_mock.assert_not_called()

    def test_the_retry_sweep_uses_the_demo_sender(self):
        item = PayrollItem.query.filter_by(payroll_run_id=self.run_id).filter(PayrollItem.email.isnot(None)).first()
        db.session.add(PayslipDelivery(
            payroll_item_id=item.id, payroll_run_id=self.run_id, channel="email", recipient=item.email,
            status=DELIVERY_FAILED, attempts=1, next_retry_at=datetime.now(timezone.utc) - timedelta(minutes=1),
        ))
        db.session.commit()
        process_due_retries()
        delivery = PayslipDelivery.query.filter_by(payroll_item_id=item.id, channel="email").one()
        self.assertEqual((delivery.status, delivery.provider), (DELIVERY_SENT, "demo-email"))
        self.network_mock.assert_not_called()

    def test_a_live_sender_handed_in_directly_is_never_used(self):
        run = db.session.get(PayrollRun, self.run_id)
        item = run.items[0]
        delivery = service._insert_pending(item, run, "sms")
        recorder = Recorder()
        status_after = service._attempt_send(delivery, item, run, run.client_company, "sms", recorder, 3, 1)
        self.assertEqual(status_after, DELIVERY_SENT)
        self.assertEqual(recorder.sent, [])
        self.assertEqual(db.session.get(PayslipDelivery, delivery.id).provider, "demo-sms")

    def test_a_real_company_still_reaches_its_provider(self):
        contoso = ClientCompany.query.filter_by(name="Contoso Ltd").one()
        run = PayrollRun(month="November", year=2026, status=APPROVED, client_company_id=contoso.id)
        db.session.add(run)
        db.session.flush()
        db.session.add(PayrollItem(payroll_run_id=run.id, staff_id="C1", full_name="Real Worker",
                                   email="worker@contoso-test.example.com", net_pay=1000))
        db.session.commit()
        recorder = Recorder()
        with mock.patch.object(service, "get_sender", return_value=recorder):
            summary = service.distribute_run(run, channel="email")
        self.assertEqual(summary["sent"], 1)
        self.assertEqual([m.recipient for m in recorder.sent], ["worker@contoso-test.example.com"])

    def test_the_send_screens_say_demo_payslips_are_not_sent(self):
        client_admin = User.query.filter_by(email="client@demo-send.example.com").one()
        client_admin.set_password("a-long-password-1")
        db.session.commit()
        web = self.app.test_client()
        web.post("/login", data={"email": client_admin.email, "password": "a-long-password-1"})
        html = web.get(f"/company/runs/{self.run_id}/distribute").get_data(as_text=True)
        self.assertIn("Demo company: payslips are never sent.", html)
        web.get("/logout")
        web.post("/login", data={"email": "admin@payrolla.com", "password": "password123"})
        html = web.get(f"/distribution/run/{self.run_id}").get_data(as_text=True)
        self.assertIn("Demo company: payslips are never sent.", html)


if __name__ == "__main__":
    unittest.main()
