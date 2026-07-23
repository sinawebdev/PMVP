"""Phase 4, Slice 6 — SLA monitoring + alert thresholds.

evaluate_sla detects overdue batches, a high recent failure rate, and (opt-in)
sent-but-unconfirmed messages. maybe_check_sla alerts platform admins on new
breaches, throttled by a cooldown. The dashboard surfaces SLA status.
"""

import os
import unittest
from datetime import datetime, timedelta, timezone

os.environ["SKIP_DOTENV"] = "true"
os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["SEED_DEMO_DATA"] = "true"
os.environ["PERSISTENCE_REQUIRED"] = "false"

from app import create_app, db  # noqa: E402
from app.distribution.dashboard import collect_dashboard_stats  # noqa: E402
from app.distribution.sla import evaluate_sla, maybe_check_sla, reset  # noqa: E402
from app.models import (  # noqa: E402
    BATCH_QUEUED,
    DELIVERY_FAILED,
    DELIVERY_SENT,
    DistributionBatch,
    DomainEvent,
    PayrollRun,
    PayslipDelivery,
    User,
)


def _ago(**kw):
    return datetime.now(timezone.utc) - timedelta(**kw)


class SlaEvaluateTestCase(unittest.TestCase):
    def setUp(self):
        self.app = create_app()
        self.app.config["TESTING"] = True
        self.app.config.update(
            SLA_BATCH_MINUTES=30, SLA_FAILURE_RATE=0.5, SLA_MIN_VOLUME=4,
            SLA_WINDOW_HOURS=24, SLA_DELIVERY_CONFIRM_HOURS=0,
        )
        self.ctx = self.app.app_context()
        self.ctx.push()
        reset()
        self.run = PayrollRun.query.filter_by(status="Approved").first()
        self.operator = User.query.filter_by(email="admin@chrisnat.local").first()

    def tearDown(self):
        reset()
        db.session.remove()
        self.ctx.pop()

    def _delivery(self, status, **kw):
        d = PayslipDelivery(
            payroll_item_id=self.run.items[0].id, payroll_run_id=self.run.id,
            channel="sms", status=status, **kw,
        )
        db.session.add(d)
        return d

    def test_clean_state_is_within_sla(self):
        self.assertTrue(evaluate_sla()["ok"])

    def test_overdue_batch_is_a_breach(self):
        db.session.add(DistributionBatch(
            payroll_run_id=self.run.id, client_company_id=self.run.client_company_id,
            channel="auto", status=BATCH_QUEUED, total=5, created_at=_ago(minutes=60),
        ))
        db.session.commit()
        result = evaluate_sla()
        self.assertFalse(result["ok"])
        self.assertTrue(any(b["type"] == "batch_overdue" for b in result["breaches"]))

    def test_recent_high_failure_rate_is_a_breach(self):
        for _ in range(3):
            self._delivery(DELIVERY_FAILED)
        self._delivery(DELIVERY_SENT)
        db.session.commit()
        result = evaluate_sla()  # 3/4 = 75% >= 50%, volume 4 >= min 4
        self.assertTrue(any(b["type"] == "failure_rate" for b in result["breaches"]))

    def test_failure_rate_ignored_below_min_volume(self):
        self._delivery(DELIVERY_FAILED)  # only 1 delivery, below min_volume 4
        db.session.commit()
        self.assertFalse(any(b["type"] == "failure_rate" for b in evaluate_sla()["breaches"]))

    def test_unconfirmed_breach_is_opt_in(self):
        self._delivery(DELIVERY_SENT, provider_message_id="wamid.1",
                       provider_status=None, sent_at=_ago(hours=5))
        db.session.commit()
        # Off by default (confirm_hours 0).
        self.assertFalse(any(b["type"] == "unconfirmed" for b in evaluate_sla()["breaches"]))
        # Enabled -> breach.
        self.app.config["SLA_DELIVERY_CONFIRM_HOURS"] = 1
        self.assertTrue(any(b["type"] == "unconfirmed" for b in evaluate_sla()["breaches"]))


class SlaAlertTestCase(unittest.TestCase):
    def setUp(self):
        self.app = create_app()
        self.app.config["TESTING"] = True
        self.app.config.update(
            SLA_BATCH_MINUTES=30, SLA_CHECK_INTERVAL_SECONDS=0,
            SLA_ALERT_COOLDOWN_SECONDS=3600,
        )
        self.ctx = self.app.app_context()
        self.ctx.push()
        reset()
        self.run = PayrollRun.query.filter_by(status="Approved").first()
        # An overdue batch to trip the SLA.
        db.session.add(DistributionBatch(
            payroll_run_id=self.run.id, client_company_id=self.run.client_company_id,
            channel="auto", status=BATCH_QUEUED, total=5, created_at=_ago(minutes=90),
        ))
        db.session.commit()

    def tearDown(self):
        reset()
        db.session.remove()
        self.ctx.pop()

    def test_alert_fires_once_then_respects_cooldown(self):
        maybe_check_sla()
        maybe_check_sla()  # within cooldown -> no second alert
        events = DomainEvent.query.filter_by(event_type="distribution.sla_breach").all()
        self.assertEqual(len(events), 1)

    def test_dashboard_shows_sla_breach(self):
        stats = collect_dashboard_stats()
        self.assertFalse(stats["sla"]["ok"])
        self.assertTrue(stats["sla"]["breaches"])


class SlaRouteTestCase(unittest.TestCase):
    def setUp(self):
        self.app = create_app()
        self.app.config["TESTING"] = True
        self.http = self.app.test_client()
        self.ctx = self.app.app_context()
        self.ctx.push()
        reset()

    def tearDown(self):
        reset()
        db.session.remove()
        self.ctx.pop()

    def test_dashboard_renders_sla_panel(self):
        self.http.post("/login", data={"email": "admin@chrisnat.local", "password": "password123"})
        resp = self.http.get("/distribution/dashboard")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("SLA", resp.get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()
