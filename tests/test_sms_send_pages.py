"""SMS Phase 3: the confirm step, and how a send's state reads on both send pages.

An SMS or auto send stops at a confirm step that says who it reaches (to a
phone, to a MoMo number, and by email or WhatsApp on auto) and who it cannot
(no contact, invalid number), with a credit estimate, before anything is
queued. Pressing its button posts back with confirmed=1 and the same nonce,
so a double press is still one batch. Email and WhatsApp sends are unchanged.
Bulk Distribute on the runs list stops at the same step, summed over its runs.

On the status pages an `unknown` row reads differently by audience: the
operator is told to check SasuSync and mark it; the company is told Payrolla is
checking. Phone numbers are masked on every operator screen.
"""
import os
import re
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

os.environ["SKIP_DOTENV"] = "true"
os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["SEED_DEMO_DATA"] = "true"
os.environ["PERSISTENCE_REQUIRED"] = "false"

from app import create_app, db  # noqa: E402
from app.models import (  # noqa: E402
    BATCH_COMPLETED,
    BATCH_QUEUED,
    BATCH_SCHEDULED,
    DistributionBatch,
    PayrollRun,
    PayslipDelivery,
    User,
)


class _PagesCase(unittest.TestCase):
    def setUp(self):
        self.app = create_app()
        self.app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
        self.http = self.app.test_client()
        self.ctx = self.app.app_context()
        self.ctx.push()
        tenant = User.query.filter_by(email="admin@msc.com").first()
        self.run = PayrollRun.query.filter_by(
            client_company_id=tenant.client_company_id, status="Approved"
        ).first()
        self.items = list(self.run.items)
        self.assertGreaterEqual(len(self.items), 3, "expected a seeded run of 3+ workers")

    def tearDown(self):
        db.session.remove()
        self.ctx.pop()

    def login(self, email):
        response = self.http.post("/login", data={"email": email, "password": "password123"})
        self.assertEqual(response.status_code, 302)

    def contacts(self, item, phone=None, momo=None, row_momo=None, email=None):
        item.employee.phone, item.employee.momo_number = phone, momo
        item.employee.email, item.email = email, None
        item.momo_number = row_momo
        db.session.commit()

    def batches(self):
        return DistributionBatch.query.filter_by(payroll_run_id=self.run.id).all()

    def row(self, item, status, channel="sms", **extra):
        delivery = PayslipDelivery(payroll_item_id=item.id, payroll_run_id=self.run.id,
                                   channel=channel, status=status, attempts=1, **extra)
        db.session.add(delivery)
        db.session.commit()
        return delivery

    @staticmethod
    def figure(html, label):
        match = re.search(rf"<dt>{re.escape(label)}</dt><dd>(\d+)</dd>", html)
        return int(match.group(1)) if match else None


class OperatorConfirmTests(_PagesCase):
    def setUp(self):
        super().setUp()
        self.login("admin@payrolla.com")
        self.base = f"/distribution/run/{self.run.id}"

    def test_an_sms_send_stops_at_the_confirm_step_and_queues_nothing(self):
        response = self.http.post(f"{self.base}/send", data={"channel": "sms", "nonce": "n1"})
        self.assertEqual(response.status_code, 302)
        location = response.headers["Location"]
        self.assertIn(f"{self.base}/confirm?", location)
        for part in ("channel=sms", "action=send", "nonce=n1"):
            self.assertIn(part, location)
        self.assertEqual(self.batches(), [])

    def test_the_confirm_step_counts_who_it_reaches_and_who_it_cannot(self):
        self.contacts(self.items[0])                                # nothing
        self.contacts(self.items[1], phone="0302123456")            # a landline
        self.contacts(self.items[2], momo="241234567")              # MoMo, 0 dropped
        html = self.http.get(f"{self.base}/confirm?channel=sms&action=send&nonce=n1").get_data(as_text=True)
        others = len(self.items) - 3  # any further seeded workers keep their phones
        self.assertEqual(
            [self.figure(html, label) for label in
             ("To a phone", "To a MoMo number", "No contact", "Invalid number")],
            [others, 1, 1, 1],
        )
        credits = others + 1
        self.assertIn(f"About {credits} SMS credit{'' if credits == 1 else 's'}:", html)
        self.assertIn("Can't be reached (2)", html)  # template text, so not escaped
        self.assertIn(self.items[0].staff_id, html)
        self.assertIn("No phone or MoMo number on record", html)
        self.assertIn("isn&#39;t a valid Ghana mobile number", html)
        self.assertIn(f"Send to {credits} worker", html)
        self.assertIn('name="confirmed" value="1"', html)
        self.assertIn('name="nonce" value="n1"', html)
        # The one decision on the screen: no second send control beside it.
        self.assertEqual(html.count('class="btn primary"'), 1)
        self.assertNotIn("Send now", html)

    def test_confirming_queues_one_batch_however_often_it_is_pressed(self):
        for _ in range(2):
            self.http.post(f"{self.base}/send",
                           data={"channel": "sms", "nonce": "n1", "confirmed": "1"})
        batches = self.batches()
        self.assertEqual(len(batches), 1)
        self.assertEqual((batches[0].channel, batches[0].status), ("sms", BATCH_QUEUED))

    def test_email_and_whatsapp_queue_without_a_confirm_step(self):
        response = self.http.post(f"{self.base}/send", data={"channel": "email", "nonce": "e1"})
        self.assertNotIn("/confirm", response.headers["Location"])
        self.assertEqual(len(self.batches()), 1)

    def test_auto_counts_email_recipients_too(self):
        self.contacts(self.items[0], email="worker@example.test")
        html = self.http.get(f"{self.base}/confirm?channel=auto&action=send").get_data(as_text=True)
        self.assertEqual(self.figure(html, "By email"), 1)
        self.assertIn("Check this send", html)

    def test_already_sent_payslips_are_left_out(self):
        self.row(self.items[0], "sent")
        self.row(self.items[1], "unknown")
        html = self.http.get(f"{self.base}/confirm?channel=sms&action=send").get_data(as_text=True)
        self.assertIn("2 payslips are left out: already sent, sending, or unconfirmed.", html)
        self.assertIn(f"Send to {len(self.items) - 2} worker", html)

    def test_a_resend_counts_failed_payslips_only(self):
        self.row(self.items[0], "failed", error="no contact on roster for sms")
        response = self.http.post(f"{self.base}/resend-failed", data={"channel": "sms", "nonce": "r1"})
        self.assertIn("action=resend", response.headers["Location"])
        html = self.http.get(response.headers["Location"]).get_data(as_text=True)
        self.assertIn("Resend to 1 worker", html)
        self.assertIn(f"{len(self.items) - 1} payslips are left out: not failed.", html)
        self.assertIn(f'action="{self.base}/resend-failed"', html)

    def test_a_schedule_carries_its_time_through_the_step(self):
        when = (datetime.now(timezone.utc) + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M")
        response = self.http.post(f"{self.base}/schedule",
                                  data={"channel": "sms", "nonce": "s1", "scheduled_for": when})
        self.assertIn("action=schedule", response.headers["Location"])
        html = self.http.get(response.headers["Location"]).get_data(as_text=True)
        self.assertIn(f"Schedule for {when.replace('T', ' ')} GMT", html)
        self.assertIn(f'name="scheduled_for" value="{when}"', html)
        self.http.post(f"{self.base}/schedule", data={"channel": "sms", "nonce": "s1",
                                                       "scheduled_for": when, "confirmed": "1"})
        self.assertEqual([b.status for b in self.batches()], [BATCH_SCHEDULED])

    def test_the_unreachable_list_pages_without_losing_the_step(self):
        for item in self.items[:2]:
            self.contacts(item)
        with mock.patch("app.distribution.confirm.PROBLEMS_PER_PAGE", 1):
            first = self.http.get(f"{self.base}/confirm?channel=sms&action=send&nonce=p1")
            second = self.http.get(f"{self.base}/confirm?channel=sms&action=send&nonce=p1&page=2")
        first, second = first.get_data(as_text=True), second.get_data(as_text=True)
        self.assertIn(self.items[0].staff_id, first)
        self.assertNotIn(self.items[1].full_name, first)
        self.assertIn(self.items[1].full_name, second)
        next_link = re.search(r'href="([^"]*page=2[^"]*)"', first).group(1)
        for part in ("channel=sms", "action=send", "nonce=p1"):
            self.assertIn(part, next_link)

    def test_nothing_reachable_offers_no_send(self):
        for item in self.items:
            self.contacts(item)
        html = self.http.get(f"{self.base}/confirm?channel=sms&action=send").get_data(as_text=True)
        self.assertNotIn('name="confirmed"', html)
        self.assertIn("No one on this run can be reached this way", html)


class TenantConfirmTests(_PagesCase):
    def setUp(self):
        super().setUp()
        self.base = f"/company/runs/{self.run.id}/distribute"

    def test_a_company_admin_confirms_an_sms_send(self):
        self.login("admin@msc.com")
        response = self.http.post(f"{self.base}/send", data={"channel": "sms", "nonce": "t1"})
        self.assertIn(f"{self.base}/confirm?", response.headers["Location"])
        self.assertEqual(self.batches(), [])
        html = self.http.get(response.headers["Location"]).get_data(as_text=True)
        self.assertEqual(self.figure(html, "To a phone"), len(self.items))
        self.assertEqual(html.count('class="btn primary"'), 1)
        self.http.post(f"{self.base}/send", data={"channel": "sms", "nonce": "t1", "confirmed": "1"})
        self.assertEqual(len(self.batches()), 1)

    def test_only_a_company_admin_reaches_the_step(self):
        preparer = User(name="MSC Preparer", email="preparer@msc.test", role="client_preparer",
                        client_company_id=self.run.client_company_id)
        preparer.set_password("password123")
        db.session.add(preparer)
        db.session.commit()
        self.login("preparer@msc.test")
        response = self.http.get(f"{self.base}/confirm?channel=sms&action=send")
        self.assertNotEqual(response.status_code, 200)


class BulkConfirmTests(_PagesCase):
    """Bulk Distribute on the runs list is an auto send, so it stops at the same step."""

    def setUp(self):
        super().setUp()
        self.login("admin@payrolla.com")
        self.draft = PayrollRun(client_company_id=self.run.client_company_id, month="August",
                                year=2099, status="Draft", total_workers=1, total_net_pay=100)
        db.session.add(self.draft)
        db.session.commit()

    def bulk(self, run_ids, **extra):
        return self.http.post("/payroll/runs/bulk/distribute",
                              data={"run_ids": [str(i) for i in run_ids], **extra})

    def test_bulk_distribute_stops_at_the_confirm_step_and_queues_nothing(self):
        response = self.bulk([self.run.id, self.draft.id], status="Approved")
        self.assertEqual(response.status_code, 302)
        location = response.headers["Location"]
        self.assertIn("/distribution/runs/confirm?", location)
        for part in (f"run_ids={self.run.id}", f"run_ids={self.draft.id}", "status=Approved"):
            self.assertIn(part, location)
        self.assertEqual(DistributionBatch.query.count(), 0)
        runs_list = self.http.get("/payroll/runs").get_data(as_text=True)
        button = re.search(r"<button[^>]*bulk/distribute[^>]*>", runs_list).group(0)
        self.assertNotIn("data-confirm", button)  # the step asks; no yes/no first

    def test_the_step_sums_the_runs_and_names_the_ones_left_out(self):
        self.contacts(self.items[0])
        location = self.bulk([self.run.id, self.draft.id]).headers["Location"]
        html = self.http.get(location).get_data(as_text=True)
        reachable = len(self.items) - 1
        self.assertEqual(self.figure(html, "No contact"), 1)
        self.assertIn("on 2 selected runs", html)
        self.assertIn("Not sent: Not eligible to distribute in its current state (Draft)", html)
        self.assertIn("Can't be reached (1)", html)  # template text, so not escaped
        self.assertIn(f"Send to {reachable} worker{'' if reachable == 1 else 's'} in 1 run", html)
        # Only the run it would queue is posted back.
        self.assertIn(f'name="run_ids" value="{self.run.id}"', html)
        self.assertNotIn(f'name="run_ids" value="{self.draft.id}"', html)
        self.assertIn('action="/payroll/runs/bulk/distribute"', html)
        self.assertEqual(html.count('class="btn primary"'), 1)

    def test_confirming_queues_the_runs_it_showed_once(self):
        for _ in range(2):
            response = self.bulk([self.run.id], confirmed="1")
        self.assertIn("/payroll/runs", response.headers["Location"])
        batches = self.batches()
        self.assertEqual([(b.channel, b.status) for b in batches], [("auto", BATCH_QUEUED)])

    def test_a_run_already_sending_is_left_out(self):
        db.session.add(DistributionBatch(
            payroll_run_id=self.run.id, client_company_id=self.run.client_company_id,
            channel="auto", status=BATCH_QUEUED, total=len(self.items),
        ))
        db.session.commit()
        html = self.http.get(f"/distribution/runs/confirm?run_ids={self.run.id}").get_data(as_text=True)
        self.assertIn("Not sent: A send is already scheduled, queued or running", html)
        self.assertNotIn('name="confirmed"', html)
        self.assertIn("None of the selected runs can be sent now.", html)

    def test_the_unreachable_list_pages_without_losing_the_selection(self):
        for item in self.items[:2]:
            self.contacts(item)
        url = f"/distribution/runs/confirm?run_ids={self.run.id}&run_ids={self.draft.id}"
        with mock.patch("app.distribution.confirm.PROBLEMS_PER_PAGE", 1):
            first = self.http.get(url).get_data(as_text=True)
        next_link = re.search(r'href="([^"]*page=2[^"]*)"', first).group(1)
        for part in (f"run_ids={self.run.id}", f"run_ids={self.draft.id}"):
            self.assertIn(part, next_link)

    def test_no_selection_goes_back_to_the_runs_list(self):
        response = self.http.get("/distribution/runs/confirm", follow_redirects=True)
        self.assertIn("No runs selected for bulk distribute.", response.get_data(as_text=True))

    def test_only_payroll_roles_reach_the_step(self):
        self.http.get("/logout")
        self.login("operations@payrolla.com")
        response = self.http.get(f"/distribution/runs/confirm?run_ids={self.run.id}")
        self.assertNotEqual(response.status_code, 200)


class StatusDisplayTests(_PagesCase):
    def test_unconfirmed_reads_differently_for_the_operator_and_the_company(self):
        self.row(self.items[0], "unknown", recipient="233240000001",
                 error="SasuSync timed out; this message may have been sent")
        self.login("admin@payrolla.com")
        operator = self.http.get(f"/distribution/run/{self.run.id}").get_data(as_text=True)
        self.assertIn(">unconfirmed<", operator)
        self.assertIn("May have been sent. Check the SasuSync portal, then mark it.", operator)
        self.assertIn("Mark sent", operator)
        self.assertIn("Mark not sent", operator)
        self.assertIn("<dt>Unconfirmed</dt><dd>1</dd>", operator)
        self.http.get("/logout")
        self.login("admin@msc.com")
        tenant = self.http.get(f"/company/runs/{self.run.id}/distribute").get_data(as_text=True)
        self.assertIn("Unconfirmed. Payrolla is checking.", tenant)
        self.assertNotIn("Mark sent", tenant)
        self.assertNotIn("SasuSync", tenant)

    def test_no_settle_buttons_while_the_page_is_polling(self):
        self.row(self.items[0], "unknown")
        db.session.add(DistributionBatch(
            payroll_run_id=self.run.id, client_company_id=self.run.client_company_id,
            channel="sms", status=BATCH_QUEUED, total=len(self.items),
        ))
        db.session.commit()
        self.login("admin@payrolla.com")
        fragment = self.http.get(
            f"/distribution/run/{self.run.id}/status-fragment"
        ).get_data(as_text=True)
        self.assertIn('hx-trigger="every 3s', fragment)
        self.assertIn("May have been sent", fragment)
        self.assertNotIn("Mark sent", fragment)
        self.assertNotIn("<button", fragment)

    def test_a_sending_row_has_its_own_badge(self):
        self.row(self.items[0], "sending")
        self.login("admin@payrolla.com")
        html = self.http.get(f"/distribution/run/{self.run.id}").get_data(as_text=True)
        self.assertIn(">sending…<", html)

    def test_phone_numbers_are_masked_on_every_operator_screen(self):
        batch = DistributionBatch(payroll_run_id=self.run.id,
                                  client_company_id=self.run.client_company_id,
                                  channel="sms", status=BATCH_COMPLETED, total=2,
                                  sent_count=1, failed_count=0, unknown_count=1)
        db.session.add(batch)
        db.session.commit()
        self.row(self.items[0], "sent", recipient="233240000001", distribution_batch_id=batch.id)
        self.row(self.items[1], "sent", channel="email", recipient="worker@example.test",
                 distribution_batch_id=batch.id)
        self.login("admin@payrolla.com")
        for path in (f"/distribution/run/{self.run.id}", "/distribution/history",
                     f"/distribution/batch/{batch.id}"):
            with self.subTest(path=path):
                html = self.http.get(path).get_data(as_text=True)
                self.assertNotIn("233240000001", html)
                self.assertIn("23324***001", html)
                self.assertIn("worker@example.test", html)  # only phone numbers are masked

    def test_the_queue_panel_counts_unconfirmed(self):
        db.session.add(DistributionBatch(
            payroll_run_id=self.run.id, client_company_id=self.run.client_company_id,
            channel="sms", status=BATCH_COMPLETED, total=3, sent_count=1, failed_count=0,
            unknown_count=2, finished_at=datetime.now(timezone.utc),
        ))
        db.session.commit()
        self.login("admin@payrolla.com")
        html = self.http.get(f"/distribution/run/{self.run.id}").get_data(as_text=True)
        self.assertIn("<dt>Unconfirmed</dt><dd>2</dd>", html)


if __name__ == "__main__":
    unittest.main()
