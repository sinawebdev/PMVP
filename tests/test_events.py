"""Phase 6 — domain events + in-app notifications.

The append-only DomainEvent log records business events and fans them out to the
right users: a platform hold/release notifies the client's users (platform ->
tenant); a client's payslip distribution notifies platform oversight (tenant ->
platform). The Notification inbox is strictly per-user.
"""

import os
import unittest

os.environ["SKIP_DOTENV"] = "true"
os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["SEED_DEMO_DATA"] = "true"
os.environ["PERSISTENCE_REQUIRED"] = "false"

from app import create_app, db  # noqa: E402
from app.distribution.queue import process_all_queued  # noqa: E402
from app.events import record_event, tenant_activity  # noqa: E402
from app.models import (  # noqa: E402
    AuditTrail,
    ClientCompany,
    DomainEvent,
    Notification,
    PayrollRun,
    User,
)
from app.payroll_status import DRAFT  # noqa: E402


class EventFanoutTestCase(unittest.TestCase):
    def setUp(self):
        self.app = create_app()
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()
        self.ctx = self.app.app_context()
        self.ctx.push()
        # A fresh tenant with one user and a first (holdable) run.
        self.co = ClientCompany(name="RiskCo Ltd", status="Active")
        db.session.add(self.co)
        db.session.commit()
        self.co_admin = User(
            name="RiskCo Admin", email="admin@riskco.demo",
            role="client_admin", client_company_id=self.co.id,
        )
        self.co_admin.set_password("password123")
        db.session.add(self.co_admin)
        self.run = PayrollRun(
            month="January", year=2026, status=DRAFT,
            client_company_id=self.co.id, total_net_pay=5000, total_workers=8,
        )
        db.session.add(self.run)
        db.session.commit()
        self.run_id = self.run.id

    def tearDown(self):
        self.ctx.pop()

    def _login(self, email):
        self.assertEqual(
            self.client.post("/login", data={"email": email, "password": "password123"}).status_code,
            302,
        )

    def _notes_for(self, user):
        return Notification.query.filter_by(user_id=user.id).all()

    def test_risk_hold_emits_event_and_notifies_tenant(self):
        self._login("operator@payrolla.com")
        self.client.post(f"/oversight/runs/{self.run_id}/risk-check")
        event = DomainEvent.query.filter_by(event_type="run.risk_held").first()
        self.assertIsNotNone(event)
        self.assertEqual(event.client_company_id, self.co.id)
        self.assertEqual(event.subject_type, "PayrollRun")
        self.assertEqual(event.subject_id, self.run_id)
        # The tenant's admin is notified.
        notes = self._notes_for(self.co_admin)
        self.assertEqual(len(notes), 1)
        self.assertEqual(notes[0].level, "warning")
        self.assertIsNone(notes[0].read_at)

    def test_release_emits_event_and_notifies_tenant(self):
        self._login("operator@payrolla.com")
        self.client.post(f"/oversight/runs/{self.run_id}/risk-check")  # -> Held + 1 note
        self.client.post(f"/oversight/runs/{self.run_id}/release")
        self.assertEqual(DomainEvent.query.filter_by(event_type="run.hold_released").count(), 1)
        self.assertEqual(len(self._notes_for(self.co_admin)), 2)  # held + released

    def test_client_distribution_notifies_platform_admins(self):
        # Seeded MSC has a client_admin (admin@msc.com) and an Approved run.
        msc = User.query.filter_by(email="admin@msc.com").first()
        msc_run = PayrollRun.query.filter_by(client_company_id=msc.client_company_id).first()
        self._login("admin@msc.com")
        self.client.post(
            f"/company/runs/{msc_run.id}/distribute/send",
            data={"channel": "auto", "nonce": "n1"},
        )
        # Sending only queues the batch now; a worker runs it and fires the event.
        process_all_queued()
        event = DomainEvent.query.filter_by(event_type="payslips.distributed").first()
        self.assertIsNotNone(event)
        self.assertEqual(event.client_company_id, msc.client_company_id)
        # The payslips.distributed (tenant -> platform) event notifies platform
        # admins, NOT the client_admin.
        chrisnat = User.query.filter_by(email="operator@payrolla.com").first()
        self.assertGreaterEqual(len(self._notes_for(chrisnat)), 1)
        msc_notes_for_event = [
            n for n in self._notes_for(msc) if n.event_id == event.id
        ]
        self.assertEqual(len(msc_notes_for_event), 0)
        # The client_admin who initiated it IS notified of completion, though
        # (Phase 3, Slice 8) — a separate distribution.completed event.
        self.assertIsNotNone(
            DomainEvent.query.filter_by(event_type="distribution.completed").first()
        )


class NotificationInboxTestCase(unittest.TestCase):
    def setUp(self):
        self.app = create_app()
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()
        self.ctx = self.app.app_context()
        self.ctx.push()
        self.msc = User.query.filter_by(email="admin@msc.com").first()
        self.stellar = User.query.filter_by(email="admin@acme.com").first()
        self.mine = Notification(
            user_id=self.msc.id, title="Yours", body="A note for MSC", level="info"
        )
        self.theirs = Notification(
            user_id=self.stellar.id, title="Theirs", body="A note for Stellar", level="info"
        )
        db.session.add_all([self.mine, self.theirs])
        db.session.commit()
        self.mine_id, self.theirs_id = self.mine.id, self.theirs.id

    def tearDown(self):
        self.ctx.pop()

    def _login(self, email):
        self.assertEqual(
            self.client.post("/login", data={"email": email, "password": "password123"}).status_code,
            302,
        )

    def test_inbox_shows_only_my_notifications(self):
        self._login("admin@msc.com")
        html = self.client.get("/notifications").get_data(as_text=True)
        self.assertEqual(self.client.get("/notifications").status_code, 200)
        self.assertIn("A note for MSC", html)
        self.assertNotIn("A note for Stellar", html)

    def test_mark_read_only_my_own(self):
        self._login("admin@msc.com")
        self.assertEqual(
            self.client.post(f"/notifications/{self.mine_id}/read").status_code, 302
        )
        self.assertIsNotNone(db.session.get(Notification, self.mine_id).read_at)
        # Another user's notification is 404, never mutated.
        self.assertEqual(
            self.client.post(f"/notifications/{self.theirs_id}/read").status_code, 404
        )
        self.assertIsNone(db.session.get(Notification, self.theirs_id).read_at)

    def test_record_event_dedups_recipients(self):
        # Same user passed twice yields one notification.
        record_event("test.event", summary="hi", recipients=[self.msc, self.msc])
        db.session.commit()
        made = Notification.query.filter_by(user_id=self.msc.id, title="Test Event").all()
        self.assertEqual(len(made), 1)


class TenantActivityScopeTestCase(unittest.TestCase):
    """The tenant's activity feed must include what PAYROLLA did to this
    tenant's payroll, not only what the tenant's own users did.

    AuditTrail carries no tenant column, so the feed used to scope purely on the
    acting user — which meant a company whose runs had been approved six times
    by an operator saw "No activity yet" under a panel captioned "Every payroll
    action, on the record", while the operator's dashboard listed all six. A
    dashboard denying an event another dashboard is showing is the exact class
    of contradiction the panel exists to prevent.
    """

    def setUp(self):
        self.app = create_app()
        self.app.config["TESTING"] = True
        self.ctx = self.app.app_context()
        self.ctx.push()

        self.co = ClientCompany(name="FeedCo Ltd", status="Active")
        self.other = ClientCompany(name="OtherCo Ltd", status="Active")
        db.session.add_all([self.co, self.other])
        db.session.commit()

        self.operator = User(
            name="Ama Operator", email="ops@feedtest.demo", role="payrolla_admin",
        )
        self.operator.set_password("password123")
        db.session.add(self.operator)

        self.run = PayrollRun(
            month="March", year=2026, status=DRAFT,
            client_company_id=self.co.id, total_net_pay=1000, total_workers=3,
        )
        self.other_run = PayrollRun(
            month="March", year=2026, status=DRAFT,
            client_company_id=self.other.id, total_net_pay=1000, total_workers=3,
        )
        db.session.add_all([self.run, self.other_run])
        db.session.commit()

    def tearDown(self):
        self.ctx.pop()

    def _approval(self, run):
        db.session.add(
            AuditTrail(
                user_id=self.operator.id,
                user_role="payrolla_admin",
                action="Payroll approval",
                related_record_type="PayrollRun",
                related_record_id=run.id,
                notes=f"{run.month} {run.year} approved.",
            )
        )
        db.session.commit()

    def test_platform_action_on_this_tenants_run_reaches_the_tenant_feed(self):
        self._approval(self.run)
        feed = tenant_activity(self.co.id)
        self.assertEqual([item["title"] for item in feed], ["Payroll approval"])
        # Named as the provider, not as the individual operator: to the tenant
        # the actor is Payrolla, and an operator's name is staffing detail they
        # never asked for.
        self.assertEqual(feed[0]["actor"], "Payrolla")

    def test_another_tenants_run_never_reaches_this_feed(self):
        self._approval(self.other_run)
        self.assertEqual(tenant_activity(self.co.id), [])


if __name__ == "__main__":
    unittest.main()
