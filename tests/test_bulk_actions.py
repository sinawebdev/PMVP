"""Phase 2 — bulk approve / reject / distribute.

Bulk actions reuse the exact per-run predicates (can_approve_run,
can_reject_run, can_distribute_run from app/permissions.py) and the same
mutation logic the single-run routes use (_approve_run/_reject_run in
app/payroll.py, distribute_run in app/distribution/service.py) — a bulk
action can never do anything to a run that clicking its own button
individually wouldn't have allowed. A run that fails the predicate (wrong
status, or not found) is skipped and reported back, never silently dropped
or forced through.
"""

import os
import unittest

os.environ["SKIP_DOTENV"] = "true"
os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["SEED_DEMO_DATA"] = "true"
os.environ["PERSISTENCE_REQUIRED"] = "false"

from app import create_app, db  # noqa: E402
from app.distribution.queue import process_all_queued  # noqa: E402
from app.models import (  # noqa: E402
    BATCH_QUEUED,
    ClientCompany,
    DistributionBatch,
    PayrollRun,
    PayslipDelivery,
)
from app.payroll_status import (  # noqa: E402
    APPROVED,
    DRAFT,
    PENDING_APPROVAL,
    PROCESSED,
    REJECTED,
)


class BulkActionsTestCase(unittest.TestCase):
    def setUp(self):
        self.app = create_app()
        self.app.config["TESTING"] = True
        self.http = self.app.test_client()
        self.ctx = self.app.app_context()
        self.ctx.push()
        self.client_id = ClientCompany.query.first().id

    def tearDown(self):
        self.ctx.pop()

    def _login(self, email):
        resp = self.http.post(
            "/login", data={"email": email, "password": "password123"}
        )
        self.assertIn(resp.status_code, (200, 302))

    def _run(self, status, month="August", year=2099):
        run = PayrollRun(
            client_company_id=self.client_id, month=month, year=year, status=status
        )
        db.session.add(run)
        db.session.commit()
        return run

    # --- bulk approve --------------------------------------------------

    def test_bulk_approve_mixed_statuses_skips_ineligible(self):
        self._login("admin@chrisnat.local")
        eligible = self._run(DRAFT)
        also_eligible = self._run(PENDING_APPROVAL)
        ineligible = self._run(APPROVED)
        resp = self.http.post(
            "/payroll/runs/bulk/approve",
            data={
                "run_ids": [str(eligible.id), str(also_eligible.id), str(ineligible.id)]
            },
            follow_redirects=True,
        )
        self.assertEqual(resp.status_code, 200)
        body = resp.get_data(as_text=True)
        self.assertIn("2 run(s) approved", body)
        self.assertIn("1 run(s) could not be approved", body)
        db.session.refresh(eligible)
        db.session.refresh(also_eligible)
        self.assertEqual(eligible.status, APPROVED)
        self.assertEqual(also_eligible.status, APPROVED)

    def test_bulk_approve_stamps_approver_and_records_audit(self):
        self._login("admin@chrisnat.local")
        run = self._run(DRAFT)
        self.http.post("/payroll/runs/bulk/approve", data={"run_ids": [str(run.id)]})
        db.session.refresh(run)
        self.assertEqual(run.status, APPROVED)
        self.assertIsNotNone(run.approved_by)
        self.assertIsNotNone(run.approved_at)
        html = self.http.get(f"/payroll/runs/{run.id}").get_data(as_text=True)
        self.assertIn("Payroll approval", html)  # shows in the run's activity feed

    def test_bulk_approve_requires_approval_role(self):
        self._login("payroll@chrisnat.local")  # payroll_officer is not in APPROVAL_ROLES
        run = self._run(DRAFT)
        resp = self.http.post(
            "/payroll/runs/bulk/approve",
            data={"run_ids": [str(run.id)]},
            follow_redirects=True,
        )
        self.assertIn("do not have permission", resp.get_data(as_text=True))
        db.session.refresh(run)
        self.assertEqual(run.status, DRAFT)

    def test_bulk_approve_empty_selection_flashes_and_changes_nothing(self):
        self._login("admin@chrisnat.local")
        resp = self.http.post(
            "/payroll/runs/bulk/approve", data={}, follow_redirects=True
        )
        self.assertIn("No runs selected for bulk approve", resp.get_data(as_text=True))

    def test_bulk_approve_ignores_duplicate_and_unknown_ids(self):
        self._login("admin@chrisnat.local")
        run = self._run(DRAFT)
        resp = self.http.post(
            "/payroll/runs/bulk/approve",
            data={"run_ids": [str(run.id), str(run.id), "999999"]},
            follow_redirects=True,
        )
        body = resp.get_data(as_text=True)
        self.assertIn("1 run(s) approved", body)
        self.assertIn("1 run(s) could not be approved", body)
        self.assertIn("not found", body)
        db.session.refresh(run)
        self.assertEqual(run.status, APPROVED)

    def test_bulk_approve_preserves_list_filter_on_redirect(self):
        self._login("admin@chrisnat.local")
        run = self._run(DRAFT)
        resp = self.http.post(
            "/payroll/runs/bulk/approve",
            data={"run_ids": [str(run.id)], "status": "needs_approval"},
        )
        self.assertEqual(resp.status_code, 302)
        self.assertIn("status=needs_approval", resp.headers["Location"])

    # --- bulk reject -----------------------------------------------------

    def test_bulk_reject_applies_shared_notes_to_every_run(self):
        self._login("md@chrisnat.local")
        a = self._run(DRAFT)
        b = self._run(PENDING_APPROVAL)
        resp = self.http.post(
            "/payroll/runs/bulk/reject",
            data={"run_ids": [str(a.id), str(b.id)], "notes": "Duplicate upload"},
            follow_redirects=True,
        )
        self.assertIn("2 run(s) rejected", resp.get_data(as_text=True))
        db.session.refresh(a)
        db.session.refresh(b)
        self.assertEqual(a.status, REJECTED)
        self.assertEqual(b.status, REJECTED)
        self.assertEqual(a.notes, "Duplicate upload")
        self.assertEqual(b.notes, "Duplicate upload")

    def test_bulk_reject_skips_already_closed_run(self):
        self._login("md@chrisnat.local")
        closed = self._run(PROCESSED)
        resp = self.http.post(
            "/payroll/runs/bulk/reject",
            data={"run_ids": [str(closed.id)]},
            follow_redirects=True,
        )
        self.assertIn("1 run(s) could not be rejected", resp.get_data(as_text=True))
        db.session.refresh(closed)
        self.assertEqual(closed.status, PROCESSED)

    def test_bulk_reject_requires_approval_role(self):
        self._login("accounts@chrisnat.local")  # accounts_officer not in APPROVAL_ROLES
        run = self._run(DRAFT)
        resp = self.http.post(
            "/payroll/runs/bulk/reject",
            data={"run_ids": [str(run.id)]},
            follow_redirects=True,
        )
        self.assertIn("do not have permission", resp.get_data(as_text=True))
        db.session.refresh(run)
        self.assertEqual(run.status, DRAFT)

    # --- bulk distribute ---------------------------------------------------

    def test_bulk_distribute_sends_eligible_and_skips_draft(self):
        self._login("admin@chrisnat.local")
        seeded_approved = PayrollRun.query.filter_by(status=APPROVED).first()
        self.assertIsNotNone(seeded_approved, "expected a seeded Approved payroll run")
        draft = self._run(DRAFT)
        resp = self.http.post(
            "/payroll/runs/bulk/distribute",
            data={"run_ids": [str(seeded_approved.id), str(draft.id)]},
            follow_redirects=True,
        )
        body = resp.get_data(as_text=True)
        self.assertIn("1 run(s) queued", body)
        self.assertIn("1 run(s) could not be distributed", body)
        # Bulk distribute only queues the batch — no request-thread sending.
        self.assertEqual(
            PayslipDelivery.query.filter_by(payroll_run_id=seeded_approved.id).count(), 0
        )
        batch = DistributionBatch.query.filter_by(payroll_run_id=seeded_approved.id).first()
        self.assertIsNotNone(batch)
        self.assertEqual(batch.status, BATCH_QUEUED)
        # A worker claiming and running the queue delivers it, same as before.
        process_all_queued()
        rows = PayslipDelivery.query.filter_by(payroll_run_id=seeded_approved.id).all()
        self.assertTrue(rows)

    def test_bulk_distribute_requires_payroll_role(self):
        self._login("operations@chrisnat.local")  # operations_supervisor: no lifecycle group
        seeded_approved = PayrollRun.query.filter_by(status=APPROVED).first()
        resp = self.http.post(
            "/payroll/runs/bulk/distribute",
            data={"run_ids": [str(seeded_approved.id)]},
            follow_redirects=True,
        )
        self.assertIn("do not have permission", resp.get_data(as_text=True))
        self.assertEqual(
            DistributionBatch.query.filter_by(payroll_run_id=seeded_approved.id).count(), 0
        )

    def test_bulk_distribute_on_processed_run_is_eligible(self):
        # SENDABLE_STATUSES = Approved or Processed — a closed run stays
        # distributable, matching can_distribute_run/the single-run route.
        self._login("admin@chrisnat.local")
        run = self._run(PROCESSED)
        resp = self.http.post(
            "/payroll/runs/bulk/distribute",
            data={"run_ids": [str(run.id)]},
            follow_redirects=True,
        )
        self.assertIn("1 run(s) queued", resp.get_data(as_text=True))

    # --- UI wiring ---------------------------------------------------------

    def test_runs_list_renders_bulk_controls_for_admin(self):
        self._login("admin@chrisnat.local")
        html = self.http.get("/payroll/runs").get_data(as_text=True)
        self.assertIn("Bulk Approve", html)
        self.assertIn("Bulk Reject", html)
        self.assertIn("Bulk Distribute", html)
        self.assertIn("run-select-checkbox", html)

    def test_runs_list_hides_bulk_controls_for_unprivileged_role(self):
        self._login("operations@chrisnat.local")
        resp = self.http.get("/payroll/runs")
        # operations_supervisor is redirected off the operator-only runs list
        # entirely (role_required), so the bulk controls never render for it.
        self.assertIn(resp.status_code, (200, 302))
        if resp.status_code == 200:
            self.assertNotIn("Bulk Approve", resp.get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()
