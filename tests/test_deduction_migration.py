"""Exercise the real migration chain against pre-update financial records."""
import json
import os
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

import sqlalchemy as sa
from flask_migrate import upgrade, check
from alembic import command

from app import create_app, db
from app.models import Employee, PayrollItem, StatutoryRate
from app.payroll_calculations.deductions import context_for_employee, deduction_adjustments
from app.seed import HISTORICAL_PAYE_BANDS


class DeductionMigrationTests(unittest.TestCase):
    def test_existing_records_upgrade_without_invented_approvals_or_changed_pay(self):
        with tempfile.TemporaryDirectory() as instance_path, patch.dict(os.environ, {
            "APP_ENV": "development", "FLASK_ENV": "development",
            "SKIP_DOTENV": "true", "DATABASE_URL": "sqlite:///:memory:",
            "AUTO_INIT_DB": "false", "PERSISTENCE_REQUIRED": "false",
            "DISTRIBUTION_WORKER_INLINE": "false",
            "PAYROLLA_INSTANCE_PATH": instance_path,
        }):
            app = create_app()
            with app.app_context():
                migrations = str(Path(__file__).resolve().parents[1] / "migrations")
                upgrade(directory=migrations, revision="3f2984c70c03")
                # Use the pre-migration schema, not today's ORM, to reproduce
                # an actual existing installation faithfully.
                old_meta = sa.MetaData()
                with db.engine.begin() as connection:
                    rates = sa.Table("statutory_rates", old_meta, autoload_with=connection)
                    connection.execute(rates.insert().values(
                        effective_from=date(2026, 1, 1), ssf_employee_rate=0.055,
                        ssf_employer_rate=0.13, paye_bands_json=json.dumps(HISTORICAL_PAYE_BANDS),
                        overtime_rate_low=0.05, overtime_rate_high=0.10,
                        overtime_basic_threshold=0.5, bonus_rate=0.05,
                        bonus_annual_basic_threshold=0.15, icu_member_rate=0.03,
                        overtime_junior_monthly_threshold=1500,
                    ))
                    employees = sa.Table("employee", old_meta, autoload_with=connection)
                    employee_id = connection.execute(employees.insert().values(
                        staff_id="MIGRATION-TEST", full_name="Migration test employee",
                        basic_salary=1000, tax_relief_monthly=125, icu_member=False,
                    )).inserted_primary_key[0]
                    runs = sa.Table("payroll_run", old_meta, autoload_with=connection)
                    run_id = connection.execute(runs.insert().values(
                        month="September", year=2026, status="Approved",
                        total_gross_pay=1000, total_net_pay=846,
                    )).inserted_primary_key[0]
                    items = sa.Table("payroll_item", old_meta, autoload_with=connection)
                    item_id = connection.execute(items.insert().values(
                        payroll_run_id=run_id, employee_id=employee_id,
                        staff_id="MIGRATION-TEST", full_name="Migration test employee",
                        basic_salary=1000, gross_pay=1000, paye=99, ssnit=55,
                        total_deductions=154, net_pay=846,
                    )).inserted_primary_key[0]
                upgrade(directory=migrations)
                self.assertEqual(db.session.execute(sa.text("select version_num from alembic_version")).scalar_one(), "9d2026c1020")
                item = db.session.get(PayrollItem, item_id)
                self.assertEqual((item.gross_pay, item.paye, item.ssnit, item.net_pay), (1000, 99, 55, 846))
                self.assertEqual(item.deductions_policy_version, 0)
                employee = db.session.get(Employee, employee_id)
                self.assertEqual(employee.tax_relief_monthly, 125)
                self.assertFalse(employee.pf_scheme_approved)
                self.assertIsNone(employee.overtime_is_junior)
                self.assertEqual(employee.tax_relief_approvals, [])
                context = context_for_employee(employee, date(2026, 10, 1))
                current = StatutoryRate.active_for(date(2026, 10, 1))
                self.assertEqual(current.deductions_policy_version, 1)
                self.assertEqual(StatutoryRate.active_for(date(2026, 8, 1)).deductions_policy_version, 0)
                computed = deduction_adjustments(current, 1000, requested_relief=125, context=context)
                self.assertEqual(computed.tax_relief_applied, 0)
                self.assertEqual(computed.ssnit, 55)
                db.session.remove()
                upgrade(directory=migrations)
                self.assertEqual(StatutoryRate.query.count(), 2)
                db.session.remove()
                check(directory=migrations)
                config = app.extensions["migrate"].migrate.get_config(migrations)
                with self.assertRaisesRegex(RuntimeError, "automatic downgrade is refused"):
                    command.downgrade(config, "8b62026a0901")
                db.session.remove()
                self.assertEqual(db.session.get(PayrollItem, item_id).net_pay, 846)
                db.session.remove()
                db.engine.dispose()


if __name__ == "__main__":
    unittest.main()
