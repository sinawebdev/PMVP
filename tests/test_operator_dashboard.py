"""Phase 2 — operator dashboard: held payrolls, recently completed, quick actions.

Reuses existing state (risk_status/status + PayslipDelivery) — no new business
logic. Verifies the dashboard surfaces held runs and completed runs and renders
the shared lifecycle stepper.
"""

import os
import unittest

os.environ["SKIP_DOTENV"] = "true"
os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["SEED_DEMO_DATA"] = "true"
os.environ["PERSISTENCE_REQUIRED"] = "false"

from app import create_app, db  # noqa: E402
from app.models import ClientCompany, PayrollRun  # noqa: E402
from app.payroll_status import HELD, PROCESSED  # noqa: E402


class OperatorDashboardTestCase(unittest.TestCase):
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

    def test_dashboard_surfaces_held_and_completed(self):
        company = ClientCompany.query.first()
        db.session.add_all([
            PayrollRun(
                month="March", year=2024, status=HELD, client_company_id=company.id,
                total_workers=3, total_net_pay=1000, risk_status="held",
                risk_reasons="net pay variance",
            ),
            PayrollRun(
                month="February", year=2024, status=PROCESSED, client_company_id=company.id,
                total_workers=3, total_net_pay=900,
            ),
        ])
        db.session.commit()

        html = self.client.get("/dashboard").get_data(as_text=True)
        self.assertIn("Held Payrolls", html)          # stat card
        self.assertIn("Held for Risk Review", html)    # held panel
        self.assertIn("net pay variance", html)        # held reason surfaced
        self.assertIn("Recently Completed", html)      # completed panel
        self.assertIn("Risk Queue", html)              # quick action
        self.assertIn("lifecycle-stepper", html)       # shared stepper


if __name__ == "__main__":
    unittest.main()
