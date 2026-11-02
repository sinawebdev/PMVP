"""Each payroll run records the statutory rate version it was calculated with.

An open run takes the version in force for its period; a finalised run keeps
the version it was approved with, even after a backdated version is added.
"""

import os
import unittest
from datetime import date

os.environ["SKIP_DOTENV"] = "true"
os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["PERSISTENCE_REQUIRED"] = "false"

from app import create_app, db  # noqa: E402
from app.models import ClientCompany, PayrollRun, StatutoryRate  # noqa: E402
from app.payroll_calculations import statutory_rate_for_run  # noqa: E402


class RateTraceabilityTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app()
        self.app.config["TESTING"] = True
        self.ctx = self.app.app_context()
        self.ctx.push()
        self.company = ClientCompany.query.first()

    def tearDown(self):
        db.session.rollback()
        self.ctx.pop()

    def _run(self, month, status="Draft"):
        run = PayrollRun(month=month, year=2026, status=status, client_company_id=self.company.id)
        db.session.add(run)
        db.session.flush()
        return run

    def test_open_runs_record_the_version_for_their_period(self):
        august, september = self._run("August"), self._run("September")
        statutory_rate_for_run(august)
        statutory_rate_for_run(september)
        self.assertEqual(august.statutory_rate.effective_from, date(2026, 1, 1))
        self.assertEqual(september.statutory_rate.effective_from, date(2026, 9, 1))

    def test_a_finalised_run_keeps_the_version_it_was_approved_with(self):
        run = self._run("October")
        statutory_rate_for_run(run)
        approved_with = run.statutory_rate_id
        run.status = "Approved"
        sept = StatutoryRate.active_for(date(2026, 9, 1))
        backdated = StatutoryRate(
            effective_from=date(2026, 10, 1), paye_bands_json=sept.paye_bands_json,
            ssf_employee_rate=sept.ssf_employee_rate, ssf_employer_rate=sept.ssf_employer_rate,
        )
        db.session.add(backdated)
        db.session.flush()
        statutory_rate_for_run(run)
        self.assertEqual(run.statutory_rate_id, approved_with)


if __name__ == "__main__":
    unittest.main()
