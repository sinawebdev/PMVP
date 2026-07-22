"""Phase 2 — Risk & Validation Summary panel.

build_recommendations() distills FOUR already-existing signals into one list
of plain-English next steps: the risk gate's per-check detail (evaluate_run,
previously only visible in a hover tooltip), row-level validation warnings
(PayrollRun.warning_count, previously not shown on the detail page at all),
the possible-duplicate match (find_possible_duplicates), and the
comparison-to-previous flags (compare_to_previous). Pure/read-only — no new
rule, no lifecycle decision.
"""

import os
import unittest

os.environ["SKIP_DOTENV"] = "true"
os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["SEED_DEMO_DATA"] = "true"
os.environ["PERSISTENCE_REQUIRED"] = "false"

from app import create_app, db  # noqa: E402
from app.models import ClientCompany, PayrollItem, PayrollRun  # noqa: E402
from app.risk import build_recommendations, compare_to_previous, evaluate_run  # noqa: E402


class BuildRecommendationsTestCase(unittest.TestCase):
    def setUp(self):
        self.app = create_app()
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()
        self.ctx = self.app.app_context()
        self.ctx.push()
        self.assertEqual(
            self.client.post(
                "/login", data={"email": "admin@chrisnat.local", "password": "password123"}
            ).status_code,
            302,
        )
        # A dedicated, empty company — the seeded ones already carry closed
        # runs with real totals, which would contaminate the risk-gate math
        # (net-pay/headcount variance vs an unrelated seeded baseline).
        fresh = ClientCompany(name="Recs Test Co", status="Active")
        db.session.add(fresh)
        db.session.commit()
        self.company = fresh

    def tearDown(self):
        self.ctx.pop()

    def _run(self, *, net=0, workers=0, month="January", year=2024, status="Draft"):
        run = PayrollRun(
            month=month, year=year, status=status, client_company_id=self.company.id,
            total_workers=workers, total_net_pay=net,
        )
        db.session.add(run)
        db.session.flush()
        return run

    def test_brand_new_client_recommends_review(self):
        # A fresh company's first run always trips the new-client rule.
        fresh = ClientCompany(name="Fresh Co", status="Active")
        db.session.add(fresh)
        db.session.flush()
        run = PayrollRun(
            month="January", year=2024, status="Draft", client_company_id=fresh.id,
            total_workers=5, total_net_pay=1000,
        )
        db.session.add(run)
        db.session.flush()
        db.session.commit()
        verdict = evaluate_run(run)
        recs = build_recommendations(run, verdict, compare_to_previous(run), [])
        self.assertTrue(any("first" in r and "run" in r for r in recs))

    def test_no_signals_returns_empty(self):
        # Past the new-client window, no warnings, no duplicates, no baseline
        # to compare against -> nothing to recommend.
        for _ in range(3):
            self._run(net=1000, workers=10)
        db.session.commit()
        run = self._run(net=1000, workers=10)
        db.session.commit()
        verdict = evaluate_run(run)
        self.assertFalse(verdict.held)
        recs = build_recommendations(run, verdict, {"previous": None, "rows": []}, [])
        self.assertEqual(recs, [])

    def test_row_level_warnings_included(self):
        for _ in range(3):
            self._run(net=1000, workers=10)
        db.session.commit()
        run = self._run(net=1000, workers=10)
        db.session.add(PayrollItem(payroll_run_id=run.id, validation_status="Warning"))
        db.session.add(PayrollItem(payroll_run_id=run.id, validation_status="OK"))
        db.session.commit()
        verdict = evaluate_run(run)
        recs = build_recommendations(run, verdict, {"previous": None, "rows": []}, [])
        self.assertTrue(any("1 row-level warning" in r for r in recs))

    def test_duplicates_included(self):
        for _ in range(3):
            self._run(net=1000, workers=10)
        db.session.commit()
        run = self._run(net=1000, workers=10)
        db.session.commit()
        verdict = evaluate_run(run)
        other = self._run(net=1000, workers=10)
        recs = build_recommendations(run, verdict, {"previous": None, "rows": []}, [other])
        self.assertTrue(any("possible duplicate" in r for r in recs))

    def test_comparison_flags_included(self):
        # Two Draft runs to clear the new-client window, plus one CLOSED run
        # so compare_to_previous has a baseline (_previous_closed_run only
        # considers Approved/Processed runs).
        self._run(net=1000, workers=10, month="January")
        self._run(net=1000, workers=10, month="February")
        self._run(net=1000, workers=10, month="March", status="Approved")
        db.session.commit()
        run = self._run(net=1200, workers=10, month="April")
        db.session.commit()
        verdict = evaluate_run(run)
        comparison = compare_to_previous(run)
        self.assertIsNotNone(comparison["previous"])
        recs = build_recommendations(run, verdict, comparison, [])
        self.assertTrue(any("Unusual change" in r and "Net pay" in r for r in recs))


class RiskSummaryPanelRenderTestCase(unittest.TestCase):
    def setUp(self):
        self.app = create_app()
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()
        self.ctx = self.app.app_context()
        self.ctx.push()
        self.assertEqual(
            self.client.post(
                "/login", data={"email": "admin@chrisnat.local", "password": "password123"}
            ).status_code,
            302,
        )
        self.company = ClientCompany.query.first()

    def tearDown(self):
        self.ctx.pop()

    def _run(self, **kw):
        defaults = dict(month="January", year=2024, status="Draft", client_company_id=self.company.id)
        defaults.update(kw)
        run = PayrollRun(**defaults)
        db.session.add(run)
        db.session.commit()
        return run

    def test_panel_renders_on_detail_page(self):
        run = self._run(total_workers=5, total_net_pay=1000)
        html = self.client.get(f"/payroll/runs/{run.id}").get_data(as_text=True)
        self.assertIn("Risk &amp; Validation Summary", html)
        self.assertIn("Risk gate checks", html)

    def test_panel_shows_no_warnings_when_none(self):
        run = self._run(total_workers=5, total_net_pay=1000)
        html = self.client.get(f"/payroll/runs/{run.id}").get_data(as_text=True)
        self.assertIn("No row-level validation warnings", html)

    def test_panel_shows_warning_count_and_grid_link(self):
        run = self._run(total_workers=2, total_net_pay=1000)
        db.session.add(PayrollItem(payroll_run_id=run.id, validation_status="Warning"))
        db.session.add(PayrollItem(payroll_run_id=run.id, validation_status="OK"))
        db.session.commit()
        html = self.client.get(f"/payroll/runs/{run.id}").get_data(as_text=True)
        self.assertIn("1 of 2 row(s) flagged", html)
        self.assertIn('href="#payroll-items-grid"', html)
        self.assertIn('id="payroll-items-grid"', html)

    def test_panel_lists_recommendations_when_present(self):
        # A brand-new client's first run always trips the new-client rule, so
        # recommendations is guaranteed non-empty regardless of seed state.
        fresh = ClientCompany(name="Fresh Recommendations Co", status="Active")
        db.session.add(fresh)
        db.session.commit()
        run = self._run(client_company_id=fresh.id, total_workers=2, total_net_pay=1000)
        html = self.client.get(f"/payroll/runs/{run.id}").get_data(as_text=True)
        self.assertIn("Recommendations", html)


if __name__ == "__main__":
    unittest.main()
