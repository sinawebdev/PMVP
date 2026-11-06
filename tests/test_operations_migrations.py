"""Operations & Onboarding — the sprint's two schema migrations.

Both are additive, so the risk is not "does it apply" but "does it apply
reversibly": a deploy that has to be rolled back must be able to drop these
columns cleanly on SQLite (batch mode) too. The chain builds the database from
empty up to the end of this sprint, walks back to the revision before it, then
walks forward to head. It cannot start from head: 9d2026c1020 deliberately
refuses to downgrade, so nothing at head can ever walk back this far.
"""

import os
import tempfile
import unittest

os.environ["SKIP_DOTENV"] = "true"
os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["PERSISTENCE_REQUIRED"] = "false"

import sqlalchemy as sa  # noqa: E402

from app import create_app, db  # noqa: E402

# The head immediately before this sprint's migrations, and its last migration.
BEFORE_SPRINT = "b1c3e5f70a29"
SPRINT_END = "e5a2c8d13f60"
NEW_CLIENT_COLUMNS = {"company_code", "address", "notes"}


class OperationsMigrationTestCase(unittest.TestCase):
    def test_single_head(self):
        from alembic.config import Config
        from alembic.script import ScriptDirectory

        cfg = Config()
        cfg.set_main_option("script_location", "migrations")
        self.assertEqual(len(ScriptDirectory.from_config(cfg).get_heads()), 1)

    def test_onboarding_and_expense_columns_round_trip(self):
        from flask_migrate import downgrade as fm_downgrade
        from flask_migrate import upgrade as fm_upgrade

        tmp = tempfile.mkdtemp()
        dbfile = os.path.join(tmp, "operations_mig.sqlite")
        previous = os.environ.get("DATABASE_URL")
        previous_init = os.environ.get("AUTO_INIT_DB")
        os.environ["DATABASE_URL"] = f"sqlite:///{dbfile}"
        os.environ["AUTO_INIT_DB"] = "false"  # an empty database: the chain builds it
        try:
            app = create_app()
            with app.app_context():
                fm_upgrade(revision=SPRINT_END)
                client_columns = {c["name"] for c in sa.inspect(db.engine).get_columns("client_company")}
                self.assertTrue(NEW_CLIENT_COLUMNS <= client_columns)

                fm_downgrade(revision=BEFORE_SPRINT)
                inspector = sa.inspect(db.engine)
                client_columns = {c["name"] for c in inspector.get_columns("client_company")}
                expense_columns = {c["name"] for c in inspector.get_columns("expense")}
                self.assertFalse(client_columns & NEW_CLIENT_COLUMNS)
                self.assertNotIn("notes", expense_columns)

                fm_upgrade(revision="head")
                inspector = sa.inspect(db.engine)
                client_columns = {c["name"] for c in inspector.get_columns("client_company")}
                expense_columns = {c["name"] for c in inspector.get_columns("expense")}
                self.assertTrue(NEW_CLIENT_COLUMNS <= client_columns)
                self.assertIn("notes", expense_columns)
                # The company code is an identifier, so its uniqueness must come
                # back with it — not just the column.
                indexes = {i["name"]: i for i in inspector.get_indexes("client_company")}
                self.assertIn("ix_client_company_company_code", indexes)
                self.assertTrue(indexes["ix_client_company_company_code"]["unique"])
        finally:
            os.environ["DATABASE_URL"] = previous or "sqlite:///:memory:"
            if previous_init is None:
                os.environ.pop("AUTO_INIT_DB", None)
            else:
                os.environ["AUTO_INIT_DB"] = previous_init


if __name__ == "__main__":
    unittest.main()
