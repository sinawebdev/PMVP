"""Phase 2 — operator dashboard: a held payroll reaches the operator.

Reuses existing state (risk_status/status + PayslipDelivery) — no new business
logic. Verifies a held run surfaces on the dashboard through the surfaces the
redesign kept (the Risk & action panel and the consolidated company table)
rather than through the three run lists it removed.
"""

import os
import unittest

os.environ["SKIP_DOTENV"] = "true"
os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["SEED_DEMO_DATA"] = "true"
os.environ["PERSISTENCE_REQUIRED"] = "false"

from app import create_app, db  # noqa: E402
from app.models import ClientCompany, PayrollRun  # noqa: E402
from app.payroll_status import HELD, PROCESSED  # noqa: E402
from app.platform_dashboard import period_scoped_trend  # noqa: E402


class OperatorDashboardTestCase(unittest.TestCase):
    def setUp(self):
        self.app = create_app()
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()
        self.ctx = self.app.app_context()
        self.ctx.push()
        self.assertEqual(
            self.client.post(
                "/login", data={"email": "admin@payrolla.com", "password": "password123"}
            ).status_code,
            302,
        )

    def tearDown(self):
        self.ctx.pop()

    def test_dashboard_surfaces_held_and_completed(self):
        company = ClientCompany.query.first()
        db.session.add_all([
            PayrollRun(
                month="March", year=2024, status=HELD, client_company_id=company.id,
                total_workers=3, total_net_pay=1000, risk_status="held",
                risk_reasons="net pay variance",
            ),
            PayrollRun(
                month="February", year=2024, status=PROCESSED, client_company_id=company.id,
                total_workers=3, total_net_pay=900,
            ),
        ])
        db.session.commit()

        html = self.client.get("/dashboard").get_data(as_text=True)
        # Where a held run surfaces, after the operator dashboard redesign.
        #
        # There is no longer a "Held Payrolls" stat card, a "Held for Risk
        # Review" list, a "Recent Payroll Runs" list or a "Recently Completed"
        # list. Three lists of runs on a dashboard restated what the risk panel,
        # the activity timeline and the company table already say, and each had
        # a page of its own to live on. A hold now shows up in exactly two
        # places here — as an action in the Risk & action panel, and as the
        # company's latest run status in the table — plus a count on the header's
        # Risk queue button. The detail (which run, why) lives one click away in
        # the risk queue, which is where it can actually be acted on.
        self.assertIn("payroll held for risk review", html)   # risk signal
        self.assertIn("Review queue", html)                   # ...and its action
        self.assertIn("Risk queue (1)", html)                 # header count
        self.assertIn("Company payroll overview", html)       # consolidated table
        self.assertIn("Held", html)                           # the run's status badge
        for gone in ("Held for Risk Review", "Recently Completed", "Recent Payroll Runs"):
            with self.subTest(panel=gone):
                self.assertNotIn(gone, html)


class PeriodScopedTrendTestCase(unittest.TestCase):
    """A delta must describe the number it is printed under.

    The portfolio trend is built from the periods that HAVE runs, so selecting a
    month with no payroll leaves the series ending on an older month. Pairing
    that series' change with the selected month's total rendered "GH₵ 0.00"
    beneath a green "▲ +3.3% vs previous month" — two true statements about two
    different questions, side by side, on the dashboard's primary panel.
    """

    def _trend(self, last_label, change=0.033):
        return {
            "points": [
                {"label": "Jul", "full_label": "July 2026", "value": 100, "pct": 20},
                {"label": last_label[:3], "full_label": last_label, "value": 103, "pct": 80},
            ],
            "change": change,
            "window_change": change,
            "periods_covered": 2,
        }

    def test_change_survives_when_the_trend_ends_at_the_selected_period(self):
        scoped = period_scoped_trend(self._trend("August 2026"), "August 2026")
        self.assertAlmostEqual(scoped["change"], 0.033)

    def test_change_is_dropped_when_the_selected_period_has_no_runs(self):
        scoped = period_scoped_trend(self._trend("August 2026"), "January 2027")
        self.assertIsNone(scoped["change"])

    def test_points_are_never_altered_and_the_input_is_not_mutated(self):
        """The history is still real history — only the claim about the
        selected period is withdrawn."""
        original = self._trend("August 2026")
        scoped = period_scoped_trend(original, "January 2027")
        self.assertEqual(scoped["points"], original["points"])
        self.assertAlmostEqual(original["change"], 0.033)

    def test_an_empty_series_yields_no_change(self):
        empty = {"points": [], "change": None, "periods_covered": 0}
        self.assertIsNone(period_scoped_trend(empty, "January 2027")["change"])


if __name__ == "__main__":
    unittest.main()
