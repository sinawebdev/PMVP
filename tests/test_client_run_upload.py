"""Phase 1 — client self-service payroll: upload -> preview -> confirm.

A client uploads a standard payroll workbook for their OWN company. The upload
creates a resumable, tenant-scoped ImportBatch DRAFT and lands on a preview;
only an explicit Confirm creates the run (reusing the operator import pipeline
with client_company_id forced to the tenant) and routes it through the Phase 5
risk gate. Re-uploading the same period replaces the existing run in any status
EXCEPT Processed/paid. Raw-hours and non-Excel files are refused, a platform
user cannot reach the flow, and one tenant can never touch another's draft.
"""

import os
import unittest
from io import BytesIO

os.environ["SKIP_DOTENV"] = "true"
os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["SEED_DEMO_DATA"] = "true"
os.environ["PERSISTENCE_REQUIRED"] = "false"

from openpyxl import Workbook  # noqa: E402

from app import create_app, db  # noqa: E402
from app.models import DomainEvent, ImportBatch, PayrollRun, User  # noqa: E402
from app.payroll_status import AUTO_ACCEPTED, HELD, PROCESSED  # noqa: E402

# A period deliberately distinct from the seeded MSC run (which uses "now"),
# so the duplicate guard doesn't reject the first upload.
UPLOAD_MONTH = "March"
UPLOAD_YEAR = 2024


def _payroll_workbook():
    workbook = Workbook()
    sheet = workbook.active
    sheet.append([
        "staff id", "full name", "ssnit no", "basic salary", "transport allowance",
        "housing allowance", "overtime pay", "gross pay", "paye", "ssnit",
        "other deductions", "net pay",
    ])
    sheet.append(["UP-1", "Upload Worker One", "SSN-UP-1", 2100, 100, 100, 0, 2300, 100, 80, 0, 2120])
    sheet.append(["UP-2", "Upload Worker Two", "SSN-UP-2", 1800, 80, 80, 0, 1960, 90, 70, 0, 1800])
    stream = BytesIO()
    workbook.save(stream)
    stream.seek(0)
    return stream


class ClientRunUploadTestCase(unittest.TestCase):
    def setUp(self):
        self.app = create_app()
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()
        self.ctx = self.app.app_context()
        self.ctx.push()
        self.msc = User.query.filter_by(email="admin@msc.demo").first()
        self.tenant_id = self.msc.client_company_id

    def tearDown(self):
        self.ctx.pop()

    # --- helpers ------------------------------------------------------------
    def _login(self, email, client=None):
        client = client or self.client
        self.assertEqual(
            client.post("/login", data={"email": email, "password": "password123"}).status_code,
            302,
        )

    def _upload(self, **overrides):
        data = {
            "month": overrides.get("month", UPLOAD_MONTH),
            "year": str(overrides.get("year", UPLOAD_YEAR)),
            "payroll_file": (
                overrides.get("stream", _payroll_workbook()),
                overrides.get("filename", "payroll.xlsx"),
            ),
        }
        return self.client.post(
            "/company/runs/upload", data=data, content_type="multipart/form-data"
        )

    def _latest_draft(self):
        return (
            ImportBatch.query.filter_by(client_company_id=self.tenant_id)
            .order_by(ImportBatch.id.desc())
            .first()
        )

    def _confirm_latest(self):
        batch = self._latest_draft()
        resp = self.client.post(f"/company/imports/{batch.id}/confirm")
        return batch, resp

    def _new_run(self):
        return PayrollRun.query.filter_by(
            client_company_id=self.tenant_id, month=UPLOAD_MONTH, year=UPLOAD_YEAR
        ).first()

    def _all_period_runs(self):
        return PayrollRun.query.filter_by(
            client_company_id=self.tenant_id, month=UPLOAD_MONTH, year=UPLOAD_YEAR
        ).all()

    # --- upload creates a draft, not a run ----------------------------------
    def test_upload_creates_draft_not_run(self):
        self._login("admin@msc.demo")
        resp = self._upload()
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/imports/", resp.headers["Location"])
        # A draft exists; no run yet — confirmation is a separate, explicit step.
        draft = self._latest_draft()
        self.assertIsNotNone(draft)
        self.assertEqual(draft.status, "Draft")
        self.assertIsNone(draft.payroll_run_id)
        self.assertIsNone(self._new_run())

    def test_preview_page_renders(self):
        self._login("admin@msc.demo")
        self._upload()
        draft = self._latest_draft()
        resp = self.client.get(f"/company/imports/{draft.id}/preview")
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"Import preview", resp.data)

    def test_confirm_creates_tenant_run_through_risk_gate(self):
        self._login("admin@msc.demo")
        self._upload()
        batch, resp = self._confirm_latest()
        self.assertEqual(resp.status_code, 302)
        run = self._new_run()
        self.assertIsNotNone(run)
        self.assertEqual(run.client_company_id, self.tenant_id)  # forced to tenant
        self.assertGreater(len(run.items), 0)
        # It went through the risk gate — MSC already has a seeded run, so this
        # (its 2nd) is still inside the new-client window and is Held.
        self.assertEqual(run.status, HELD)
        self.assertEqual(run.risk_status, "held")
        # The draft is now linked to the created run.
        db.session.refresh(batch)
        self.assertEqual(batch.status, "Imported")
        self.assertEqual(batch.payroll_run_id, run.id)
        # platform oversight was notified (tenant -> platform).
        event = DomainEvent.query.filter_by(
            event_type="run.risk_held", subject_id=run.id
        ).first()
        self.assertIsNotNone(event)
        self.assertEqual(event.client_company_id, self.tenant_id)

    def test_non_excel_is_rejected(self):
        self._login("admin@msc.demo")
        resp = self._upload(stream=BytesIO(b"not a workbook"), filename="notes.txt")
        self.assertEqual(resp.status_code, 200)  # re-renders the form inline
        self.assertIsNone(self._latest_draft())
        self.assertIsNone(self._new_run())

    def test_platform_user_cannot_upload(self):
        self._login("chrisnat.admin@chrisnat.local")
        # tenant_role_required bounces a platform user to the oversight dashboard.
        self.assertEqual(self.client.get("/company/runs/upload").status_code, 302)
        self.assertEqual(self._upload().status_code, 302)
        self.assertIsNone(self._new_run())

    def test_discard_removes_draft(self):
        self._login("admin@msc.demo")
        self._upload()
        draft = self._latest_draft()
        resp = self.client.post(f"/company/imports/{draft.id}/discard")
        self.assertEqual(resp.status_code, 302)
        self.assertIsNone(db.session.get(ImportBatch, draft.id))
        self.assertIsNone(self._new_run())

    # --- tenant isolation ---------------------------------------------------
    def test_cross_tenant_draft_is_404(self):
        self._login("admin@msc.demo")
        self._upload()
        msc_draft_id = self._latest_draft().id
        # A different tenant (Stellar) must never reach MSC's draft. Single client
        # + logout/login is the project's tenant-switch idiom (two test clients
        # share a session here).
        self.client.get("/logout")
        self._login("admin@stellar.demo")
        self.assertEqual(self.client.get(f"/company/imports/{msc_draft_id}/preview").status_code, 404)
        self.assertEqual(self.client.get(f"/company/imports/{msc_draft_id}/errors").status_code, 404)
        self.assertEqual(self.client.post(f"/company/imports/{msc_draft_id}/confirm").status_code, 404)
        self.assertEqual(self.client.post(f"/company/imports/{msc_draft_id}/discard").status_code, 404)
        self.client.get("/logout")
        # MSC's draft is untouched and no run leaked into either tenant.
        self.assertIsNotNone(db.session.get(ImportBatch, msc_draft_id))
        self.assertIsNone(self._new_run())

    # --- replace policy (Sina 2026-07-22: any status except Processed) -------
    def test_reupload_replaces_existing_non_processed_run(self):
        self._login("admin@msc.demo")
        self._upload()
        self._confirm_latest()
        first = self._new_run()
        self.assertIsNotNone(first)
        # Re-upload the same period and confirm — replaces the (Held) run.
        self._upload()
        self._confirm_latest()
        runs = self._all_period_runs()
        self.assertEqual(len(runs), 1)  # replaced, not duplicated
        self.assertNotEqual(runs[0].id, first.id)  # a fresh run
        self.assertIsNone(db.session.get(PayrollRun, first.id))  # old run purged
        self.assertGreater(len(runs[0].items), 0)

    def test_reupload_blocked_when_existing_run_processed(self):
        self._login("admin@msc.demo")
        self._upload()
        self._confirm_latest()
        run = self._new_run()
        run.status = PROCESSED  # closed/paid — no longer client-replaceable
        db.session.commit()
        processed_id = run.id
        # A second upload+confirm for the same period must be refused.
        self._upload()
        batch, resp = self._confirm_latest()
        self.assertEqual(resp.status_code, 302)
        self.assertIn(f"/imports/{batch.id}/preview", resp.headers["Location"])
        runs = self._all_period_runs()
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0].id, processed_id)  # untouched
        self.assertIsNone(batch.payroll_run_id)  # draft not consumed


    # --- history / resumable drafts -----------------------------------------
    def test_draft_appears_in_runs_list_until_confirmed(self):
        self._login("admin@msc.demo")
        self._upload()
        draft = self._latest_draft()
        html = self.client.get("/company/runs").get_data(as_text=True)
        self.assertIn("In-progress imports", html)
        self.assertIn("payroll.xlsx", html)
        # Once confirmed it's a run, not an in-progress draft.
        self.client.post(f"/company/imports/{draft.id}/confirm")
        html2 = self.client.get("/company/runs").get_data(as_text=True)
        self.assertNotIn("In-progress imports", html2)

    def test_dashboard_nudges_in_progress_import(self):
        self._login("admin@msc.demo")
        self._upload()
        html = self.client.get("/company").get_data(as_text=True)
        self.assertIn("in progress", html)

    def test_upload_form_has_progress_and_month_select(self):
        self._login("admin@msc.demo")
        html = self.client.get("/company/runs/upload").get_data(as_text=True)
        self.assertIn('id="upload-progress"', html)
        self.assertIn('<select id="month"', html)
        self.assertIn('name="payroll_file"', html)

    def test_upload_page_offers_standard_and_raw_choices(self):
        """Regression: the client upload page must expose BOTH workflows, render
        inside the shared left-sidebar shell, and carry the CSRF token wiring
        (meta + app.js) the whole app relies on. The missing wiring is what broke
        client uploads with 'The CSRF token is missing.'"""
        self._login("admin@msc.demo")
        html = self.client.get("/company/runs/upload").get_data(as_text=True)
        # Both upload workflows are offered (the operator page exposes both too).
        self.assertIn("Standard Payroll Upload", html)
        self.assertIn("Raw Hours Upload", html)
        self.assertIn("/company/runs/raw/upload", html)
        # Shared design language: the left-sidebar shell, not the old top header.
        self.assertIn("portal-sidebar", html)
        self.assertIn("portal-shell", html)
        # CSRF wiring on the client shell (the regression guard): a rendered token
        # + app.js, which attaches it to every mutating request.
        self.assertIn('name="csrf-token"', html)
        self.assertIn("app.js", html)

    def test_client_raw_routes_are_wired_and_tenant_guarded(self):
        """The raw-hours client routes exist, are tenant-scoped, and reject a
        platform user (tenant_role_required) rather than 404/redirecting away."""
        # Platform user is bounced to the oversight console.
        self._login("chrisnat.admin@chrisnat.local")
        self.assertEqual(self.client.post("/company/runs/raw/upload").status_code, 302)
        self.client.get("/logout")
        # A tenant user reaches the route's own validation (JSON), proving it is
        # wired and tenant-scoped (company is forced, never read from the form).
        self._login("admin@msc.demo")
        resp = self.client.post(
            "/company/runs/raw/upload", data={"month": "March", "year": "2024"}
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("No file provided", resp.get_json()["error"])
        # The monthly template is gated until the company is set up for raw-hours.
        tmpl = self.client.get("/company/runs/raw/template")
        self.assertEqual(tmpl.status_code, 404)


if __name__ == "__main__":
    unittest.main()
