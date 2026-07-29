"""Operations & Onboarding, Phase 4 — the Payrolla admin executive overview.

Two halves: the pure aggregation in app/analytics.py + app/events.py (tested
directly, no HTTP), and the dashboard actually rendering it. The charts are
server-rendered SVG/CSS from the shared macros, so "it renders" is a real
assertion here — there is no client-side library to blame.
"""

import os
import re
import unittest
from datetime import datetime, timedelta, timezone

os.environ["SKIP_DOTENV"] = "true"
os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["SEED_DEMO_DATA"] = "true"
os.environ["PERSISTENCE_REQUIRED"] = "false"

from app import create_app, db  # noqa: E402
from app.analytics import (  # noqa: E402
    TOP_CLIENT_LIMIT,
    client_growth,
    platform_dashboard_analytics,
    status_distribution,
    top_clients,
)
from app.events import platform_activity  # noqa: E402
from app.models import AuditTrail, ClientCompany, Employee, PayrollRun, User  # noqa: E402


_MACRO_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "app", "templates", "macros", "dashboard.html",
)


def _icon_names_the_macro_can_draw():
    """The icon names macros/dashboard.html::icon has a path for.

    Read from the macro itself rather than duplicated here: an icon name the
    macro does not know renders as the fallback glyph, silently, and a copy of
    the list in this file would be the thing that let that happen.
    """
    with open(_MACRO_FILE, encoding="utf-8") as handle:
        return set(re.findall(r"name == '([a-z-]+)'", handle.read()))


class FakeRun:
    def __init__(self, month, year, status="Approved", net=0.0, gross=0.0, employer=0.0):
        self.month = month
        self.year = year
        self.status = status
        self.total_net_pay = net
        self.total_gross_pay = gross
        self.total_ssnit_employer = employer
        self.total_workers = 1


class FakeCompany:
    def __init__(self, name, code, runs=(), employees=0, status="Active", created_at=None):
        self.name = name
        self.company_code = code
        self.payroll_runs = list(runs)
        self.employees = [type("E", (), {"status": "Active"})() for _ in range(employees)]
        self.status = status
        self.created_at = created_at


class PlatformAnalyticsUnitTestCase(unittest.TestCase):
    """The aggregation, exercised without a database or a request."""

    def setUp(self):
        self.msc = FakeCompany(
            "MSC Limited", "MSC",
            runs=[
                FakeRun("May", 2026, net=1000, gross=1200, employer=150),
                FakeRun("June", 2026, net=1500, gross=1800, employer=220),
            ],
            employees=4,
            created_at=datetime(2026, 1, 5),
        )
        self.acme = FakeCompany(
            "Acme Manufacturing Ltd", "ACME",
            runs=[FakeRun("June", 2026, status="Held", net=500, gross=600, employer=70)],
            employees=2,
            created_at=datetime(2026, 3, 9),
        )
        self.dormant = FakeCompany(
            "Dormant Ltd", "DORM", status="Inactive", created_at=datetime(2026, 4, 1)
        )
        self.companies = [self.msc, self.acme, self.dormant]

    def _analytics(self, **kwargs):
        return platform_dashboard_analytics(
            self.companies, expense_total=300, active_employees=6, **kwargs
        )

    def test_revenue_trend_sums_every_company_per_month(self):
        trend = self._analytics()["revenue_trend"]
        self.assertEqual([p["label"] for p in trend], ["May", "Jun"])
        self.assertEqual(trend[0]["value"], 1000)   # MSC only
        self.assertEqual(trend[1]["value"], 2000)   # MSC 1500 + Acme 500
        self.assertEqual(trend[1]["pct"], 100)      # scaled against the peak

    def test_companies_are_ranked_by_payroll_cost(self):
        ranked = self._analytics()["companies_by_cost"]
        self.assertEqual([p["label"] for p in ranked], ["MSC", "ACME", "DORM"])
        self.assertEqual(ranked[0]["full_label"], "MSC Limited")
        # gross + employer SSF across the charted months
        self.assertEqual(ranked[0]["value"], 1200 + 150 + 1800 + 220)
        self.assertEqual(ranked[1]["value"], 600 + 70)
        self.assertEqual(ranked[2]["value"], 0)
        self.assertLessEqual(len(ranked), TOP_CLIENT_LIMIT)

    def test_status_distribution_counts_runs_not_money(self):
        mix = self._analytics()["status_mix"]
        self.assertEqual(mix["total"], 3)
        by_label = {s["label"]: s for s in mix["slices"]}
        self.assertEqual(by_label["Approved"]["value"], 2)
        self.assertEqual(by_label["Held"]["value"], 1)
        self.assertEqual(by_label["Held"]["tone"], "warn")
        self.assertEqual(round(sum(s["pct"] for s in mix["slices"])), 100)
        self.assertEqual(status_distribution([]), {"slices": [], "total": 0})

    def test_client_growth_is_cumulative_and_ends_at_the_company_count(self):
        points = client_growth(self.companies)
        self.assertEqual([p["value"] for p in points], [1, 2, 3])
        self.assertEqual([p["label"] for p in points], ["Jan", "Mar", "Apr"])
        # A month with no signings must hold the line, not drop it.
        self.assertEqual(points[-1]["value"], len(self.companies))

    def test_client_growth_respects_the_selected_period_cutoff(self):
        points = client_growth(self.companies, cutoff=(2026, 3))
        self.assertEqual([p["value"] for p in points], [1, 2])

    def test_quick_stats(self):
        stats = self._analytics()["stats"]
        self.assertEqual(stats["active_companies"], 2)      # Dormant is Inactive
        self.assertEqual(stats["active_employees"], 6)
        self.assertEqual(stats["payroll_runs"], 3)
        self.assertEqual(stats["payroll_total"], 3000)
        self.assertEqual(stats["expense_total"], 300)
        self.assertEqual(stats["average_per_company"], 1500)  # per ACTIVE company

    def test_average_per_company_is_zero_with_no_active_companies(self):
        stats = platform_dashboard_analytics(
            [], expense_total=0, active_employees=0
        )["stats"]
        self.assertEqual(stats["average_per_company"], 0)
        self.assertEqual(stats["active_companies"], 0)

    def test_top_clients_table_shape(self):
        rows = top_clients(self.companies)
        self.assertEqual(rows[0]["company"], "MSC Limited")
        self.assertEqual(rows[0]["code"], "MSC")
        self.assertEqual(rows[0]["employees"], 4)
        self.assertEqual(rows[0]["payroll"], 2500)
        self.assertEqual(rows[0]["last_run"], "June 2026")
        self.assertEqual(rows[0]["last_run_status"], "Approved")
        self.assertIsNone(rows[-1]["last_run"])   # Dormant has no runs

    def test_cutoff_truncates_every_series(self):
        analytics = self._analytics(cutoff=(2026, 5))
        self.assertEqual([p["label"] for p in analytics["revenue_trend"]], ["May"])
        self.assertEqual(analytics["stats"]["payroll_runs"], 1)
        self.assertEqual(analytics["status_mix"]["total"], 1)

    def test_no_runs_means_no_data(self):
        analytics = platform_dashboard_analytics(
            [self.dormant], expense_total=0, active_employees=0
        )
        self.assertFalse(analytics["has_data"])
        self.assertEqual(analytics["revenue_trend"], [])


class PlatformDashboardRenderTestCase(unittest.TestCase):
    """The dashboard route actually renders the overview."""

    def setUp(self):
        self.app = create_app()
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()
        self.ctx = self.app.app_context()
        self.ctx.push()
        self.client.post(
            "/login", data={"email": "admin@payrolla.com", "password": "password123"}
        )

    def tearDown(self):
        self.ctx.pop()

    def test_dashboard_renders_every_executive_section(self):
        # The operator dashboard's information architecture, asserted as
        # headings. Five KPI tiles (one per distinct question), one dominant
        # chart plus two supporting ones, one consolidated company table, and a
        # two-panel right rail — see app/platform_dashboard.py for the reasoning
        # behind each split.
        page = self.client.get("/dashboard")
        self.assertEqual(page.status_code, 200)
        body = page.get_data(as_text=True)
        for heading in (
            "Payroll cost",
            "Workforce",
            "Client companies",
            "Client expenses",
            "Payslip delivery",
            "Risk &amp; action",
            "Payroll cost trend",
            "Companies by payroll cost",
            "Payroll status distribution",
            "Company payroll overview",
            "Recent activity",
            "Statutory exposure",
        ):
            with self.subTest(heading=heading):
                self.assertIn(heading, body)

    def test_dashboard_carries_no_removed_or_duplicated_section(self):
        """The redesign's subtractions, asserted so they cannot creep back.

        Top Clients and Payroll Cost Per Client ranked the same companies by two
        measures of the same money; they are one table now. Held / Recent /
        Recently Completed were three run lists restating the risk panel, the
        timeline and that table. Client growth is a board question, not one an
        operator acts on during a payroll day — it survives as the caption on the
        Client companies tile. And the Approval Queue button appeared twice on
        one screen, which reads as two different actions until you check.
        """
        body = self.client.get("/dashboard").get_data(as_text=True)
        for gone in (
            "Top Clients",
            "Payroll Cost Per Client",
            "Held for Risk Review",
            "Recent Payroll Runs",
            "Recently Completed",
            "Client growth",
            "Approval Queue",
        ):
            with self.subTest(removed=gone):
                self.assertNotIn(gone, body)
        # Exactly three charts, and exactly five KPI tiles.
        self.assertEqual(body.count('<figure class="chart'), 3)
        self.assertEqual(body.count('class="kpi"'), 5)

    def test_charts_are_server_rendered_with_no_javascript_library(self):
        body = self.client.get("/dashboard").get_data(as_text=True)
        self.assertIn("<svg", body)              # inline SVG, drawn server-side
        self.assertIn("hbar-fill", body)         # the CSS ranking bars
        for library in ("chart.js", "d3.min.js", "apexcharts", "highcharts"):
            self.assertNotIn(library, body.lower())

    def test_dashboard_is_platform_only(self):
        self.client.get("/logout")
        self.client.post(
            "/login", data={"email": "admin@msc.com", "password": "password123"}
        )
        resp = self.client.get("/dashboard")
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(resp.headers["Location"].endswith("/company"))

    def test_activity_timeline_surfaces_a_newly_onboarded_company(self):
        resp = self.client.post(
            "/clients/add",
            data={
                "name": "Timeline Test Ltd",
                "company_code": "TLT",
                "email": "ops@timelinetest.com",
                "status": "Active",
            },
        )
        self.assertEqual(resp.status_code, 302)
        body = self.client.get("/dashboard").get_data(as_text=True)
        self.assertIn("Client company onboarded", body)
        self.assertIn("Timeline Test Ltd", body)

    def test_platform_activity_is_bounded_and_newest_first(self):
        admin = User.query.filter_by(email="admin@payrolla.com").first()
        base = datetime.now(timezone.utc).replace(tzinfo=None)
        for index in range(15):
            db.session.add(
                AuditTrail(
                    user_id=admin.id,
                    user_role=admin.role,
                    action="Payroll approval",
                    related_record_type="PayrollRun",
                    related_record_id=index,
                    notes=f"approval {index}",
                    created_at=base + timedelta(minutes=index),
                )
            )
        db.session.commit()

        items = platform_activity(limit=5)
        self.assertEqual(len(items), 5)
        stamps = [item["at"] for item in items]
        self.assertEqual(stamps, sorted(stamps, reverse=True))
        # Every item carries the presentation fields the template reads — in the
        # vocabulary the component that reads them actually understands.
        #
        # This used to assert a "bi-" prefix (Bootstrap Icons). Nothing renders
        # this feed that way: macros/dashboard.html::activity_item draws an
        # inline SVG by name from its own icon set and tones the node from
        # `ok | warn | danger | brand | muted`, so every operator-dashboard entry
        # fell through to the generic glyph with no tone. Both feeds now read the
        # one table in app/events.py, which is in those terms.
        drawable = _icon_names_the_macro_can_draw()
        for item in items:
            self.assertFalse(item["icon"].startswith("bi-"))
            self.assertIn(item["icon"], drawable)
            self.assertIn(item["tone"], {"ok", "warn", "danger", "brand", "muted"})

    def test_dashboard_matches_the_underlying_records(self):
        page = self.client.get("/dashboard")
        self.assertEqual(page.status_code, 200)
        body = page.get_data(as_text=True)
        active_companies = ClientCompany.query.filter_by(status="Active").count()
        active_employees = Employee.query.filter_by(status="Active").count()
        self.assertIn(f">{active_companies}<", body)
        self.assertIn(f">{active_employees}<", body)
        self.assertTrue(PayrollRun.query.count() >= 1)


class TimelineLookTestCase(unittest.TestCase):
    """Every timeline entry must be drawable by the component that draws it.

    Both feeds render through macros/dashboard.html::activity_item, which takes
    an icon NAME from its own inline SVG set and a tone from a fixed five. A
    mapping in any other vocabulary does not fail loudly — it renders a generic
    glyph with no tone, which is how the operator feed ran for a while.
    """

    TONES = {"ok", "warn", "danger", "brand", "muted"}

    def test_every_mapped_title_renders(self):
        from app.events import (
            PLATFORM_TIMELINE_ACTIONS,
            TENANT_TIMELINE_ACTIONS,
            timeline_look,
        )

        drawable = _icon_names_the_macro_can_draw()
        self.assertIn("shield-check", drawable)   # the helper found a real set
        for title in sorted(set(PLATFORM_TIMELINE_ACTIONS) | set(TENANT_TIMELINE_ACTIONS)):
            icon, tone = timeline_look(title)
            with self.subTest(title=title):
                self.assertIn(icon, drawable)
                self.assertIn(tone, self.TONES)

    def test_unmapped_title_falls_back_to_something_drawable(self):
        from app.events import timeline_look

        icon, tone = timeline_look("Some event nobody has mapped yet")
        self.assertIn(icon, _icon_names_the_macro_can_draw())
        self.assertIn(tone, self.TONES)


if __name__ == "__main__":
    unittest.main()
