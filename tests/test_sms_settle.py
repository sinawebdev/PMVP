"""SMS Phase 3 (Q2): an operator settles a send that may have gone out.

`unknown` is never resent automatically, so a person closes it after checking
the SasuSync portal: `sent` (the operator's word, not a delivery report) or
`not_sent` (a failure that "Resend failed" picks up, with a new link). Only a
row that is `unknown` right now can be settled, only through its own run, only
by an operator, and every settle is audited.
"""
import os
import unittest
from datetime import datetime, timedelta, timezone

os.environ["SKIP_DOTENV"] = "true"
os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["SEED_DEMO_DATA"] = "true"
os.environ["PERSISTENCE_REQUIRED"] = "false"

from app import create_app, db  # noqa: E402
from app.distribution import sla  # noqa: E402
from app.distribution.links import mint_payslip_link, resolve_payslip_link  # noqa: E402
from app.distribution.service import distribute_run  # noqa: E402
from app.distribution.settle import NOT_SENT_ERROR  # noqa: E402
from app.models import AuditTrail, PayrollRun, PayslipDelivery, User  # noqa: E402

CLAIMED = datetime(2026, 10, 1, 9, 30)


class SettleTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app()
        self.app.config.update(TESTING=True, WTF_CSRF_ENABLED=False,
                               PUBLIC_BASE_URL="https://pmvp-v1.onrender.com")
        self.http = self.app.test_client()
        self.ctx = self.app.app_context()
        self.ctx.push()
        sla.reset()
        self.operator = User.query.filter_by(email="admin@payrolla.com").first()
        tenant = User.query.filter_by(email="admin@msc.com").first()
        self.run = PayrollRun.query.filter_by(
            client_company_id=tenant.client_company_id, status="Approved"
        ).first()
        self.items = list(self.run.items)
        self.delivery = self.unknown(self.items[0])

    def tearDown(self):
        sla.reset()
        db.session.remove()
        self.ctx.pop()

    def unknown(self, item, claimed_at=CLAIMED):
        delivery = PayslipDelivery(
            payroll_item_id=item.id, payroll_run_id=self.run.id, channel="sms",
            status="unknown", attempts=1, recipient="233240000001", provider="sasusync",
            error="SasuSync timed out; this message may have been sent", claimed_at=claimed_at,
        )
        db.session.add(delivery)
        db.session.commit()
        return delivery

    def login(self, email="admin@payrolla.com"):
        self.http.post("/login", data={"email": email, "password": "password123"})

    def settle(self, outcome, delivery=None, run_id=None):
        delivery = delivery or self.delivery
        return self.http.post(
            f"/distribution/run/{run_id or self.run.id}/delivery/{delivery.id}/settle",
            data={"outcome": outcome},
        )

    def audits(self):
        return AuditTrail.query.filter_by(action="Unconfirmed delivery settled").all()

    def test_sent_settles_it_as_sent_and_is_audited(self):
        self.login()
        response = self.settle("sent")
        self.assertEqual(response.status_code, 302)
        db.session.refresh(self.delivery)
        self.assertEqual(self.delivery.status, "sent")
        self.assertIsNone(self.delivery.provider_status)  # the operator's word, not a report
        self.assertIsNone(self.delivery.error)
        self.assertEqual(self.delivery.sent_at, CLAIMED)
        (audit,) = self.audits()
        self.assertEqual(audit.user_id, self.operator.id)
        self.assertIn(f"delivery={self.delivery.id}", audit.notes)
        self.assertIn("outcome=sent", audit.notes)

    def test_not_sent_makes_it_a_failure_and_is_audited(self):
        self.login()
        self.settle("not_sent")
        db.session.refresh(self.delivery)
        self.assertEqual(self.delivery.status, "failed")
        self.assertEqual(self.delivery.error, NOT_SENT_ERROR)
        self.assertIsNone(self.delivery.next_retry_at)  # never retried on its own
        (audit,) = self.audits()
        self.assertIn("outcome=not_sent", audit.notes)

    def test_resend_failed_picks_up_a_not_sent_row_with_a_new_link(self):
        old_code = mint_payslip_link(self.items[0], self.delivery)
        db.session.commit()
        self.login()
        self.settle("not_sent")
        summary = distribute_run(self.run, channel="sms", only_failed=True)
        self.assertEqual(summary["sent"], 1)
        db.session.refresh(self.delivery)
        self.assertEqual((self.delivery.status, self.delivery.attempts), ("sent", 2))
        self.assertIsNone(resolve_payslip_link(old_code))  # the old link never arrived

    def test_a_row_that_is_not_unknown_is_a_conflict(self):
        self.login()
        for status in ("sent", "failed", "sending"):
            with self.subTest(status=status):
                self.delivery.status = status
                db.session.commit()
                response = self.settle("sent")
                self.assertEqual(response.status_code, 409)
                db.session.refresh(self.delivery)
                self.assertEqual(self.delivery.status, status)
        self.assertEqual(self.audits(), [])

    def test_settling_twice_is_a_conflict_the_second_time(self):
        self.login()
        self.assertEqual(self.settle("not_sent").status_code, 302)
        self.assertEqual(self.settle("sent").status_code, 409)
        db.session.refresh(self.delivery)
        self.assertEqual(self.delivery.status, "failed")

    def test_a_delivery_from_another_run_is_404(self):
        other = PayrollRun(client_company_id=self.run.client_company_id,
                           month="November", year=2026, status="Approved")
        db.session.add(other)
        db.session.commit()
        self.login()
        self.assertEqual(self.settle("sent", run_id=other.id).status_code, 404)
        db.session.refresh(self.delivery)
        self.assertEqual(self.delivery.status, "unknown")

    def test_an_outcome_it_does_not_know_is_400(self):
        self.login()
        self.assertEqual(self.settle("delivered").status_code, 400)

    def test_a_company_admin_cannot_reach_it(self):
        self.login("admin@msc.com")
        response = self.settle("sent")
        self.assertEqual(response.status_code, 302)
        self.assertNotIn(f"/distribution/run/{self.run.id}", response.headers["Location"])
        db.session.refresh(self.delivery)
        self.assertEqual(self.delivery.status, "unknown")
        self.assertEqual(self.audits(), [])

    def test_settling_the_last_unknown_row_clears_the_sla_breach(self):
        long_ago = datetime.now(timezone.utc) - timedelta(minutes=45)
        self.delivery.claimed_at = long_ago
        second = self.unknown(self.items[1], claimed_at=long_ago)
        db.session.commit()

        def breach():
            found = [b for b in sla.evaluate_sla()["breaches"] if b["type"] == "unknown"]
            return found[0]["count"] if found else 0

        self.assertEqual(breach(), 2)
        self.login()
        self.settle("sent")
        self.assertEqual(breach(), 1)
        self.settle("not_sent", delivery=second)
        self.assertEqual(breach(), 0)


if __name__ == "__main__":
    unittest.main()
