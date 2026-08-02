"""Phase 4 acceptance criteria for Tasks 4.3, 4.4 and 4.5.

Task 4.1's criterion is pinned separately in test_payroll_detail_bounded.py, and
4.2's in test_mvp.py (the runs page is the list; the upload lives on its own
route). What is left is the three that are about what a screen SAYS, in what
order, without being opened first:

  4.3  the approval queue states why a run is held, and clears it in place
  4.4  the import preview leads with what is wrong, not with a stat wall
  4.5  a company nobody can sign in to says so, on the list and the dashboard
"""

import os
import unittest

os.environ["SKIP_DOTENV"] = "true"
os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["SEED_DEMO_DATA"] = "true"
os.environ["PERSISTENCE_REQUIRED"] = "false"

from app import create_app, db  # noqa: E402
from app.models import ClientCompany, PayrollRun, User  # noqa: E402
from app.payroll_status import HELD, PENDING_APPROVAL  # noqa: E402


class Phase4TestCase(unittest.TestCase):
    def setUp(self):
        self.app = create_app()
        self.app.config["TESTING"] = True
        self.app.config["WTF_CSRF_ENABLED"] = False
        self.client = self.app.test_client()
        self.ctx = self.app.app_context()
        self.ctx.push()
        self.assertEqual(
            self.client.post(
                "/login",
                data={"email": "admin@payrolla.com", "password": "password123"},
            ).status_code,
            302,
        )

    def tearDown(self):
        db.session.remove()
        self.ctx.pop()

    def _company(self, name):
        company = ClientCompany(name=name, status="Active")
        db.session.add(company)
        db.session.commit()
        return company


class ApprovalQueueInPlaceTests(Phase4TestCase):
    """Task 4.3 — the reason is in the row, and the action does not navigate."""

    REASON = "Net pay 1,000.00 vs previous 9,999.00 (90.0% change; threshold 15%)"

    def setUp(self):
        super().setUp()
        company = self._company("Held Reason Co")
        self.run = PayrollRun(
            month="March", year=2024, status=HELD,
            client_company_id=company.id, total_workers=4, total_net_pay=1000,
            risk_reasons=self.REASON,
        )
        db.session.add(self.run)
        db.session.commit()

    def test_hold_reason_is_visible_without_opening_the_run(self):
        html = self.client.get("/payroll/runs").get_data(as_text=True)
        self.assertIn("Why held", html)
        self.assertIn("90.0% change", html)

    def test_approving_over_htmx_returns_the_refreshed_body_not_a_redirect(self):
        pending = PayrollRun(
            month="April", year=2024, status=PENDING_APPROVAL,
            client_company_id=self.run.client_company_id,
            total_workers=4, total_net_pay=1000,
        )
        db.session.add(pending)
        db.session.commit()

        resp = self.client.post(
            f"/payroll/runs/{pending.id}/approve", headers={"HX-Request": "true"}
        )
        body = resp.get_data(as_text=True)

        self.assertEqual(resp.status_code, 200)
        # A fragment, not a page: the reviewer stays on the queue.
        self.assertNotIn("<!doctype html>", body.lower())
        self.assertIn('id="runs-body"', body)
        self.assertEqual(db.session.get(PayrollRun, pending.id).status, "Approved")

    def test_approving_without_htmx_still_lands_on_the_run(self):
        """The same route, unchanged, for a browser that posts a plain form."""
        pending = PayrollRun(
            month="May", year=2024, status=PENDING_APPROVAL,
            client_company_id=self.run.client_company_id,
            total_workers=4, total_net_pay=1000,
        )
        db.session.add(pending)
        db.session.commit()

        resp = self.client.post(f"/payroll/runs/{pending.id}/approve")
        self.assertEqual(resp.status_code, 302)
        self.assertIn(f"/payroll/runs/{pending.id}", resp.headers["Location"])


class ImportPreviewOrderTests(Phase4TestCase):
    """Task 4.4 — blockers precede the figures in document order."""

    def _preview_html(self):
        from io import BytesIO

        from openpyxl import Workbook

        company = ClientCompany.query.filter_by(status="Active").first()
        workbook = Workbook()
        sheet = workbook.active
        sheet.append([
            "staff id", "full name", "basic salary", "gross pay",
            "paye", "ssnit", "net pay",
        ])
        # No staff id on row 2 — a validation failure, which is the blocker the
        # panel must lead with.
        sheet.append(["ORD-1", "Ordered Worker", 1000, 1000, 50, 30, 920])
        sheet.append(["", "Nameless Row", 900, 900, 40, 20, 840])
        stream = BytesIO()
        workbook.save(stream)
        stream.seek(0)

        resp = self.client.post(
            "/payroll/runs/new",
            data={
                "client_company_id": str(company.id),
                "month": "June",
                "year": "2099",
                "payroll_file": (stream, "ordering.xlsx"),
            },
            content_type="multipart/form-data",
            follow_redirects=True,
        )
        self.assertEqual(resp.status_code, 200)
        return resp.get_data(as_text=True)

    def test_blockers_panel_comes_before_the_figures(self):
        html = self._preview_html()
        blockers = html.find("Check before you confirm")
        figures = html.find("dh-figures")
        self.assertNotEqual(blockers, -1, "no blockers panel rendered")
        self.assertNotEqual(figures, -1, "no decision header rendered")
        self.assertLess(blockers, figures)

    def test_rows_and_workers_are_labelled_as_different_things(self):
        html = self._preview_html()
        self.assertIn("Rows read from file", html)
        self.assertIn("Workers recognised", html)
        self.assertIn("resolved to", html)

    def test_raw_dump_is_behind_a_tab_not_leading_the_page(self):
        html = self._preview_html()
        self.assertIn('id="tab-raw-panel"', html)
        self.assertLess(html.find("Check before you confirm"), html.find("tab-raw-panel"))


class AwaitingCredentialsTests(Phase4TestCase):
    """Task 4.5 — an onboarded company nobody can sign in to says so."""

    def test_company_with_no_user_is_awaiting_credentials(self):
        company = self._company("No Login Co")
        self.assertTrue(company.awaiting_credentials)
        self.assertIn(company.id, ClientCompany.ids_awaiting_credentials())

    def test_binding_a_user_resolves_it_with_no_column_to_update(self):
        company = self._company("Gets Login Co")
        self.assertTrue(company.awaiting_credentials)

        user = User(
            name="Client Person", email="person@getslogin.test",
            role="client_admin", client_company_id=company.id,
        )
        user.password_hash = "x"
        db.session.add(user)
        db.session.commit()

        self.assertFalse(company.awaiting_credentials)
        self.assertNotIn(company.id, ClientCompany.ids_awaiting_credentials())

    def test_client_list_badges_it(self):
        company = self._company("Badged Co")
        html = self.client.get("/clients").get_data(as_text=True)
        self.assertIn("Awaiting credentials", html)
        self.assertIn(company.name, html)

    def test_dashboard_raises_it_as_a_signal(self):
        self._company("Signal Co")
        html = self.client.get("/dashboard").get_data(as_text=True)
        self.assertIn("awaiting credentials", html)

    def test_onboarding_summary_states_which_side_it_is_on(self):
        company = self._company("Onboarding Co")
        html = self.client.get(f"/clients/{company.id}/onboarding").get_data(as_text=True)
        self.assertIn("Awaiting credentials", html)
        self.assertIn("Nobody at Onboarding Co can sign in", html)

    def test_onboarding_offers_two_exits_not_five(self):
        company = self._company("Two Exits Co")
        html = self.client.get(f"/clients/{company.id}/onboarding").get_data(as_text=True)
        self.assertIn("Open company", html)
        self.assertIn("Add another", html)
        # The three that were competing with them are gone from this screen.
        self.assertNotIn("Edit details", html)
        self.assertNotIn("Add employees", html)
        self.assertNotIn("Onboard Another", html)


if __name__ == "__main__":
    unittest.main()
