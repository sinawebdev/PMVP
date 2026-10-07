"""SMS Phase 2 migration (3f2984c70c03): up from empty, down, and up again.

``tests/test_migration_chain.py`` already proves the chain builds the schema
the models describe. This pins the rest of what the plan asks of this one
revision: its downgrade really reverses it, existing delivery rows survive both
directions (SQLite rebuilds the table to make these changes), and the unique
(item, channel) constraint is enforced on a chain-built database.

SQLite only, like CI. PostgreSQL is checked by hand before deploying.
"""
import os
import tempfile
import unittest

os.environ["SKIP_DOTENV"] = "true"
os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["PERSISTENCE_REQUIRED"] = "false"

from sqlalchemy import inspect, text  # noqa: E402
from sqlalchemy.exc import IntegrityError  # noqa: E402

from app import create_app, db  # noqa: E402

BEFORE = "a1c5e9b73f40"
NEW_DELIVERY_COLUMNS = {"units", "claimed_at", "template_version"}


class SmsMigrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._saved = {
            key: os.environ.get(key)
            for key in ("PAYROLLA_INSTANCE_PATH", "AUTO_INIT_DB", "DATABASE_URL")
        }
        cls._tmp = tempfile.TemporaryDirectory()
        # File-backed: each in-memory connection would get its own blank database.
        os.environ.pop("DATABASE_URL", None)
        os.environ["AUTO_INIT_DB"] = "false"
        os.environ["PAYROLLA_INSTANCE_PATH"] = cls._tmp.name
        cls.app = create_app()

    @classmethod
    def tearDownClass(cls):
        for key, value in cls._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        with cls.app.app_context():
            db.engine.dispose()
        try:
            cls._tmp.cleanup()
        except (OSError, PermissionError):
            pass  # Windows holds the SQLite file briefly after dispose

    def _schema(self):
        inspector = inspect(db.engine)  # a fresh inspector: it caches reflection
        tables = set(inspector.get_table_names())
        delivery = inspector.get_columns("payslip_delivery")
        return {
            "tables": tables,
            "delivery_columns": {c["name"] for c in delivery},
            "batch_columns": {c["name"] for c in inspector.get_columns("distribution_batch")},
            "delivery_unique": [
                tuple(u["column_names"])
                for u in inspector.get_unique_constraints("payslip_delivery")
            ],
            "delivery_indexes": {i["name"] for i in inspector.get_indexes("payslip_delivery")},
        }

    def _assert_upgraded(self):
        schema = self._schema()
        self.assertIn("payslip_link", schema["tables"])
        self.assertLessEqual(NEW_DELIVERY_COLUMNS, schema["delivery_columns"])
        self.assertIn("unknown_count", schema["batch_columns"])
        self.assertIn(("payroll_item_id", "channel"), schema["delivery_unique"])
        self.assertNotIn("ix_payslip_delivery_item_channel", schema["delivery_indexes"])

    def _delivery_rows(self):
        return db.session.execute(
            text("SELECT id, status, attempts FROM payslip_delivery ORDER BY id")
        ).all()

    def test_up_from_empty_down_and_up_again_keeping_existing_rows(self):
        from flask_migrate import downgrade, upgrade

        with self.app.app_context():
            upgrade(revision=BEFORE)
            # SQLite does not enforce foreign keys here, so a bare row is enough.
            db.session.execute(text(
                "INSERT INTO payslip_delivery (payroll_item_id, payroll_run_id, channel, "
                "status, attempts) VALUES (1, 1, 'sms', 'failed', 4)"
            ))
            db.session.commit()
            before = self._delivery_rows()

            upgrade()
            self._assert_upgraded()
            self.assertEqual(self._delivery_rows(), before)

            downgrade(revision=BEFORE)
            schema = self._schema()
            self.assertNotIn("payslip_link", schema["tables"])
            self.assertFalse(NEW_DELIVERY_COLUMNS & schema["delivery_columns"])
            self.assertNotIn("unknown_count", schema["batch_columns"])
            self.assertNotIn(("payroll_item_id", "channel"), schema["delivery_unique"])
            self.assertIn("ix_payslip_delivery_item_channel", schema["delivery_indexes"])
            self.assertEqual(self._delivery_rows(), before)

            upgrade()
            self._assert_upgraded()
            self.assertEqual(self._delivery_rows(), before)

            # The constraint is real on a chain-built database, not just declared.
            with self.assertRaises(IntegrityError):
                db.session.execute(text(
                    "INSERT INTO payslip_delivery (payroll_item_id, payroll_run_id, "
                    "channel, status, attempts) VALUES (1, 1, 'sms', 'pending', 0)"
                ))
                db.session.commit()
            db.session.rollback()


if __name__ == "__main__":
    unittest.main()
