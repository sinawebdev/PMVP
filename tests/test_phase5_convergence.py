"""Phase 5 acceptance criteria — one implementation per decision surface.

The criteria are structural ("exactly one implementation shared by both
portals"), so these tests assert it two ways: at the seam, that both sides
resolve to the same object; and at the response, that both portals actually emit
the shared component's markup rather than a look-alike of their own.

They also pin the three drifts the duplication had already produced, because a
regression here looks exactly like the bug that was fixed:

  * the tenant distribute page had no `scheduled` batch state and polled a
    far-future scheduled batch every three seconds
  * the tenant notification inbox had no click-through to the run it was
    telling you about
  * the operator had no reports surface at all — four buttons, no reason given
"""

import os
import unittest

os.environ["SKIP_DOTENV"] = "true"
os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["SEED_DEMO_DATA"] = "true"
os.environ["PERSISTENCE_REQUIRED"] = "false"

from app import create_app, db  # noqa: E402
from app.models import Notification, PayrollRun, User  # noqa: E402


class Phase5TestCase(unittest.TestCase):
    def setUp(self):
        self.app = create_app()
        self.app.config["TESTING"] = True
        self.app.config["WTF_CSRF_ENABLED"] = False
        self.client = self.app.test_client()
        self.ctx = self.app.app_context()
        self.ctx.push()
        self.tenant = User.query.filter_by(email="admin@msc.com").first()
        self.run = PayrollRun.query.filter_by(
            client_company_id=self.tenant.client_company_id
        ).first()

    def tearDown(self):
        db.session.remove()
        self.ctx.pop()

    def _login(self, email):
        self.assertEqual(
            self.client.post(
                "/login", data={"email": email, "password": "password123"}
            ).status_code,
            302,
            f"could not sign in as {email}",
        )

    def _logout(self):
        self.client.get("/logout")


class OneDeliveryImplementationTests(Phase5TestCase):
    def test_both_portals_resolve_to_the_same_context_builder(self):
        import app.client as client_module
        import app.distribution as distribution_module
        from app.distribution.status import delivery_status_context

        self.assertIs(distribution_module._run_status_context, delivery_status_context)
        self.assertIs(client_module._distribute_context, delivery_status_context)

    def test_the_duplicated_private_helpers_are_gone(self):
        """`_latest_delivery`/`_latest_batch` were byte-identical in both modules."""
        import app.client as client_module

        self.assertFalse(hasattr(client_module, "_latest_batch"))

    def test_both_portals_render_the_shared_delivery_table(self):
        self._login("admin@payrolla.com")
        operator = self.client.get(f"/distribution/run/{self.run.id}").get_data(as_text=True)
        self._logout()

        self._login("admin@msc.com")
        tenant = self.client.get(
            f"/company/runs/{self.run.id}/distribute"
        ).get_data(as_text=True)
        self._logout()

        for body, who in ((operator, "operator"), (tenant, "tenant")):
            self.assertIn("ds-table", body, f"{who} is not rendering the shared table")
            self.assertIn("ds-pill", body, f"{who} is not rendering the shared badges")

    def test_scope_still_separates_what_each_portal_may_do(self):
        """One implementation must not mean one set of affordances."""
        self._login("admin@payrolla.com")
        operator = self.client.get(f"/distribution/run/{self.run.id}").get_data(as_text=True)
        self._logout()

        self._login("admin@msc.com")
        tenant = self.client.get(
            f"/company/runs/{self.run.id}/distribute"
        ).get_data(as_text=True)
        self._logout()

        # Retargeting a worker's delivery channel is an operator action.
        self.assertIn("preferred_channel", operator)
        self.assertNotIn("preferred_channel", tenant)

    def test_tenant_context_now_carries_the_scheduled_keys(self):
        """The tenant copy never computed these, so its template could not
        render a scheduled batch and polled one that was days away."""
        from app.distribution.status import delivery_status_context

        context = delivery_status_context(self.run)
        for key in ("scheduled", "live", "seconds_until_scheduled"):
            self.assertIn(key, context)

    def test_latest_deliveries_takes_one_query_not_one_per_worker(self):
        from sqlalchemy import event

        from app.distribution.status import latest_deliveries_by_item

        item_ids = [item.id for item in self.run.items]
        self.assertGreater(len(item_ids), 1, "need a multi-row run to prove this")

        statements = []
        engine = db.engine

        def _record(conn, cursor, statement, *args):  # pragma: no cover - bookkeeping
            statements.append(statement)

        event.listen(engine, "before_cursor_execute", _record)
        try:
            latest_deliveries_by_item(item_ids)
        finally:
            event.remove(engine, "before_cursor_execute", _record)

        self.assertEqual(len(statements), 1, statements)


class OneNotificationInboxTests(Phase5TestCase):
    def setUp(self):
        super().setUp()
        # Seeded users have empty inboxes, and an empty inbox renders the empty
        # state rather than the shared table — give each plane one to show.
        operator = User.query.filter_by(email="admin@payrolla.com").first()
        for user in (operator, self.tenant):
            db.session.add(
                Notification(
                    user_id=user.id,
                    title="Payslip distribution finished",
                    body="12 sent, 1 failed.",
                    level="info",
                )
            )
        db.session.commit()

    def test_both_inboxes_render_the_shared_table(self):
        self._login("admin@payrolla.com")
        operator = self.client.get("/notifications").get_data(as_text=True)
        self._logout()

        self._login("admin@msc.com")
        tenant = self.client.get("/notifications").get_data(as_text=True)
        self._logout()

        for body, who in ((operator, "operator"), (tenant, "tenant")):
            self.assertIn("Notifications", body, f"{who} inbox did not render")
        # The Company column is the one scoped difference.
        self.assertIn("<th scope=\"col\">Company</th>", operator)
        self.assertNotIn("<th scope=\"col\">Company</th>", tenant)


class OneImportPreviewTests(Phase5TestCase):
    def test_both_previews_share_the_section_macros(self):
        """Asserted on the template source: rendering a preview needs an upload
        on each side, but the criterion is that neither page hand-rolls the
        analysis any more."""
        import io

        for path in (
            "app/templates/payroll_preview.html",
            "app/templates/client/import_preview.html",
        ):
            source = io.open(path, encoding="utf8").read()
            self.assertIn("macros/import_preview.html", source, path)
            self.assertIn("ip.row_warnings", source, path)
            self.assertIn("ip.mapping_table", source, path)
            self.assertIn("ip.raw_rows", source, path)


class OperatorReportsPatternTests(Phase5TestCase):
    def setUp(self):
        super().setUp()
        self._login("admin@payrolla.com")

    def test_operator_has_a_reports_page(self):
        resp = self.client.get(f"/payroll/runs/{self.run.id}/reports")
        self.assertEqual(resp.status_code, 200)

    def test_it_uses_the_same_grouped_pattern_as_the_client_screen(self):
        body = self.client.get(
            f"/payroll/runs/{self.run.id}/reports"
        ).get_data(as_text=True)
        # The pattern's three parts: purpose, availability, preview.
        self.assertIn("rp-head", body)
        self.assertIn("rp-why", body)
        for title in ("Payroll export", "Bank listing", "Wages sheet", "GRA PAYE schedule"):
            self.assertIn(title, body)

    def test_the_lifecycle_side_effect_is_stated_not_hidden(self):
        """`payroll.export` sets the run to Processed. On a row of four
        identical buttons that was invisible."""
        body = self.client.get(
            f"/payroll/runs/{self.run.id}/reports"
        ).get_data(as_text=True)
        self.assertIn("marks the run Processed", body)

    def test_an_unavailable_export_says_why(self):
        from app.models import ClientCompany

        company = ClientCompany(name="Empty Reports Co", status="Active")
        db.session.add(company)
        db.session.commit()
        empty = PayrollRun(
            month="July", year=2024, status="Draft",
            client_company_id=company.id, total_workers=0,
        )
        db.session.add(empty)
        db.session.commit()

        body = self.client.get(
            f"/payroll/runs/{empty.id}/reports"
        ).get_data(as_text=True)
        self.assertIn("no payroll rows", body)
        self.assertIn("disabled", body)

    def test_the_run_page_links_to_reports_instead_of_listing_four_exports(self):
        body = self.client.get(f"/payroll/runs/{self.run.id}").get_data(as_text=True)
        self.assertIn(f"/payroll/runs/{self.run.id}/reports", body)
        self.assertNotIn(f'href="/payroll/runs/{self.run.id}/export/bank-listing"', body)
        self.assertNotIn(f'href="/payroll/runs/{self.run.id}/export/wages-sheet"', body)


if __name__ == "__main__":
    unittest.main()
