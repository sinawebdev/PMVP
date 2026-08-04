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
from app.payroll_status import (  # noqa: E402
    APPROVED,
    AUTO_ACCEPTED,
    DRAFT,
    HELD,
    PENDING_APPROVAL,
    RISK_RELEASED,
    run_progress,
)
from app.risk import (  # noqa: E402
    HEADCOUNT_SWING_PCT,
    NET_PAY_VARIANCE_PCT,
    evaluate_run,
    held_run_count,
    held_runs_query,
    queue_rows,
    queue_summary,
    reason_items,
    risk_badge,
    risk_summary,
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


class RiskQueuePresentationTestCase(unittest.TestCase):
    """What the review queue derives on top of the verdict, so a reviewer can
    rank holds without opening them (DDEP Phase 1/2 — the queue used to state
    neither the stake nor the movement that caused each hold)."""

    def setUp(self):
        self.app = create_app()
        self.app.config["TESTING"] = True
        self.ctx = self.app.app_context()
        self.ctx.push()
        self.co = ClientCompany(name="QueueCo Ltd", status="Active")
        db.session.add(self.co)
        db.session.commit()
        self._seq = 0

    def tearDown(self):
        self.ctx.pop()

    def _run(self, status=DRAFT, net=0, workers=0, reasons=None, checked=None):
        self._seq += 1
        run = PayrollRun(
            month="January",
            year=2026,
            status=status,
            client_company_id=self.co.id,
            total_net_pay=net,
            total_workers=workers,
            risk_reasons=reasons,
            risk_checked_at=checked,
            created_at=_BASE + timedelta(hours=self._seq),
        )
        db.session.add(run)
        db.session.commit()
        return run

    def test_reasons_split_into_one_item_per_rule(self):
        """A run tripping two rules read as one sentence with a pipe in it."""
        run = self._run(
            status=HELD,
            reasons="Net pay 1,200.00 vs previous 1,000.00 (20.0% change; threshold 15%)."
            " | 13 workers vs previous 10 (30.0% change; threshold 20%).",
        )
        items = reason_items(run)
        self.assertEqual([i["code"] for i in items], ["net_pay", "headcount"])
        self.assertEqual([i["label"] for i in items], ["Net pay", "Headcount"])
        # The recorded sentence is never rewritten — only split.
        self.assertIn("20.0% change", items[0]["detail"])

    def test_unclassifiable_reason_keeps_its_own_words(self):
        """A reason written by an older gate must still reach the operator."""
        run = self._run(status=HELD, reasons="Something a future rule wrote.")
        items = reason_items(run)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["code"], "other")
        self.assertEqual(items[0]["detail"], "Something a future rule wrote.")

    def test_summary_totals_the_whole_queue_not_the_page(self):
        self._run(status=HELD, net=1000, workers=10, checked=_BASE)
        self._run(status=HELD, net=2500, workers=15, checked=_BASE + timedelta(days=1))
        self._run(status=APPROVED, net=9999, workers=99)  # not held: excluded
        summary = queue_summary()
        self.assertEqual(summary["count"], 2)
        self.assertEqual(summary["net_pay"], 3500)
        self.assertEqual(summary["workers"], 25)
        self.assertEqual(summary["oldest_checked_at"], _BASE)
        self.assertIsNotNone(summary["oldest_age"])

    def test_rows_carry_the_movement_against_the_gate_s_own_baseline(self):
        self._run(status=APPROVED, net=1000, workers=10)
        held = self._run(status=HELD, net=1200, workers=12, checked=_BASE)
        row = queue_rows([held])[0]
        self.assertIsNotNone(row["previous"])
        self.assertAlmostEqual(row["net_change"], 0.2)
        self.assertAlmostEqual(row["worker_change"], 0.2)
        self.assertEqual(row["previous_workers"], 10)

    def test_a_moved_baseline_is_flagged_rather_than_silently_contradicting(self):
        """The verdict is a photograph. If a newer run closes after scoring, the
        recorded reason describes a comparison the row can no longer reproduce —
        which is exactly what putting the movement on the row makes visible."""
        self._run(status=APPROVED, net=1000, workers=10)
        held = self._run(
            status=HELD,
            net=1100,  # only +10% against the CURRENT baseline
            workers=10,
            reasons="Net pay 1,100.00 vs previous 800.00 (37.5% change; threshold 15%).",
            checked=_BASE,
        )
        self.assertTrue(queue_rows([held])[0]["stale"])

        # A hold whose recorded reason still matches today's comparison is not.
        agreeing = self._run(
            status=HELD,
            net=1400,  # +40% against the same baseline
            workers=10,
            reasons="Net pay 1,400.00 vs previous 1,000.00 (40.0% change; threshold 15%).",
            checked=_BASE,
        )
        self.assertFalse(queue_rows([agreeing])[0]["stale"])

    def test_queue_orders_oldest_first_by_default(self):
        """A work queue serves the client who has waited longest. The dashboard's
        Held panel keeps newest-first — it is a feed, not a queue — so the two
        orders must stay distinguishable."""
        newest = self._run(status=HELD, checked=_BASE + timedelta(days=5))
        oldest = self._run(status=HELD, checked=_BASE)
        self.assertEqual(held_runs_query(order="oldest").first().id, oldest.id)
        self.assertEqual(held_runs_query(order="newest").first().id, newest.id)
        self.assertEqual(held_runs_query().first().id, newest.id)  # unchanged default


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
        self._login("operator@payrolla.com")
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

    def test_release_clears_every_stale_risk_indicator(self):
        """Releasing a hold must stop the run reading as held EVERYWHERE.

        Regression: release moved PayrollRun.status off Held but left
        risk_status at 'held', so the dashboard counter/panel, the run's risk
        badge, and the tenant's own run page all kept reporting a hold that had
        already been released."""
        self._login("operator@payrolla.com")
        self.client.post(f"/oversight/runs/{self.run_id}/risk-check")

        # Held: counted on the dashboard and listed in the queue. The dashboard
        # says so in its Risk & action panel — the standalone "Held for Risk
        # Review" list was removed in the operator dashboard redesign, so the
        # signal's own wording is what has to clear.
        self.assertEqual(held_run_count(), 1)
        dashboard = self.client.get("/dashboard").get_data(as_text=True)
        self.assertIn("payroll held for risk review", dashboard)
        self.assertIn("Risk gate: Held", self.client.get(
            f"/payroll/runs/{self.run_id}").get_data(as_text=True))

        self.client.post(f"/oversight/runs/{self.run_id}/release")
        run = db.session.get(PayrollRun, self.run_id)

        # Verdict moved off 'held' — reasons kept for the audit record.
        self.assertEqual(run.status, PENDING_APPROVAL)
        self.assertEqual(run.risk_status, RISK_RELEASED)
        self.assertIsNotNone(run.risk_reasons)

        # Every derived indicator has cleared.
        self.assertEqual(held_run_count(), 0)
        self.assertEqual(risk_summary()["held_runs"], [])
        self.assertEqual(risk_badge(run)["label"], "Released")
        self.assertIn(
            "No runs are currently held",
            self.client.get("/oversight/risk").get_data(as_text=True),
        )
        dashboard = self.client.get("/dashboard").get_data(as_text=True)
        self.assertNotIn("payroll held for risk review", dashboard)
        detail = self.client.get(f"/payroll/runs/{self.run_id}").get_data(as_text=True)
        self.assertIn("Risk gate: Released", detail)
        self.assertNotIn("Risk gate: Held", detail)

        # The lifecycle stepper still records that it passed THROUGH the hold.
        self.assertEqual(
            next(s["state"] for s in run_progress(run) if s["key"] == "held"), "done"
        )

    def test_risk_check_rejects_closed_run(self):
        self._login("operator@payrolla.com")
        self.run.status = APPROVED
        db.session.commit()
        resp = self.client.post(f"/oversight/runs/{self.run_id}/risk-check")
        self.assertEqual(resp.status_code, 302)  # bounced to detail, no change
        self.assertEqual(db.session.get(PayrollRun, self.run_id).status, APPROVED)

    def test_tenant_user_cannot_reach_oversight(self):
        self._login("admin@msc.com")  # a tenant user
        resp = self.client.get("/oversight/risk")
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(resp.headers["Location"].endswith("/company"))

    def test_paging_past_the_end_does_not_claim_the_queue_is_clear(self):
        """`paginate` uses error_out=False, so a ?page= past the end is an empty
        page rather than a 404 — and the template's `if page.items` then rendered
        the all-clear. An operator who typed a page number was told no payroll
        was held while one sat in the queue. Empty PAGE and empty QUEUE are
        different states and must read differently."""
        self._login("operator@payrolla.com")
        self.client.post(f"/oversight/runs/{self.run_id}/risk-check")
        self.assertEqual(db.session.get(PayrollRun, self.run_id).status, HELD)

        html = self.client.get("/oversight/risk?page=9999").get_data(as_text=True)
        self.assertNotIn("No runs are currently held", html)
        self.assertIn("past the end of the queue", html)
        self.assertIn("still held for review", html)

    def test_a_junk_order_falls_back_rather_than_raising(self):
        """The order arrives from a URL a user can edit."""
        self._login("operator@payrolla.com")
        self.client.post(f"/oversight/runs/{self.run_id}/risk-check")
        for value in ("", "nonsense", "DROP TABLE payroll_run"):
            resp = self.client.get(f"/oversight/risk?order={value}")
            self.assertEqual(resp.status_code, 200)
            self.assertIn("Runs held for review", resp.get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()
