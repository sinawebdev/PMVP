"""Client (tenant) self-service reports & exports.

A tenant user previews and downloads the SAME payroll outputs operators do — the
payroll workbook, the bank transfer listing, and the GRA PAYE schedule — for
their OWN completed runs, without operator intervention. Covers: the preview hub
renders; each export downloads as an .xlsx attachment; a client export never
advances the run to Processed (unlike the operator export); reports are gated on a
completed run (Approved/Processed); a platform user is bounced; one tenant can
never reach another tenant's run (404); and the shared bank_listing_groups helper
partitions the run's items faithfully.
"""

import os
import unittest

os.environ["SKIP_DOTENV"] = "true"
os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["SEED_DEMO_DATA"] = "true"
os.environ["PERSISTENCE_REQUIRED"] = "false"

from app import create_app, db  # noqa: E402
from app.excel_utils import bank_listing_groups  # noqa: E402
from app.models import PayrollRun, User  # noqa: E402
from app.payroll_status import APPROVED, HELD, PROCESSED  # noqa: E402

EXPORT_PATHS = ["/export", "/export/bank-listing", "/export/gra-paye"]


class ClientReportsTestCase(unittest.TestCase):
    def setUp(self):
        self.app = create_app()
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()
        self.ctx = self.app.app_context()
        self.ctx.push()
        self.msc = User.query.filter_by(email="admin@msc.demo").first()
        self.tenant_id = self.msc.client_company_id
        self.run = PayrollRun.query.filter_by(client_company_id=self.tenant_id).first()
        self.run.status = APPROVED  # a completed run — reports unlocked
        db.session.commit()

    def tearDown(self):
        self.ctx.pop()

    def _login(self, email):
        self.assertEqual(
            self.client.post(
                "/login", data={"email": email, "password": "password123"}
            ).status_code,
            302,
        )

    # --- preview hub --------------------------------------------------------
    def test_reports_hub_renders_with_all_three_exports(self):
        self._login("admin@msc.demo")
        resp = self.client.get(f"/company/runs/{self.run.id}/reports")
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertIn("Bank listing", html)
        self.assertIn("GRA PAYE schedule", html)
        self.assertIn("Payroll export", html)
        # Download links for each export are present on a completed run.
        self.assertIn(f"/company/runs/{self.run.id}/export/bank-listing", html)
        self.assertIn(f"/company/runs/{self.run.id}/export/gra-paye", html)
        # Left-sidebar shell (consistent client chrome).
        self.assertIn("portal-sidebar", html)

    # --- downloads reuse the shared engine ----------------------------------
    def test_each_export_downloads_as_xlsx_attachment(self):
        self._login("admin@msc.demo")
        for path in EXPORT_PATHS:
            resp = self.client.get(f"/company/runs/{self.run.id}{path}")
            self.assertEqual(resp.status_code, 200, path)
            disposition = resp.headers.get("Content-Disposition", "")
            self.assertIn("attachment", disposition, path)
            self.assertIn(".xlsx", disposition, path)

    def test_client_export_does_not_advance_run_to_processed(self):
        # Unlike the operator payroll export, a client export is read-only and
        # must NOT close the run — that stays an operator/accounts action.
        self._login("admin@msc.demo")
        self.client.get(f"/company/runs/{self.run.id}/export")
        db.session.refresh(self.run)
        self.assertEqual(self.run.status, APPROVED)
        self.assertNotEqual(self.run.status, PROCESSED)

    # --- lifecycle gate -----------------------------------------------------
    def test_downloads_blocked_until_run_is_completed(self):
        self.run.status = HELD  # not yet approved
        db.session.commit()
        self._login("admin@msc.demo")
        for path in EXPORT_PATHS:
            resp = self.client.get(f"/company/runs/{self.run.id}{path}")
            self.assertEqual(resp.status_code, 302, path)  # redirected, no file
            self.assertIn("/reports", resp.headers["Location"])
        # The hub still renders (as a preview) and explains the lock.
        hub = self.client.get(f"/company/runs/{self.run.id}/reports").get_data(as_text=True)
        self.assertIn("unlock", hub.lower())

    # --- tenant isolation ---------------------------------------------------
    def test_cross_tenant_run_is_404_on_every_report_route(self):
        # A different tenant must never reach MSC's run through the report routes.
        msc_run_id = self.run.id
        self._login("admin@stellar.demo")
        for path in ["/reports"] + EXPORT_PATHS:
            self.assertEqual(
                self.client.get(f"/company/runs/{msc_run_id}{path}").status_code, 404, path
            )

    # --- authorization ------------------------------------------------------
    def test_platform_user_bounced_from_client_reports(self):
        self._login("chrisnat.admin@chrisnat.local")
        resp = self.client.get(f"/company/runs/{self.run.id}/reports")
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(resp.headers["Location"].endswith("/dashboard"))

    # --- shared helper parity (no logic duplication) ------------------------
    def test_bank_listing_groups_partitions_items_faithfully(self):
        groups, grand_total = bank_listing_groups(self.run)
        # Every item appears exactly once across the bank groups.
        grouped_ids = [it.id for g in groups for it in g["items"]]
        self.assertCountEqual(grouped_ids, [i.id for i in self.run.items])
        # Grand total equals the sum of item net pay; subtotals reconcile to it.
        expected = round(sum(i.net_pay or 0 for i in self.run.items), 2)
        self.assertEqual(grand_total, expected)
        self.assertEqual(round(sum(g["subtotal"] for g in groups), 2), expected)
        # Groups are ordered by bank name (stable, matches the exported sheet).
        self.assertEqual([g["bank"] for g in groups], sorted(g["bank"] for g in groups))


if __name__ == "__main__":
    unittest.main()
