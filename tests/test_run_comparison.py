"""Phase 2 — payroll comparison vs the client's previous closed run.

compare_to_previous() reuses the risk gate's baseline (_previous_closed_run) and
thresholds, so 'unusual change' on the comparison panel is consistent with the
gate's 'held'. Read-only; changes no lifecycle decision.
"""

import os
import unittest

os.environ["SKIP_DOTENV"] = "true"
os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["SEED_DEMO_DATA"] = "true"
os.environ["PERSISTENCE_REQUIRED"] = "false"

from app import create_app, db  # noqa: E402
from app.models import ClientCompany, PayrollRun  # noqa: E402
from app.risk import compare_to_previous  # noqa: E402


class RunComparisonTestCase(unittest.TestCase):
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
        self.company = ClientCompany.query.first()

    def tearDown(self):
        self.ctx.pop()

    def _run(self, *, status, net, workers, gross=0, deductions=0, paye=0, ssnit=0, month="January"):
        run = PayrollRun(
            month=month, year=2024, status=status, client_company_id=self.company.id,
            total_workers=workers, total_net_pay=net, total_gross_pay=gross,
            total_deductions=deductions, total_paye=paye, total_ssnit=ssnit,
        )
        db.session.add(run)
        db.session.flush()
        return run

    def _row(self, rows, key):
        return next(r for r in rows if r["key"] == key)

    def test_no_baseline_returns_empty(self):
        # A brand-new company with no prior closed run — the seed's first company
        # already has one, so use a fresh tenant to exercise the no-baseline path.
        fresh = ClientCompany(name="Fresh Co No Runs", status="Active")
        db.session.add(fresh)
        db.session.flush()
        run = PayrollRun(
            month="January", year=2024, status="Draft", client_company_id=fresh.id,
            total_workers=10, total_net_pay=1000,
        )
        db.session.add(run)
        db.session.flush()
        db.session.commit()
        result = compare_to_previous(run)
        self.assertIsNone(result["previous"])
        self.assertEqual(result["rows"], [])

    def test_flags_change_beyond_threshold(self):
        self._run(status="Approved", net=1000, workers=10, month="December")
        current = self._run(status="Draft", net=1200, workers=10, month="January")
        db.session.commit()
        result = compare_to_previous(current)
        self.assertIsNotNone(result["previous"])
        net_row = self._row(result["rows"], "net")
        self.assertTrue(net_row["flag"])  # +20% net > 15% threshold
        self.assertAlmostEqual(net_row["pct"], 0.2, places=3)
        workers_row = self._row(result["rows"], "workers")
        self.assertFalse(workers_row["flag"])  # unchanged headcount

    def test_detail_page_renders_comparison(self):
        self._run(status="Approved", net=1000, workers=10, month="December")
        current = self._run(status="Draft", net=1200, workers=10, month="January")
        db.session.commit()
        html = self.client.get(f"/payroll/runs/{current.id}").get_data(as_text=True)
        self.assertIn("Compared to Previous Run", html)


if __name__ == "__main__":
    unittest.main()
