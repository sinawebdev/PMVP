"""Phase 2 — run detail: progress stepper + approval timeline / activity feed.

run_activity() merges the existing AuditTrail + DomainEvent records for a run
into one time-sorted stream — no new model, no new business logic.
"""

import os
import unittest

os.environ["SKIP_DOTENV"] = "true"
os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["SEED_DEMO_DATA"] = "true"
os.environ["PERSISTENCE_REQUIRED"] = "false"

from app import create_app, db  # noqa: E402
from app.audit import record_audit  # noqa: E402
from app.events import record_event, run_activity  # noqa: E402
from app.models import ClientCompany, PayrollRun  # noqa: E402


class RunActivityTestCase(unittest.TestCase):
    def setUp(self):
        self.app = create_app()
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()
        self.ctx = self.app.app_context()
        self.ctx.push()
        self.assertEqual(
            self.client.post(
                "/login", data={"email": "admin@chrisnat.local", "password": "password123"}
            ).status_code,
            302,
        )

    def tearDown(self):
        self.ctx.pop()

    def _run(self, month="March", status="Approved"):
        company = ClientCompany.query.first()
        run = PayrollRun(
            month=month, year=2024, status=status, client_company_id=company.id,
            total_workers=2, total_net_pay=500,
        )
        db.session.add(run)
        db.session.flush()
        return run, company

    def test_run_activity_merges_audit_and_events(self):
        run, company = self._run()
        record_audit("Payroll approval", run, "Payroll approved (single-stage).")
        record_event("run.risk_accepted", summary="auto-accepted", subject=run,
                     client_company_id=company.id)
        db.session.commit()
        titles = [a["title"] for a in run_activity(run)]
        self.assertIn("Payroll approval", titles)          # from AuditTrail
        self.assertIn("Payroll run auto-accepted", titles)  # from DomainEvent (labelled)

    def test_detail_page_shows_progress_and_timeline(self):
        run, _ = self._run(month="April")
        record_audit("Payroll approval", run, "Payroll approved.")
        db.session.commit()
        html = self.client.get(f"/payroll/runs/{run.id}").get_data(as_text=True)
        self.assertIn("Payroll Progress", html)
        self.assertIn("lifecycle-stepper", html)
        self.assertIn("Activity &amp; Approval Timeline", html)
        self.assertIn("Payroll approval", html)


if __name__ == "__main__":
    unittest.main()
