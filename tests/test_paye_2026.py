"""September 2026 PAYE: independently calculated tax and effective-date guards.

Expected tax amounts below are hand calculated from GRA's marginal portions,
not obtained from the engine's bands. Every test owns an empty in-memory DB.
"""

import importlib.util
import json
import os
import tempfile
import unittest
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from alembic.migration import MigrationContext
from alembic.operations import Operations

from app import create_app, db
from app.models import ClientCompany, Employee, PayrollRun, RawPayEntry, StatutoryRate, WageRateProfile
from app.payroll_calculations import statutory_rate_for_run
from app.payroll_calculations.hourly import HourlyShiftCalculator
from app.payroll_calculations.salaried import SalariedCalculator
from app.raw_engine.compute import PayslipInputs, compute_payslip
from app.seed import ensure_paye_2026_rates, seed_statutory_rates


EFFECTIVE_FROM = date(2026, 9, 1)
NON_PAYE_FIELDS = (
    "ssf_employee_rate",
    "ssf_employer_rate",
    "overtime_rate_low",
    "overtime_rate_high",
    "overtime_basic_threshold",
    "bonus_rate",
    "bonus_annual_basic_threshold",
    "overtime_junior_monthly_threshold",
    "icu_member_rate",
)

# At each threshold, the tax on all lower portions has already accumulated.
# Examples: 80 * 5% = 4; 4 + 100 * 10% = 14;
# 14 + 2,900 * 17.5% = 521.50; +16,000 * 25% = 4,521.50;
# +30,332 * 30% = 13,621.10. Decimal half-pesewas round upwards.
TAX_CASES = (
    (-1, 0),
    (0, 0),
    (587, 0),
    (588, 0),
    (589, 0.05),
    (667, 3.95),
    (668, 4),
    (668.05, 4.01),
    (669, 4.10),
    (767, 13.90),
    (768, 14),
    (768.20, 14.04),
    (769, 14.18),
    (3667, 521.33),
    (3668, 521.50),
    (3669, 521.75),
    (19667, 4521.25),
    (19668, 4521.50),
    (19669, 4521.80),
    (49999, 13620.80),
    (50000, 13621.10),
    (50000.10, 13621.14),
    (50001, 13621.45),
    (100000, 31121.10),
)


class PayeSeptember2026Tests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.env = patch.dict(
            os.environ,
            {
                "APP_ENV": "development",
                "FLASK_ENV": "development",
                "DATABASE_URL": "sqlite:///:memory:",
                "SKIP_DOTENV": "true",
                "PERSISTENCE_REQUIRED": "false",
                "AUTO_INIT_DB": "false",
                "DISTRIBUTION_WORKER_INLINE": "false",
                "PAYROLLA_INSTANCE_PATH": self.temp_dir.name,
            },
        )
        self.env.start()
        self.addCleanup(self.env.stop)
        self.addCleanup(self.temp_dir.cleanup)
        self.app = create_app()
        self.app.config["TESTING"] = True
        self.ctx = self.app.app_context()
        self.ctx.push()
        self.addCleanup(self.ctx.pop)
        db.create_all()
        self.addCleanup(db.session.remove)

    def seed(self):
        seed_statutory_rates()
        db.session.flush()
        return StatutoryRate.active_for(EFFECTIVE_FROM)

    def migrate(self, operation="upgrade"):
        path = (
            Path(__file__).resolve().parents[1]
            / "migrations" / "versions"
            / "8b62026a0901_add_september_2026_paye_rates.py"
        )
        spec = importlib.util.spec_from_file_location("paye_2026_migration_test", path)
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)
        db.session.commit()
        with db.engine.begin() as connection:
            with Operations.context(MigrationContext.configure(connection)):
                getattr(migration, operation)()
        db.session.expire_all()

    @staticmethod
    def snapshot(rate):
        # Read the stored representation; SQLite drops the UTC timezone flag
        # from created_at on reload, so pre/post snapshots must both reload.
        db.session.refresh(rate)
        return {
            column.name: getattr(rate, column.name)
            for column in StatutoryRate.__table__.columns
        }

    def custom_rate(self, effective_from, marker):
        """Deliberately non-default ancillary settings detect accidental resets."""
        rate = StatutoryRate(
            effective_from=effective_from,
            ssf_employee_rate=0.051 + marker / 10000,
            ssf_employer_rate=0.121 + marker / 10000,
            overtime_rate_low=0.041 + marker / 10000,
            overtime_rate_high=0.091 + marker / 10000,
            overtime_basic_threshold=0.41 + marker / 10000,
            bonus_rate=0.031 + marker / 10000,
            bonus_annual_basic_threshold=0.141 + marker / 10000,
            overtime_junior_monthly_threshold=1400 + marker,
            icu_member_rate=0.021 + marker / 10000,
            paye_bands_json=json.dumps([{"over": 100, "rate": 0.10, "base": marker}]),
            notes=f"Custom version {marker}",
        )
        db.session.add(rate)
        db.session.flush()
        return rate

    def test_new_tax_matches_hand_calculation_at_every_band_and_rounds_half_up(self):
        rate = self.seed()
        for taxable, expected in TAX_CASES:
            with self.subTest(taxable=taxable):
                self.assertEqual(rate.compute_paye(taxable), expected)

    def test_august_uses_legacy_but_september_and_october_use_new_version(self):
        current = self.seed()
        legacy = StatutoryRate.active_for(date(2026, 8, 31))
        self.assertEqual(legacy.effective_from, date(2026, 1, 1))
        self.assertEqual(current.effective_from, EFFECTIVE_FROM)
        for month, expected in (("August", legacy), ("September", current), ("October", current)):
            with self.subTest(month=month):
                actual = statutory_rate_for_run(SimpleNamespace(month=month, year=2026))
                self.assertEqual(actual.id, expected.id)
        # Existing client figures stay reproducible; the new schedule changes
        # this SAME taxable amount by 11.15 cedis starting in September.
        self.assertEqual(legacy.compute_paye(2810.74), 382.63)
        self.assertEqual(current.compute_paye(2810.74), 371.48)

    def test_seeding_twice_is_idempotent_and_preserves_legacy_row(self):
        current = self.seed()
        legacy = StatutoryRate.active_for(date(2026, 8, 31))
        before = self.snapshot(legacy)
        seed_statutory_rates()
        db.session.flush()
        self.assertEqual(StatutoryRate.query.count(), 2)
        self.assertEqual(StatutoryRate.active_for(EFFECTIVE_FROM).id, current.id)
        self.assertEqual(self.snapshot(legacy), before)

    def test_new_version_copies_last_prior_ancillary_rates_and_preserves_future(self):
        self.custom_rate(date(2025, 1, 1), 1)
        prior = self.custom_rate(date(2026, 8, 1), 7)
        future = self.custom_rate(date(2027, 1, 1), 99)
        prior_before = self.snapshot(prior)
        future_before = self.snapshot(future)
        current = ensure_paye_2026_rates()
        db.session.flush()
        self.assertEqual(current.effective_from, EFFECTIVE_FROM)
        for field in NON_PAYE_FIELDS:
            with self.subTest(field=field):
                self.assertEqual(getattr(current, field), getattr(prior, field))
                self.assertNotEqual(getattr(current, field), getattr(future, field))
        self.assertEqual(current.compute_paye(50000), 13621.10)
        self.assertEqual(self.snapshot(prior), prior_before)
        self.assertEqual(self.snapshot(future), future_before)
        self.assertEqual(StatutoryRate.query.count(), 4)

    def test_existing_september_version_is_preserved_without_overwrite(self):
        self.custom_rate(date(2026, 1, 1), 1)
        existing = self.custom_rate(EFFECTIVE_FROM, 42)
        before = self.snapshot(existing)
        actual = ensure_paye_2026_rates()
        seed_statutory_rates()
        db.session.flush()
        self.assertEqual(actual.id, existing.id)
        self.assertEqual(self.snapshot(existing), before)
        self.assertEqual(StatutoryRate.query.count(), 2)

    def test_helper_without_prior_version_does_not_guess_ancillary_rates(self):
        self.assertIsNone(ensure_paye_2026_rates())
        self.custom_rate(date(2027, 1, 1), 99)
        self.assertIsNone(ensure_paye_2026_rates())
        self.assertEqual(StatutoryRate.query.count(), 1)

    def test_migration_preserves_history_carries_prior_settings_and_is_idempotent(self):
        self.custom_rate(date(2025, 1, 1), 1)
        prior = self.custom_rate(date(2026, 8, 1), 7)
        future = self.custom_rate(date(2027, 1, 1), 99)
        prior_before = self.snapshot(prior)
        future_before = self.snapshot(future)
        # A future edit of application defaults must never alter this revision.
        with patch("app.seed.PAYE_BANDS_2026", []):
            self.migrate()
        current = StatutoryRate.query.filter_by(effective_from=EFFECTIVE_FROM).one()
        current_before = self.snapshot(current)
        for field in NON_PAYE_FIELDS:
            with self.subTest(field=field):
                self.assertEqual(getattr(current, field), getattr(prior, field))
                self.assertNotEqual(getattr(current, field), getattr(future, field))
        for taxable, expected in TAX_CASES:
            with self.subTest(taxable=taxable):
                self.assertEqual(current.compute_paye(taxable), expected)
        self.migrate()
        self.assertEqual(StatutoryRate.query.count(), 4)
        self.assertEqual(self.snapshot(current), current_before)
        self.assertEqual(self.snapshot(prior), prior_before)
        self.assertEqual(self.snapshot(future), future_before)
        # Downgrading code retains financial configuration used by past runs.
        self.migrate("downgrade")
        self.assertEqual(StatutoryRate.query.count(), 4)
        self.assertEqual(self.snapshot(current), current_before)

    def test_migration_does_not_overwrite_existing_september_configuration(self):
        self.custom_rate(date(2026, 1, 1), 1)
        existing = self.custom_rate(EFFECTIVE_FROM, 42)
        before = self.snapshot(existing)
        self.migrate()
        self.assertEqual(self.snapshot(existing), before)
        self.assertEqual(StatutoryRate.query.count(), 2)

    def test_migration_with_no_prior_rates_does_not_invent_configuration(self):
        self.migrate()
        self.assertEqual(StatutoryRate.query.count(), 0)
        future = self.custom_rate(date(2027, 1, 1), 99)
        before = self.snapshot(future)
        self.migrate()
        self.assertEqual(StatutoryRate.query.count(), 1)
        self.assertEqual(self.snapshot(future), before)

    def test_salaried_and_raw_paths_apply_the_new_schedule(self):
        rate = self.seed()
        # Using taxable allowances with zero basic isolates the PAYE schedule
        # while still running the complete public calculator entry points.
        for taxable, expected in TAX_CASES:
            if taxable < 0:
                continue
            with self.subTest(taxable=taxable):
                salaried = SalariedCalculator(rate).calculate(0, other_allowances=taxable)
                raw = compute_payslip(PayslipInputs(staff_id="PAYE2026", allowances=taxable), rate)
                self.assertEqual(salaried.ordinary_paye, expected)
                self.assertEqual(raw.ordinary_paye, expected)
                self.assertEqual(salaried.paye, raw.total_tax)
                self.assertEqual(salaried.net_pay, raw.net_pay)

    def test_new_schedule_requires_evidence_for_overtime_pf_and_relief(self):
        rate = self.seed()
        salaried = SalariedCalculator(rate).calculate(
            2000,
            other_allowances=200,
            overtime_pay=1500,
            productivity_bonus=5000,
            pf_fund_employee=50,
            tax_relief_monthly=40,
        )
        raw = compute_payslip(
            PayslipInputs(
                staff_id="PAYE2026",
                basic_wage=2000,
                allowances=200,
                overtime_pay=1500,
                bonus=5000,
                provident_fund=50,
                tax_relief_monthly=40,
            ),
            rate,
        )
        # Previous fixture assumed every OT/PF/relief concession. Without
        # evidence: OT1,500 is ordinary; PF50 remains cash-only; relief40 is not
        # allowed. 2,000 - 110 + 200 + 1,500 + 1,400 = 4,990; tax852 + bonus180.
        for result in (salaried, raw):
            self.assertEqual(result.taxable_income, 4990)
            self.assertEqual(result.ordinary_paye, 852)
            self.assertEqual(result.overtime_tax, 0)
            self.assertEqual(result.bonus_tax, 180)
            self.assertEqual(result.bonus_excess, 1400)
            self.assertEqual(result.net_pay, 7508)
        self.assertEqual(salaried.ssnit, 110)
        self.assertEqual(raw.employee_ssnit, 110)
        self.assertEqual(salaried.paye, 1032)
        self.assertEqual(raw.total_tax, 1032)

    def test_hourly_import_path_selects_september_rates_and_deducts_roster_relief(self):
        self.seed()
        company = ClientCompany(name="PAYE 2026 regression company")
        db.session.add(company)
        db.session.flush()
        employee = Employee(
            staff_id="PAYE26",
            full_name="PAYE regression worker",
            client_company_id=company.id,
            tax_relief_monthly=40,
            tax_relief_approval_ref="GRA-PAYE-REGRESSION",
            tax_relief_category="Combined approved award",
            tax_relief_valid_from=date(2026, 1, 1),
            tax_relief_valid_to=date(2026, 12, 31),
        )
        run = PayrollRun(
            client_company_id=company.id,
            month="September",
            year=2026,
            upload_type="raw",
            status="Draft",
        )
        db.session.add_all([employee, run])
        db.session.flush()
        for code, category, hourly_rate, hours in (
            ("NORM", "basic", 20, 100),
            ("OT", "overtime", 30, 50),
            ("ALLOW", "allowance", 200, 1),
            ("BONUS", "bonus", 5000, 1),
        ):
            db.session.add(WageRateProfile(
                client_company_id=company.id,
                pay_code=code,
                category=category,
                hourly_rate=hourly_rate,
            ))
            db.session.add(RawPayEntry(
                payroll_run_id=run.id,
                employee_id_str=employee.staff_id,
                pay_code=code,
                hours=hours,
            ))
        db.session.flush()
        rate = statutory_rate_for_run(run)
        self.assertEqual(rate.effective_from, EFFECTIVE_FROM)
        result = HourlyShiftCalculator(run, rate).calculate_run()[employee.staff_id]
        # Dated relief40 applies; no junior evidence, so OT1,500 is ordinary:
        # 2,000 - 110 + 200 - 40 + 1,500 + 1,400 = 4,950.
        self.assertEqual(result.taxable_income, 4950)
        self.assertEqual(result.ssnit, 110)
        self.assertEqual(result.ordinary_paye, 842)
        self.assertEqual(result.overtime_tax, 0)
        self.assertEqual(result.bonus_tax, 180)
        self.assertEqual(result.paye, 1022)
        self.assertEqual(result.net_pay, 7568)


if __name__ == "__main__":
    unittest.main()
