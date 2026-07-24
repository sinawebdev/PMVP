"""Phase 5 — run risk gate.

Two layers: the pure engine (app/risk.py) against each of the three settled
rules, and the platform oversight routes that persist the verdict and drive the
Held / Auto-Accepted / released lifecycle.
"""

import os
import unittest
from datetime import datetime, timedelta

os.environ["SKIP_DOTENV"] = "true"
os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["SEED_DEMO_DATA"] = "true"
os.environ["PERSISTENCE_REQUIRED"] = "false"

from app import create_app, db  # noqa: E402
from app.models import ClientCompany, PayrollRun, User  # noqa: E402
from app.payroll_status import APPROVED, AUTO_ACCEPTED, DRAFT, HELD, PENDING_APPROVAL  # noqa: E402
from app.risk import (  # noqa: E402
    HEADCOUNT_SWING_PCT,
    NET_PAY_VARIANCE_PCT,
    evaluate_run,
)

_BASE = datetime(2026, 1, 1, 12, 0, 0)


class RiskEngineTestCase(unittest.TestCase):
    def setUp(self):
        self.app = create_app()
        self.app.config["TESTING"] = True
        self.ctx = self.app.app_context()
        self.ctx.push()
        self.co = ClientCompany(name="RiskCo Ltd", status="Active")
        db.session.add(self.co)
        db.session.commit()
        self._seq = 0

    def tearDown(self):
        self.ctx.pop()

    def _run(self, status=DRAFT, net=0, workers=0):
        """Add a run for RiskCo with a strictly increasing created_at."""
        self._seq += 1
        run = PayrollRun(
            month="January",
            year=2026,
            status=status,
            client_company_id=self.co.id,
            total_net_pay=net,
            total_workers=workers,
            created_at=_BASE + timedelta(hours=self._seq),
        )
        db.session.add(run)
        db.session.commit()
        return run

    def _codes(self, verdict):
        return {c.code: c.tripped for c in verdict.checks}

    # --- Rule 1: new-client hold (first 2 runs) ----------------------------
    def test_rule1_first_two_runs_held_third_not(self):
        first = self._run()  # 0 priors -> run #1
        self.assertTrue(evaluate_run(first).held)
        self.assertTrue(self._codes(evaluate_run(first))["new_client"])

        second = self._run()  # 1 prior -> run #2
        self.assertTrue(self._codes(evaluate_run(second))["new_client"])

        third = self._run()  # 2 priors -> run #3, past the window
        self.assertFalse(self._codes(evaluate_run(third))["new_client"])

    # --- Rule 2: net-pay variance vs previous closed run -------------------
    def test_rule2_net_pay_variance(self):
        # Two closed history runs so Rule 1 is satisfied; the later one (net=1000)
        # is the baseline.
        self._run(status=APPROVED, net=1000, workers=10)
        self._run(status=APPROVED, net=1000, workers=10)

        over = self._run(net=1200, workers=10)  # +20% > 15% threshold
        codes = self._codes(evaluate_run(over))
        self.assertFalse(codes["new_client"])
        self.assertTrue(codes["net_pay_variance"])
        self.assertFalse(codes["headcount_swing"])
        self.assertTrue(evaluate_run(over).held)

        under = self._run(net=1100, workers=10)  # +10% < 15% threshold
        self.assertFalse(self._codes(evaluate_run(under))["net_pay_variance"])

    # --- Rule 3: headcount swing vs previous closed run --------------------
    def test_rule3_headcount_swing(self):
        self._run(status=APPROVED, net=1000, workers=10)
        self._run(status=APPROVED, net=1000, workers=10)  # baseline: 10 workers

        over = self._run(net=1000, workers=13)  # +30% > 20% threshold
        codes = self._codes(evaluate_run(over))
        self.assertFalse(codes["net_pay_variance"])
        self.assertTrue(codes["headcount_swing"])
        self.assertTrue(evaluate_run(over).held)

        under = self._run(net=1000, workers=11)  # +10% < 20% threshold
        self.assertFalse(self._codes(evaluate_run(under))["headcount_swing"])

    def test_no_previous_closed_run_only_rule1_applies(self):
        # Give the client 2 pending (non-closed) priors so Rule 1 passes but there
        # is still no CLOSED baseline for Rules 2 and 3.
        self._run(status=DRAFT, net=1000, workers=10)
        self._run(status=DRAFT, net=1000, workers=10)
        run = self._run(net=999999, workers=999)
        codes = self._codes(evaluate_run(run))
        self.assertFalse(codes["new_client"])
        self.assertFalse(codes["net_pay_variance"])  # no baseline -> not tripped
        self.assertFalse(codes["headcount_swing"])
        self.assertFalse(evaluate_run(run).held)

    def test_thresholds_are_the_settled_values(self):
        self.assertEqual(NET_PAY_VARIANCE_PCT, 0.15)
        self.assertEqual(HEADCOUNT_SWING_PCT, 0.20)


class RiskOversightRoutesTestCase(unittest.TestCase):
    def setUp(self):
        self.app = create_app()
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()
        self.ctx = self.app.app_context()
        self.ctx.push()
        self.co = ClientCompany(name="RiskCo Ltd", status="Active")
        db.session.add(self.co)
        db.session.commit()
        # A first run for RiskCo -> Rule 1 will hold it.
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

    def test_risk_check_holds_first_run_then_release(self):
        self._login("chrisnat.admin@chrisnat.local")
        resp = self.client.post(f"/oversight/runs/{self.run_id}/risk-check")
        self.assertEqual(resp.status_code, 302)
        run = db.session.get(PayrollRun, self.run_id)
        self.assertEqual(run.status, HELD)
        self.assertEqual(run.risk_status, "held")
        self.assertIsNotNone(run.risk_reasons)
        self.assertIsNotNone(run.risk_checked_at)

        # It shows in the oversight queue.
        html = self.client.get("/oversight/risk").get_data(as_text=True)
        self.assertIn("RiskCo Ltd", html)

        # Releasing moves it to Pending Approval.
        self.assertEqual(
            self.client.post(f"/oversight/runs/{self.run_id}/release").status_code, 302
        )
        self.assertEqual(db.session.get(PayrollRun, self.run_id).status, PENDING_APPROVAL)

    def test_risk_check_rejects_closed_run(self):
        self._login("chrisnat.admin@chrisnat.local")
        self.run.status = APPROVED
        db.session.commit()
        resp = self.client.post(f"/oversight/runs/{self.run_id}/risk-check")
        self.assertEqual(resp.status_code, 302)  # bounced to detail, no change
        self.assertEqual(db.session.get(PayrollRun, self.run_id).status, APPROVED)

    def test_tenant_user_cannot_reach_oversight(self):
        self._login("admin@msc.demo")  # a tenant user
        resp = self.client.get("/oversight/risk")
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(resp.headers["Location"].endswith("/company"))


if __name__ == "__main__":
    unittest.main()
