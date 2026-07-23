"""Phase 5 — database indexes (Workstream B).

Asserts the query-backed indexes exist on the built schema and that the migration
tree still resolves to a single head (so `flask db upgrade` stays linear).
"""
import os
import unittest

os.environ["SKIP_DOTENV"] = "true"
os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["SEED_DEMO_DATA"] = "false"
os.environ["PERSISTENCE_REQUIRED"] = "false"

import sqlalchemy as sa  # noqa: E402

from app import create_app, db  # noqa: E402

EXPECTED = {
    "payroll_item": {"ix_payroll_item_payroll_run_id"},
    "payslip_delivery": {
        "ix_payslip_delivery_status_next_retry",
        "ix_payslip_delivery_item_channel",
    },
    "distribution_batch": {"ix_distribution_batch_status_created"},
}


class Phase5IndexTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app()
        self.ctx = self.app.app_context()
        self.ctx.push()

    def tearDown(self):
        db.session.remove()
        self.ctx.pop()

    def test_query_backed_indexes_present(self):
        insp = sa.inspect(db.engine)
        for table, expected in EXPECTED.items():
            names = {ix["name"] for ix in insp.get_indexes(table)}
            missing = expected - names
            self.assertFalse(missing, f"{table} missing indexes: {missing}")

    def test_migrations_have_single_head(self):
        from alembic.config import Config
        from alembic.script import ScriptDirectory

        cfg = Config()
        cfg.set_main_option("script_location", "migrations")
        self.assertEqual(len(ScriptDirectory.from_config(cfg).get_heads()), 1)


if __name__ == "__main__":
    unittest.main()
