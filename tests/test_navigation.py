"""Phase 3 — chrome and navigation.

Pins the three properties the phase is judged on:

  * no authenticated page carries standing instructional prose in its chrome;
  * the sidebar is a fixed set of domain nouns whose length does not grow with
    the customer count;
  * active state comes from route metadata, so adding a route needs no template
    edit to highlight correctly.
"""

import os
import re
import unittest

os.environ["SKIP_DOTENV"] = "true"
os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["SEED_DEMO_DATA"] = "true"
os.environ["PERSISTENCE_REQUIRED"] = "false"

from app import create_app, db  # noqa: E402
from app.models import ClientCompany  # noqa: E402
from app.navigation import NAV, active_nav_key, visible_nav  # noqa: E402
from app.seed import DEMO_PASSWORD  # noqa: E402

MAX_SIDEBAR_ITEMS = 7


class SidebarShapeTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app()
        self.app.config["TESTING"] = True
        self.http = self.app.test_client()
        self.ctx = self.app.app_context()
        self.ctx.push()

    def tearDown(self):
        db.session.remove()
        self.ctx.pop()

    def _login(self):
        self.http.post(
            "/login", data={"email": "admin@payrolla.com", "password": DEMO_PASSWORD}
        )

    def _sidebar(self, path="/dashboard"):
        """Just the shell's sidebar. Client names legitimately appear in page
        *content* (the dashboard ranks companies); the point of this phase is
        that they are no longer in the navigation."""
        body = self.http.get(path).get_data(as_text=True)
        start = body.find('<aside class="sidebar"')
        self.assertNotEqual(start, -1, "sidebar not rendered")
        end = body.find("</aside>", start)
        return body[start:end]

    def test_at_most_seven_top_level_items(self):
        self.assertLessEqual(len(NAV), MAX_SIDEBAR_ITEMS)
        for role in ("admin", "md", "payrolla_admin", "payroll_officer"):
            with self.subTest(role=role):
                self.assertLessEqual(len(visible_nav(role)), MAX_SIDEBAR_ITEMS)

    def test_sidebar_length_does_not_grow_with_the_client_count(self):
        """The old sidebar listed every client company, so navigation got longer
        as the business got bigger. Adding clients must change nothing."""
        self._login()
        base_links = self._sidebar().count('class="nav-link"')

        for i in range(25):
            db.session.add(
                ClientCompany(
                    name=f"Scale Co {i:02d}",
                    company_code=f"NAV{i:02d}",
                    email=f"c{i}@nav.test",
                    status="Active",
                )
            )
        db.session.commit()

        sidebar = self._sidebar()
        self.assertEqual(base_links, sidebar.count('class="nav-link"'))
        self.assertNotIn("Scale Co 00", sidebar, "client names must not be in the nav")

    def test_no_client_list_markup_remains_in_the_shell(self):
        self._login()
        sidebar = self._sidebar()
        for marker in ("client-tabs", "client-tab", "Client Payroll Tabs"):
            self.assertNotIn(marker, sidebar)


class ActiveStateComesFromRouteMetadataTests(unittest.TestCase):
    def test_blueprint_decides_the_active_item(self):
        cases = [
            ("main.dashboard", "main", "dashboard"),
            ("main.clients", "main", "clients"),
            ("employees.roster", "employees", "clients"),
            ("payroll.runs", "payroll", "payroll"),
            ("payroll.detail", "payroll", "payroll"),
            ("oversight.risk_queue", "oversight", "payroll"),
            ("payslip.index", "payslip", "payslips"),
            ("distribution.dashboard", "distribution", "payslips"),
            ("audit.audit_trail", "audit", "audit"),
            ("statutory.index", "statutory", "statutory"),
            ("notifications.inbox", "notifications", "notifications"),
        ]
        for endpoint, blueprint, expected in cases:
            with self.subTest(endpoint=endpoint):
                self.assertEqual(active_nav_key(endpoint, blueprint), expected)

    def test_a_brand_new_route_needs_no_template_edit(self):
        """The acceptance criterion: a route added to an existing blueprint
        resolves to the right nav item without anyone touching base.html."""
        self.assertEqual(
            active_nav_key("payroll.some_route_invented_tomorrow", "payroll"),
            "payroll",
        )
        self.assertEqual(
            active_nav_key("distribution.brand_new_view", "distribution"), "payslips"
        )

    def test_unknown_blueprint_is_simply_inactive(self):
        self.assertIsNone(active_nav_key("auth.login", "auth"))
        self.assertIsNone(active_nav_key(None, None))


class NoStandingProseInChromeTests(unittest.TestCase):
    """Task 3.1: the instructional paragraph is gone from the shell. Error pages
    keep theirs — there the text IS the page's message, not chrome."""

    TEMPLATES = os.path.join(os.path.dirname(__file__), os.pardir, "app", "templates")

    def test_only_error_pages_still_define_page_intro(self):
        offenders = []
        for root, _dirs, files in os.walk(self.TEMPLATES):
            for name in files:
                if not name.endswith(".html"):
                    continue
                path = os.path.join(root, name)
                with open(path, encoding="utf-8") as handle:
                    if "block page_intro" in handle.read():
                        rel = os.path.relpath(path, self.TEMPLATES).replace("\\", "/")
                        if not rel.startswith("errors/"):
                            offenders.append(rel)
        self.assertEqual(offenders, [], f"standing prose still in chrome: {offenders}")

    def test_the_shell_no_longer_renders_an_intro_or_a_greeting(self):
        with open(os.path.join(self.TEMPLATES, "base.html"), encoding="utf-8") as handle:
            shell = handle.read()
        # Strip comments before asserting — the removal is explained in one.
        shell = re.sub(r"\{#.*?#\}", "", shell, flags=re.S)
        self.assertNotIn('class="page-intro"', shell)
        self.assertNotIn('class="signed-in', shell)


if __name__ == "__main__":
    unittest.main()
