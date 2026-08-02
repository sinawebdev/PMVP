"""Phase 4, Task 4.1 — the detail page's cost must not track the payroll's size.

The page used to render every imported row: a 400-worker run built a 400-row
table on every view, including the views whose only purpose was to click
Approve. Two things made it scale — the unpaginated grid, and
``PayrollRun.warning_count``, which walked the whole ``items`` collection in
Python to produce one integer (and was also summed across every run of a
company on the client detail page).

These tests pin the fix at the boundary that matters: what the response
actually contains, and how many rows the database was asked for.
"""

import os
import unittest

os.environ["SKIP_DOTENV"] = "true"
os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["SEED_DEMO_DATA"] = "true"
os.environ["PERSISTENCE_REQUIRED"] = "false"

from sqlalchemy import event  # noqa: E402

from app import create_app, db  # noqa: E402
from app.models import ClientCompany, PayrollItem, PayrollRun  # noqa: E402
from app.payroll import ITEMS_PER_PAGE  # noqa: E402

LARGE_RUN = 400


class PayrollDetailBoundedTestCase(unittest.TestCase):
    def setUp(self):
        self.app = create_app()
        self.app.config["TESTING"] = True
        self.app.config["WTF_CSRF_ENABLED"] = False
        self.client = self.app.test_client()
        self.ctx = self.app.app_context()
        self.ctx.push()
        self.assertEqual(
            self.client.post(
                "/login",
                data={"email": "admin@payrolla.com", "password": "password123"},
            ).status_code,
            302,
        )
        company = ClientCompany(name="Bounded Render Co", status="Active")
        db.session.add(company)
        db.session.commit()
        self.run = PayrollRun(
            month="January", year=2024, status="Draft",
            client_company_id=company.id,
            total_workers=LARGE_RUN, total_net_pay=1000,
            total_rows_imported=LARGE_RUN,
        )
        db.session.add(self.run)
        db.session.commit()
        db.session.bulk_save_objects([
            PayrollItem(
                payroll_run_id=self.run.id,
                staff_id=f"EMP{i:04d}",
                full_name=f"Worker {i:04d}",
                net_pay=100,
                # Every tenth row is flagged, so warning_count has real work to do.
                validation_status="Warning" if i % 10 == 0 else "OK",
            )
            for i in range(LARGE_RUN)
        ])
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        self.ctx.pop()

    def _detail(self, **params):
        return self.client.get(
            f"/payroll/runs/{self.run.id}", query_string=params
        ).get_data(as_text=True)

    def test_first_page_renders_one_page_of_rows_not_the_run(self):
        html = self._detail()
        rendered = html.count("/payroll/items/")
        self.assertEqual(rendered, ITEMS_PER_PAGE)
        self.assertLess(rendered, LARGE_RUN)

    def test_footer_states_the_true_total(self):
        html = self._detail()
        self.assertIn(f"of {LARGE_RUN}", html)

    def test_last_page_is_reachable_and_holds_the_remainder(self):
        last = -(-LARGE_RUN // ITEMS_PER_PAGE)
        html = self._detail(page=last)
        self.assertEqual(html.count("/payroll/items/"), LARGE_RUN - (last - 1) * ITEMS_PER_PAGE)
        # Ordered by name, so the final page ends on the last worker — an
        # unordered paginated query could repeat a row across two pages instead.
        self.assertIn(f"Worker {LARGE_RUN - 1:04d}", html)

    def test_warning_count_is_a_count_not_a_scan(self):
        """The property must not load PayrollItem rows into the session."""
        loaded = []

        @event.listens_for(PayrollItem, "load")
        def _record(target, _context):  # pragma: no cover - bookkeeping
            loaded.append(target)

        try:
            db.session.expire_all()
            count = self.run.warning_count
        finally:
            event.remove(PayrollItem, "load", _record)

        self.assertEqual(count, LARGE_RUN // 10)
        self.assertEqual(loaded, [], "warning_count loaded PayrollItem rows")

    def test_item_count_is_a_count_not_a_scan(self):
        loaded = []

        @event.listens_for(PayrollItem, "load")
        def _record(target, _context):  # pragma: no cover - bookkeeping
            loaded.append(target)

        try:
            db.session.expire_all()
            count = self.run.item_count
        finally:
            event.remove(PayrollItem, "load", _record)

        self.assertEqual(count, LARGE_RUN)
        self.assertEqual(loaded, [], "item_count loaded PayrollItem rows")

    def test_whole_response_never_materialises_every_row(self):
        """End to end: one page view, one page of PayrollItem objects."""
        loaded = []

        @event.listens_for(PayrollItem, "load")
        def _record(target, _context):  # pragma: no cover - bookkeeping
            loaded.append(target)

        try:
            db.session.expire_all()
            self._detail()
        finally:
            event.remove(PayrollItem, "load", _record)

        self.assertLessEqual(len(loaded), ITEMS_PER_PAGE)


if __name__ == "__main__":
    unittest.main()
