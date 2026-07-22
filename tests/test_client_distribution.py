"""Phase 4 — client payslip distribution.

A client_admin distributes their own run's payslips: view the distribution
page, download the whole run as a ZIP, and send per-worker links (console
channels in v1). A client_preparer may view/download but not send. Every
surface is tenant-scoped — another tenant's run is 404, never data.
"""

import os
import unittest

os.environ["SKIP_DOTENV"] = "true"
os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["SEED_DEMO_DATA"] = "true"
os.environ["PERSISTENCE_REQUIRED"] = "false"

from app import create_app, db  # noqa: E402
from app.distribution.queue import process_all_queued  # noqa: E402
from app.models import DistributionBatch, PayrollRun, PayslipDelivery, User  # noqa: E402


class ClientDistributionTestCase(unittest.TestCase):
    def setUp(self):
        self.app = create_app()
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()
        self.ctx = self.app.app_context()
        self.ctx.push()
        self.msc = User.query.filter_by(email="admin@msc.demo").first()
        self.stellar = User.query.filter_by(email="admin@stellar.demo").first()
        self.msc_run = PayrollRun.query.filter_by(
            client_company_id=self.msc.client_company_id
        ).first()
        # A preparer in the same tenant — may view/download, must not send.
        self.preparer = User(
            name="MSC Preparer",
            email="preparer@msc.demo",
            role="client_preparer",
            client_company_id=self.msc.client_company_id,
        )
        self.preparer.set_password("password123")
        db.session.add(self.preparer)
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

    def _deliveries_for_run(self, run):
        item_ids = [it.id for it in run.items]
        return PayslipDelivery.query.filter(
            PayslipDelivery.payroll_item_id.in_(item_ids)
        ).all()

    def test_admin_views_page_and_downloads_zip(self):
        self._login("admin@msc.demo")
        self.assertEqual(
            self.client.get(f"/company/runs/{self.msc_run.id}/distribute").status_code, 200
        )
        resp = self.client.get(f"/company/runs/{self.msc_run.id}/payslips.zip")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.mimetype, "application/zip")
        self.assertTrue(resp.get_data().startswith(b"PK"))  # a real zip

    def test_admin_send_queues_a_batch_and_is_idempotent(self):
        self._login("admin@msc.demo")
        nonce = "fixed-nonce-1"
        first = self.client.post(
            f"/company/runs/{self.msc_run.id}/distribute/send",
            data={"channel": "auto", "nonce": nonce},
        )
        self.assertEqual(first.status_code, 302)
        # Sending queues a batch — no deliveries yet, request wasn't blocked on a send.
        self.assertEqual(len(self._deliveries_for_run(self.msc_run)), 0)
        batches = DistributionBatch.query.filter_by(payroll_run_id=self.msc_run.id).all()
        self.assertEqual(len(batches), 1)
        self.assertEqual(batches[0].status, "queued")
        # Same nonce replays — no duplicate batch.
        self.client.post(
            f"/company/runs/{self.msc_run.id}/distribute/send",
            data={"channel": "auto", "nonce": nonce},
        )
        self.assertEqual(
            DistributionBatch.query.filter_by(payroll_run_id=self.msc_run.id).count(), 1
        )
        # A worker claiming and running the queue delivers it, same end state as before.
        process_all_queued()
        n_items = len(self.msc_run.items)
        self.assertGreater(n_items, 0)
        self.assertEqual(len(self._deliveries_for_run(self.msc_run)), n_items)

    def test_preparer_can_view_but_not_send(self):
        self._login("preparer@msc.demo")
        # Viewing/downloading is allowed.
        self.assertEqual(
            self.client.get(f"/company/runs/{self.msc_run.id}/distribute").status_code, 200
        )
        # Sending is bounced to the company dashboard; nothing is queued.
        resp = self.client.post(
            f"/company/runs/{self.msc_run.id}/distribute/send",
            data={"channel": "auto", "nonce": "x"},
        )
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(resp.headers["Location"].endswith("/company"))
        self.assertEqual(
            DistributionBatch.query.filter_by(payroll_run_id=self.msc_run.id).count(), 0
        )

    def test_cross_tenant_distribution_is_404(self):
        self._login("admin@stellar.demo")  # a different tenant (also client_admin)
        self.assertEqual(
            self.client.get(f"/company/runs/{self.msc_run.id}/distribute").status_code, 404
        )
        self.assertEqual(
            self.client.get(f"/company/runs/{self.msc_run.id}/payslips.zip").status_code, 404
        )
        # Passes the client_admin role gate, then 404s on the cross-tenant run.
        self.assertEqual(
            self.client.post(
                f"/company/runs/{self.msc_run.id}/distribute/send",
                data={"channel": "auto", "nonce": "y"},
            ).status_code,
            404,
        )
        self.assertEqual(
            DistributionBatch.query.filter_by(payroll_run_id=self.msc_run.id).count(), 0
        )


if __name__ == "__main__":
    unittest.main()
