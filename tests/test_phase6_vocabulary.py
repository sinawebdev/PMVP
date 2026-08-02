"""Phase 6 acceptance criteria — one visual vocabulary, product-wide.

The criteria are about ambiguity, not eradication: a deprecated class name must
have "exactly one meaning across the codebase, or [be] fully removed". So these
tests assert against the stylesheets themselves — a class defined twice, in two
shells, with two different rule sets IS the two-meanings failure — and against
the rendered pages for the data-point spot-check.

The three collisions this pins, all of which had already caused defects:

  .panel      a padded content card in styles.css, an `overflow-x: auto;
              padding: 4px` table wrapper in portal.css. Phase 5's shared macros
              landed in whichever box the surrounding shell happened to mean;
              `class="panel ip-blockers"` rendered a prose list at 4px padding.
  .toolbar    a control row in the operator shell, the PAGE HEADER in the tenant
              shell (10 of its 12 tenant uses).
  .stat-card  identical visuals in both shells over two incompatible child
              contracts — span/strong vs .n/.l, in opposite order — so a stat
              card written for one portal rendered as unstyled text in the other.
"""

import io
import os
import re
import unittest

os.environ["SKIP_DOTENV"] = "true"
os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["SEED_DEMO_DATA"] = "true"
os.environ["PERSISTENCE_REQUIRED"] = "false"

from app import create_app, db  # noqa: E402
from app.models import Employee, User  # noqa: E402

SHELL_SHEETS = ("app/static/styles.css", "app/static/portal.css")
CANONICAL = "app/static/components.css"


def _defines(path, klass):
    """Does this stylesheet carry a rule whose selector is bare `.klass`?

    Deliberately ignores compounds (`.stat-card:hover` is part of the same
    definition; `.panel-head` is a different class entirely)."""
    source = io.open(path, encoding="utf8").read()
    pattern = re.compile(
        r"(^|[},]\s*)\.%s\s*(?:,|\{|:hover|::after|\s*>\s*\w)" % re.escape(klass),
        re.M,
    )
    return bool(pattern.search(source))


class OneMeaningPerClassTests(unittest.TestCase):
    """Structural — no HTTP needed, and that is the point: the criterion is a
    property of the stylesheets, not of any one page."""

    DEPRECATED = ("panel", "stat-card")

    def test_each_shared_class_is_defined_exactly_once(self):
        for klass in self.DEPRECATED:
            with self.subTest(klass=klass):
                shells = [p for p in SHELL_SHEETS if _defines(p, klass)]
                self.assertEqual(
                    shells,
                    [],
                    f".{klass} is still defined in {shells} as well as the "
                    "canonical components.css — that is the two-meanings bug",
                )
                self.assertTrue(
                    _defines(CANONICAL, klass),
                    f".{klass} has no canonical definition in {CANONICAL}",
                )

    def test_toolbar_is_not_also_the_tenant_page_header(self):
        """`.toolbar` means a control row. The tenant's title bars are
        `.page-bar`, which is defined once, canonically."""
        import glob

        offenders = []
        for path in glob.glob("app/templates/client/*.html"):
            source = io.open(path, encoding="utf8").read()
            if re.search(r'<div class="toolbar">\s*(<div class="page-head">|<h2>)', source):
                offenders.append(path)
        self.assertEqual(offenders, [], "these still use .toolbar as a page header")
        self.assertTrue(_defines(CANONICAL, "page-bar"))

    def test_no_tenant_template_uses_panel_as_a_table_wrapper(self):
        """The scroll-box meaning has its own canonical name, `.dt-wrap`."""
        import glob

        offenders = []
        for path in glob.glob("app/templates/client/*.html"):
            source = io.open(path, encoding="utf8").read()
            if re.search(r'<div class="panel">\s*<table', source):
                offenders.append(path)
        self.assertEqual(offenders, [])

    def test_the_two_stat_card_child_contracts_became_one(self):
        import glob

        offenders = [
            path
            for path in glob.glob("app/templates/client/*.html")
            if 'class="n"' in io.open(path, encoding="utf8").read()
            or 'class="l"' in io.open(path, encoding="utf8").read()
        ]
        self.assertEqual(
            offenders, [], "these still use the tenant-only .n/.l stat-card contract"
        )


class ContactCompletenessRendersIdenticallyTests(unittest.TestCase):
    """The plan's named spot-check: the same underlying data point must render
    the same way in both portals."""

    def setUp(self):
        self.app = create_app()
        self.app.config["TESTING"] = True
        self.app.config["WTF_CSRF_ENABLED"] = False
        self.client = self.app.test_client()
        self.ctx = self.app.app_context()
        self.ctx.push()
        self.tenant = User.query.filter_by(email="admin@msc.com").first()
        self.company_id = self.tenant.client_company_id
        self.unreachable = Employee(
            client_company_id=self.company_id,
            staff_id="NOCONTACT-1",
            full_name="Unreachable Worker",
            status="Active",
            email=None,
            phone=None,
        )
        db.session.add(self.unreachable)
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        self.ctx.pop()

    def _login(self, email):
        self.assertEqual(
            self.client.post(
                "/login", data={"email": email, "password": "password123"}
            ).status_code,
            302,
        )

    def test_the_predicate_is_one_property_not_three_inline_expressions(self):
        self.assertFalse(self.unreachable.has_contact)
        self.unreachable.phone = "0244000000"
        self.assertTrue(self.unreachable.has_contact)
        self.unreachable.phone = None
        self.unreachable.email = "someone@example.test"
        self.assertTrue(self.unreachable.has_contact)

    def test_momo_alone_is_not_contactable(self):
        """It is a payment destination, not a delivery channel — and the
        import-time warning has always drawn the line there."""
        self.unreachable.momo_number = "0244000000"
        self.assertFalse(self.unreachable.has_contact)

    def test_both_portals_render_the_same_badge(self):
        self._login("admin@payrolla.com")
        operator = self.client.get(
            f"/employees/clients/{self.company_id}/roster"
        ).get_data(as_text=True)
        self.client.get("/logout")

        self._login("admin@msc.com")
        tenant = self.client.get("/company/employees").get_data(as_text=True)
        self.client.get("/logout")

        for body, who in ((operator, "operator"), (tenant, "tenant")):
            self.assertIn("Unreachable Worker", body, f"{who} roster missing the worker")
            self.assertIn(
                "No contact", body, f"{who} roster does not surface contact completeness"
            )
            self.assertIn("ds-pill--warn", body, f"{who} is not using the shared badge")


if __name__ == "__main__":
    unittest.main()
