"""GATE 1 — Raw Hours Engine seed path.

Reconciled against the real production workbook ``DZ-PAYROLL JAN 2026.xlsx``
(DZVANS COMPANY LIMITED, January 2026): 181 employees, 137 ICU members, George
Akoto a non-member on basic 1800, and Richard Woode (DCL9)'s per-employee rate
table. The workbook is real client PII — decrypted locally into
``tests/fixtures/`` (gitignored), never committed. If the fixture is absent the
whole case skips with instructions rather than failing.
"""
import os
import unittest
from unittest import mock

os.environ["DATABASE_URL"] = "sqlite:///:memory:"

from app import create_app, db
from app.models import ClientCompany, Employee, WageRateProfile
from app.raw_engine import store as store_mod
from app.raw_engine.detection import (
    company_is_seeded,
    is_rich_raw_data,
)
from app.raw_engine.mapping import HeaderError
from app.raw_engine.seed import parse_rich_workbook
from app.raw_engine.store import persist_seed

FIXTURE_DIR = os.path.join(os.path.dirname(__file__), "fixtures")
DZ_FIXTURE = os.path.join(FIXTURE_DIR, "DZ-PAYROLL JAN 2026.xlsx")
THIN_FIXTURE = os.path.join(
    FIXTURE_DIR, "Rawdata to work on data_consolidation.xlsx"
)


@unittest.skipUnless(
    os.path.exists(DZ_FIXTURE),
    "Decrypted DZ specimen missing — place 'DZ-PAYROLL JAN 2026.xlsx' "
    "(password 'deeper', decrypted) in tests/fixtures/ to run GATE 1.",
)
class RawEngineSeedTestCase(unittest.TestCase):
    def setUp(self):
        self.app = create_app()
        self.app.config["TESTING"] = True
        self.ctx = self.app.app_context()
        self.ctx.push()
        self.client = ClientCompany(name="DZVANS COMPANY LIMITED", status="Active")
        db.session.add(self.client)
        db.session.commit()

    def tearDown(self):
        db.session.rollback()
        self.ctx.pop()

    def _parse(self):
        return parse_rich_workbook(DZ_FIXTURE, self.client.id)

    # ---- parsing ---------------------------------------------------------

    def test_parses_181_employees(self):
        context = self._parse()
        self.assertEqual(len(context.employees), 181)

    def test_137_icu_members_flagged(self):
        context = self._parse()
        self.assertEqual(context.icu_member_count, 137)
        george = self._find(context, "GEORGE AKOTO")
        self.assertFalse(george.icu_member)

    def test_basic_1800_parses(self):
        context = self._parse()
        george = self._find(context, "GEORGE AKOTO")
        self.assertAlmostEqual(george.basic_salary, 1800.0, delta=0.001)
        self.assertFalse(george.is_hourly)  # salaried admin, no rate table
        self.assertEqual(george.rates, [])

    def test_rate_categories_correct(self):
        """An hourly worker's rate table is tagged basic / overtime / allowance;
        Richard Woode (DCL9) is the shared regression fixture."""
        context = self._parse()
        woode = self._find(context, "RICHARD WOODE")
        self.assertTrue(woode.is_hourly)
        by_code = {r.pay_code: r for r in woode.rates}
        self.assertEqual(by_code["NORMAL"].category, WageRateProfile.CATEGORY_BASIC)
        self.assertEqual(
            by_code["WEEKDAY_OT"].category, WageRateProfile.CATEGORY_OVERTIME
        )
        self.assertEqual(
            by_code["SAT_OT"].category, WageRateProfile.CATEGORY_OVERTIME
        )
        self.assertEqual(
            by_code["AFTERNOON"].category, WageRateProfile.CATEGORY_ALLOWANCE
        )
        self.assertEqual(
            by_code["NIGHT"].category, WageRateProfile.CATEGORY_ALLOWANCE
        )
        # NORMAL rate = daily / 8 = 126.27 / 8 = 15.78375 (verified cell N16).
        self.assertAlmostEqual(by_code["NORMAL"].hourly_rate, 15.78375, delta=1e-5)

    # ---- persistence -----------------------------------------------------

    def test_confirm_persists_basic_1800(self):
        context = self._parse()
        result = persist_seed(context, preserve=False)
        self.assertEqual(result.employees_created, 181)
        self.assertEqual(result.icu_members, 137)

        george = Employee.query.filter_by(
            client_company_id=self.client.id, full_name="GEORGE AKOTO"
        ).one()
        self.assertAlmostEqual(george.basic_salary, 1800.0, delta=0.001)
        self.assertFalse(george.icu_member)

        # Company is now seeded; per-employee rate rows exist for hourly staff.
        self.assertTrue(company_is_seeded(self.client.id))
        woode = Employee.query.filter_by(
            client_company_id=self.client.id, full_name="RICHARD WOODE"
        ).one()
        codes = {
            p.pay_code
            for p in WageRateProfile.query.filter_by(employee_id=woode.id)
        }
        self.assertIn("NORMAL", codes)

    def test_confirm_is_transactional(self):
        """A forced mid-seed failure writes zero rows (single-transaction confirm)."""
        context = self._parse()
        real = store_mod._upsert_employee
        state = {"n": 0}

        def boom(*args, **kwargs):
            state["n"] += 1
            if state["n"] >= 5:  # fail partway through the 181 employees
                raise RuntimeError("forced mid-seed failure")
            return real(*args, **kwargs)

        with mock.patch.object(store_mod, "_upsert_employee", side_effect=boom):
            with self.assertRaises(RuntimeError):
                persist_seed(context, preserve=False)

        self.assertEqual(
            Employee.query.filter_by(client_company_id=self.client.id).count(), 0
        )
        self.assertEqual(
            WageRateProfile.query.filter_by(client_company_id=self.client.id).count(),
            0,
        )

    def test_reseed_is_idempotent(self):
        """Re-seeding the same workbook updates in place — no duplicate
        employees and no duplicate rate rows (uq_wage_rate_scope_code)."""
        persist_seed(self._parse(), preserve=False)
        first_emps = Employee.query.filter_by(client_company_id=self.client.id).count()
        first_rates = WageRateProfile.query.filter_by(
            client_company_id=self.client.id
        ).count()

        result = persist_seed(self._parse(), preserve=False)  # re-seed
        self.assertEqual(result.employees_created, 0)
        self.assertEqual(result.employees_updated, 181)

        self.assertEqual(
            Employee.query.filter_by(client_company_id=self.client.id).count(),
            first_emps,
        )
        self.assertEqual(
            WageRateProfile.query.filter_by(
                client_company_id=self.client.id
            ).count(),
            first_rates,
        )

    # ---- detection / routing --------------------------------------------

    def test_detects_rich_workbook_and_rejects_thin(self):
        self.assertTrue(is_rich_raw_data(DZ_FIXTURE))
        if os.path.exists(THIN_FIXTURE):
            self.assertFalse(is_rich_raw_data(THIN_FIXTURE))

    def test_unseeded_company_before_seed(self):
        self.assertFalse(company_is_seeded(self.client.id))

    def test_wrong_layout_fails_loud(self):
        """A workbook without the DZ RAW DATA layout raises HeaderError rather
        than silently reading the wrong columns."""
        if not os.path.exists(THIN_FIXTURE):
            self.skipTest("thin consolidation specimen missing")
        with self.assertRaises(HeaderError):
            parse_rich_workbook(THIN_FIXTURE, self.client.id)

    # ---- helpers ---------------------------------------------------------

    def _find(self, context, name):
        for emp in context.employees:
            if emp.full_name == name:
                return emp
        self.fail(f"{name} not found in parsed context")


if __name__ == "__main__":
    unittest.main()
