"""Phase 5 — performance regression guards (Workstream C).

A lightweight SQL counter (a before_cursor_execute listener) proves the runs list
does not issue a query per row (the client_company N+1) and that the bulk-apply
path fetches its runs in one query rather than one-per-run.
"""
import os
import unittest

os.environ["SKIP_DOTENV"] = "true"
os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["SEED_DEMO_DATA"] = "true"
os.environ["PERSISTENCE_REQUIRED"] = "false"

from sqlalchemy import event  # noqa: E402

from app import create_app, db  # noqa: E402
from app.models import ClientCompany, PayrollRun  # noqa: E402


class _QueryCounter:
    """Count SQL statements executed on the bound engine within the block."""

    def __init__(self):
        self.count = 0

    def _on_exec(self, *_args, **_kwargs):
        self.count += 1

    def __enter__(self):
        event.listen(db.engine, "before_cursor_execute", self._on_exec)
        return self

    def __exit__(self, *_exc):
        event.remove(db.engine, "before_cursor_execute", self._on_exec)


class RunsListNoNPlusOneTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app()
        self.app.config["TESTING"] = True
        self.ctx = self.app.app_context()
        self.ctx.push()
        self.client = self.app.test_client()
        self.company = ClientCompany.query.first()
        self._login()

    def tearDown(self):
        db.session.remove()
        self.ctx.pop()

    def _login(self):
        self.client.post(
            "/login", data={"email": "admin@chrisnat.local", "password": "password123"},
            follow_redirects=True,
        )

    def _add_runs(self, n):
        for i in range(n):
            db.session.add(PayrollRun(
                client_company_id=self.company.id, month="January", year=2000 + i,
                status="Draft", upload_type="standard",
            ))
        db.session.commit()

    def _get_runs_query_count(self):
        with _QueryCounter() as counter:
            resp = self.client.get("/payroll/runs")
            self.assertEqual(resp.status_code, 200)
        return counter.count

    def test_runs_list_query_count_is_constant_in_row_count(self):
        # The client_company column is rendered per row; without eager loading the
        # query count grows with the number of runs (an N+1). It must not.
        self._add_runs(3)
        small = self._get_runs_query_count()
        self._add_runs(6)  # 9 runs total now
        large = self._get_runs_query_count()
        self.assertEqual(
            small, large,
            f"runs list query count scaled with rows ({small} -> {large}) — N+1 regressed",
        )


class BulkApplyBatchFetchTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app()
        self.app.config["TESTING"] = True
        self.ctx = self.app.app_context()
        self.ctx.push()
        self.company = ClientCompany.query.first()

    def tearDown(self):
        db.session.remove()
        self.ctx.pop()

    def test_bulk_apply_fetches_runs_in_one_query(self):
        from flask_login import login_user

        from app.models import User
        from app.payroll import _bulk_apply

        ids = []
        for i in range(5):
            run = PayrollRun(client_company_id=self.company.id, month="March",
                             year=2010 + i, status="Draft", upload_type="standard")
            db.session.add(run)
            db.session.flush()
            ids.append(run.id)
        db.session.commit()
        user = User.query.filter_by(email="admin@chrisnat.local").first()

        # predicate=False exercises the skip branch, which builds a label from
        # run.client_company — batching must eager-load it so neither the fetch nor
        # the label reporting scales one-query-per-run.
        with self.app.test_request_context():
            login_user(user)
            with _QueryCounter() as counter:
                _bulk_apply(ids, predicate=lambda role, run: False,
                            action=lambda run: None, verb="noop")
        self.assertLessEqual(
            counter.count, 3,
            f"bulk apply issued {counter.count} queries for 5 runs — expected a single batched fetch",
        )


if __name__ == "__main__":
    unittest.main()
