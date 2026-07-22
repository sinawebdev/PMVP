"""Phase 2 — possible-duplicate payroll detection.

find_possible_duplicates() is advisory: it flags OTHER runs for the same
client whose worker count and net pay exactly match, which is what a client
re-uploading the same payroll under the wrong month looks like. It never
blocks a lifecycle transition — see app/risk.py for the distinction from the
exact same-client/month/year block enforced at import time.
"""

import os
import unittest

os.environ["SKIP_DOTENV"] = "true"
os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["SEED_DEMO_DATA"] = "true"
os.environ["PERSISTENCE_REQUIRED"] = "false"

from app import create_app, db  # noqa: E402
from app.models import ClientCompany, PayrollRun  # noqa: E402
from app.risk import find_possible_duplicates  # noqa: E402


class DuplicateDetectionTestCase(unittest.TestCase):
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

    def _run(self, *, status="Draft", net, workers, month="January", company=None):
        run = PayrollRun(
            month=month, year=2024, status=status,
            client_company_id=(company or self.company).id,
            total_workers=workers, total_net_pay=net,
        )
        db.session.add(run)
        db.session.flush()
        return run

    def test_no_match_returns_empty(self):
        run = self._run(net=1000, workers=10, month="January")
        db.session.commit()
        self.assertEqual(find_possible_duplicates(run), [])

    def test_matching_totals_different_period_flagged(self):
        earlier = self._run(net=1000, workers=10, month="January")
        current = self._run(net=1000, workers=10, month="February")
        db.session.commit()
        dupes = find_possible_duplicates(current)
        self.assertEqual([d.id for d in dupes], [earlier.id])

    def test_different_totals_not_flagged(self):
        self._run(net=1000, workers=10, month="January")
        current = self._run(net=1200, workers=10, month="February")
        db.session.commit()
        self.assertEqual(find_possible_duplicates(current), [])

    def test_rejected_run_excluded(self):
        self._run(status="Rejected", net=1000, workers=10, month="January")
        current = self._run(net=1000, workers=10, month="February")
        db.session.commit()
        self.assertEqual(find_possible_duplicates(current), [])

    def test_other_client_not_flagged(self):
        other = ClientCompany(name="Other Co", status="Active")
        db.session.add(other)
        db.session.flush()
        self._run(net=1000, workers=10, month="January", company=other)
        current = self._run(net=1000, workers=10, month="January")
        db.session.commit()
        self.assertEqual(find_possible_duplicates(current), [])

    def test_zero_totals_not_flagged(self):
        self._run(net=0, workers=0, month="January")
        current = self._run(net=0, workers=0, month="February")
        db.session.commit()
        self.assertEqual(find_possible_duplicates(current), [])

    def test_detail_page_renders_duplicate_warning(self):
        self._run(net=1000, workers=10, month="January")
        current = self._run(net=1000, workers=10, month="February")
        db.session.commit()
        html = self.client.get(f"/payroll/runs/{current.id}").get_data(as_text=True)
        self.assertIn("Possible duplicate payroll", html)


if __name__ == "__main__":
    unittest.main()
