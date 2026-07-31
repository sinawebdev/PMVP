"""Phase 1, Task 1.4 — the runs-list filter is a server-side query.

The filter used to hide rows in the browser while the list itself was paginated
on the server (RUNS_PER_PAGE). The two disagreed: a run that matched the term but
sat on page 2 was never in the DOM to be revealed, so the page confidently showed
nothing. These tests pin the fix — the term is a query predicate, it survives
pagination, and it travels in the URL.
"""

import os
import unittest

os.environ["SKIP_DOTENV"] = "true"
os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["SEED_DEMO_DATA"] = "true"
os.environ["PERSISTENCE_REQUIRED"] = "false"

from app import create_app, db  # noqa: E402
from app.models import ClientCompany, PayrollRun, User  # noqa: E402
from app.payroll import RUNS_PER_PAGE  # noqa: E402
from app.seed import DEMO_PASSWORD  # noqa: E402

NEEDLE_MONTH = "Zzytember"  # deliberately unlike any real month


class RunsFilterIsServerSideTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app()
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()
        self.ctx = self.app.app_context()
        self.ctx.push()
        self.admin = User.query.filter_by(email="admin@payrolla.com").first()
        self.company = ClientCompany.query.first()
        self.client.post(
            "/login", data={"email": self.admin.email, "password": DEMO_PASSWORD}
        )

    def tearDown(self):
        db.session.remove()
        self.ctx.pop()

    def _rows(self, url):
        """The runs table's own <tbody>, so an assertion can't be satisfied by
        the search term being echoed back in the input or the result count."""
        body = self.client.get(url).get_data(as_text=True)
        start = body.find('id="runs-table"')
        self.assertNotEqual(start, -1, "runs table not rendered")
        open_tag = body.find("<tbody>", start)
        close_tag = body.find("</tbody>", open_tag)
        return body[open_tag:close_tag]

    def _make_runs(self, count, month):
        for i in range(count):
            db.session.add(
                PayrollRun(
                    month=month,
                    year=2090 + (i % 5),
                    status="Draft",
                    created_by=self.admin.id,
                    client_company_id=self.company.id,
                    total_workers=1,
                    total_net_pay=100,
                    source_filename=f"f{i}.xlsx",
                )
            )
        db.session.commit()

    def test_match_beyond_the_first_page_is_still_found(self):
        """The original defect: enough runs to fill page 1, with the only match
        pushed onto a later page."""
        # The needle is created FIRST, so the newest-first ordering buries it.
        self._make_runs(1, NEEDLE_MONTH)
        self._make_runs(RUNS_PER_PAGE + 5, "January")

        self.assertNotIn(
            NEEDLE_MONTH, self._rows("/payroll/runs"), "needle should be off page 1"
        )
        self.assertIn(NEEDLE_MONTH, self._rows(f"/payroll/runs?q={NEEDLE_MONTH}"))

    def test_filter_matches_client_name(self):
        self._make_runs(1, "January")
        self.assertIn(
            self.company.name, self._rows(f"/payroll/runs?q={self.company.name[:6]}")
        )

    def test_non_matching_term_returns_no_rows(self):
        self._make_runs(2, NEEDLE_MONTH)
        self.assertNotIn(
            NEEDLE_MONTH, self._rows("/payroll/runs?q=NoSuchClientAnywhere")
        )

    def test_filter_term_survives_pagination_links(self):
        self._make_runs(RUNS_PER_PAGE + 5, "January")
        body = self.client.get("/payroll/runs?q=January").get_data(as_text=True)
        # The Next link must carry the term, or page 2 silently drops the filter.
        self.assertIn("q=January", body)

    def test_filter_combines_with_the_status_filter(self):
        self._make_runs(2, NEEDLE_MONTH)
        # Those runs are Draft, so an Approved-scoped search must not return them.
        self.assertNotIn(
            NEEDLE_MONTH, self._rows(f"/payroll/runs?q={NEEDLE_MONTH}&status=Approved")
        )
        # …but the same term without the status scope does.
        self.assertIn(NEEDLE_MONTH, self._rows(f"/payroll/runs?q={NEEDLE_MONTH}"))

    def test_no_client_side_row_hiding_filter_remains(self):
        """The browser-side filter must be gone, not merely supplemented."""
        body = self.client.get("/payroll/runs").get_data(as_text=True)
        self.assertNotIn('data-table-filter="runs-table"', body)


if __name__ == "__main__":
    unittest.main()
