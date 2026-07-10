"""GATE 2 — Raw Hours Engine compute layer.

Two kinds of coverage:
  * Per-rule unit tests with hand-checked cases (SSNIT, PAYE, overtime tax,
    bonus tax, ICU).
  * Cent reconciliation: recompute the seeded DZ month and tie every sampled
    employee's gross / ordinary PAYE / net back to the workbook's own computed
    columns (AQ / AR / BL), spanning a salaried non-member (George Akoto) and
    hourly union members across rate profiles.

Tolerance note: the DZ workbook carries full-precision internals in every cell,
while the engine rounds each statutory component to 2dp (the money discipline
shared with the standard engine and its StatutoryRate primitives). Agreement is
therefore within a pesewa or two — PAYE within 0.01, gross/net within 0.02 —
which is what "to the cent" means against an unrounded source (the existing
Richard Woode fixture already needs a 0.011 gross tolerance for the same reason).
"""
import os
import unittest
from datetime import date

os.environ["DATABASE_URL"] = "sqlite:///:memory:"

import openpyxl

from app import create_app, db
from app.models import ClientCompany, Employee, PayrollItem, PayrollRun, StatutoryRate
from app.raw_engine.calc import (
    bonus_split,
    employee_ssnit,
    employer_ssnit,
    icu_dues,
    ordinary_paye,
    overtime_tax,
)
from app.raw_engine.compute import PayslipInputs, compute_payslip
from app.raw_engine.run import compute_seed_month
from app.raw_engine.seed import parse_rich_workbook
from app.raw_engine.store import persist_seed, write_payroll_items

FIXTURE = os.path.join(
    os.path.dirname(__file__), "fixtures", "DZ-PAYROLL JAN 2026.xlsx"
)

# Sample rows spanning the profiles, keyed by name -> (workbook row, is_member).
SAMPLE = {
    "GEORGE AKOTO": 13,       # salaried admin, non-member, basic 1800
    "RICHARD WOODE": 16,      # hourly member, OT over threshold + bonus
    "SAMUEL OTCHERE": 17,     # hourly member, larger basic
    "CHRISTOPHER GBEDZO": 18, # hourly member
    "SAMUEL AMANKRAH": 21,    # hourly member, tiny basic, OT >> basic
}
COL_AQ_GROSS, COL_AR_PAYE, COL_BL_NET = 43, 44, 64


class RawEngineComputeUnitTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app()
        self.app.config["TESTING"] = True
        self.ctx = self.app.app_context()
        self.ctx.push()

    def tearDown(self):
        db.session.rollback()
        self.ctx.pop()

    def _rate(self):
        rate = StatutoryRate.active_for(date(2026, 1, 1))
        self.assertIsNotNone(rate)
        return rate

    def test_ssnit_split(self):
        rate = self._rate()
        self.assertAlmostEqual(employee_ssnit(rate, 1000), 55.00, delta=0.001)
        self.assertAlmostEqual(employer_ssnit(rate, 1000), 130.00, delta=0.001)

    def test_ordinary_paye_hand_checked(self):
        # taxable 1701 -> (1701-730)*17.5% + 18.5 = 188.425
        self.assertAlmostEqual(
            ordinary_paye(self._rate(), 1701), 188.43, delta=0.01
        )

    def test_overtime_tax_concession(self):
        # OT 978.59 on basic 631.35: half-basic 315.675 @5% + excess 662.915 @10%
        self.assertAlmostEqual(
            overtime_tax(self._rate(), 978.59, 631.35), 82.08, delta=0.01
        )

    def test_bonus_within_cap_is_flat(self):
        tax, excess = bonus_split(self._rate(), 120.97, 631.35)
        self.assertAlmostEqual(tax, 6.05, delta=0.01)     # 120.97 * 5%
        self.assertEqual(excess, 0.0)

    def test_icu_member_vs_nonmember(self):
        rate = self._rate()
        self.assertAlmostEqual(icu_dues(rate, 631.35, True), 18.94, delta=0.01)
        self.assertEqual(icu_dues(rate, 631.35, False), 0.0)

    def test_compute_payslip_salaried_nonmember(self):
        """George Akoto rebuilt from inputs: gross 1800, PAYE 188.43, ICU 0,
        net 1412.57 (welfare 100)."""
        slip = compute_payslip(
            PayslipInputs(staff_id="1", basic_wage=1800.0, welfare=100.0),
            self._rate(),
        )
        self.assertAlmostEqual(slip.gross_pay, 1800.00, delta=0.001)
        self.assertAlmostEqual(slip.ordinary_paye, 188.43, delta=0.01)
        self.assertEqual(slip.icu, 0.0)
        self.assertAlmostEqual(slip.net_pay, 1412.57, delta=0.01)
        # gross - total_deductions == net, exactly.
        self.assertAlmostEqual(
            slip.gross_pay - slip.total_deductions, slip.net_pay, delta=0.001
        )

    def test_icu_only_for_members_in_full_payslip(self):
        member = compute_payslip(
            PayslipInputs(staff_id="M", basic_wage=631.35, is_icu_member=True),
            self._rate(),
        )
        self.assertAlmostEqual(member.icu, 18.94, delta=0.01)


@unittest.skipUnless(
    os.path.exists(FIXTURE),
    "Decrypted DZ specimen missing — see tests/test_raw_engine_seed.py.",
)
class RawEngineReconciliationTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app()
        self.app.config["TESTING"] = True
        self.ctx = self.app.app_context()
        self.ctx.push()
        self.client = ClientCompany(name="DZVANS COMPANY LIMITED", status="Active")
        db.session.add(self.client)
        db.session.commit()
        self.context = parse_rich_workbook(FIXTURE, self.client.id)
        self.rate = StatutoryRate.active_for(date(2026, 1, 1))
        self._ws = openpyxl.load_workbook(FIXTURE, data_only=True)["RAW DATA"]

    def tearDown(self):
        db.session.rollback()
        self.ctx.pop()

    def _staff_id(self, name):
        for emp in self.context.employees:
            if emp.full_name == name:
                return emp.staff_id
        self.fail(f"{name} not parsed")

    def _wb(self, row, col):
        return float(self._ws.cell(row, col).value or 0)

    def test_reconciles_gross_paye_net_to_the_cent(self):
        payslips = compute_seed_month(self.context, self.rate)
        for name, row in SAMPLE.items():
            with self.subTest(employee=name):
                slip = payslips[self._staff_id(name)]
                self.assertAlmostEqual(
                    slip.gross_pay, self._wb(row, COL_AQ_GROSS), delta=0.02,
                    msg=f"{name} gross vs AQ",
                )
                self.assertAlmostEqual(
                    slip.ordinary_paye, self._wb(row, COL_AR_PAYE), delta=0.01,
                    msg=f"{name} ordinary PAYE vs AR",
                )
                self.assertAlmostEqual(
                    slip.net_pay, self._wb(row, COL_BL_NET), delta=0.02,
                    msg=f"{name} net vs BL",
                )

    def test_member_icu_and_nonmember_zero(self):
        payslips = compute_seed_month(self.context, self.rate)
        george = payslips[self._staff_id("GEORGE AKOTO")]
        woode = payslips[self._staff_id("RICHARD WOODE")]
        self.assertEqual(george.icu, 0.0)                 # non-member
        self.assertAlmostEqual(woode.icu, 18.94, delta=0.01)  # 3% of 631.35

    def test_whole_run_persists_to_payroll_items(self):
        """Seed -> compute -> write: 181 PayrollItems, ICU stored, run totals set,
        and net == gross - total_deductions on every row."""
        persist_seed(self.context, preserve=False)
        run = PayrollRun(
            month="January", year=2026, status="Draft",
            upload_type="raw", client_company_id=self.client.id,
        )
        db.session.add(run)
        db.session.commit()

        payslips = compute_seed_month(self.context, self.rate)
        count = write_payroll_items(run, payslips)
        self.assertEqual(count, 181)
        self.assertEqual(
            PayrollItem.query.filter_by(payroll_run_id=run.id).count(), 181
        )

        george = PayrollItem.query.filter_by(
            payroll_run_id=run.id, full_name="GEORGE AKOTO"
        ).one()
        self.assertAlmostEqual(george.basic_salary, 1800.0, delta=0.001)
        self.assertEqual(george.icu_dues, 0.0)
        self.assertAlmostEqual(george.net_pay, self._wb(13, COL_BL_NET), delta=0.02)

        woode = PayrollItem.query.filter_by(
            payroll_run_id=run.id, full_name="RICHARD WOODE"
        ).one()
        self.assertAlmostEqual(woode.icu_dues, 18.94, delta=0.01)

        # Run aggregates populated; net never exceeds gross on any row.
        self.assertGreater(run.total_gross_pay, 0)
        self.assertGreater(run.total_ssnit_employer, 0)
        for item in PayrollItem.query.filter_by(payroll_run_id=run.id):
            self.assertLessEqual(item.net_pay, item.gross_pay + 0.001)
            self.assertAlmostEqual(
                item.gross_pay - item.total_deductions, item.net_pay, delta=0.02
            )

    def test_idempotent_write_replaces(self):
        persist_seed(self.context, preserve=False)
        run = PayrollRun(
            month="January", year=2026, status="Draft",
            upload_type="raw", client_company_id=self.client.id,
        )
        db.session.add(run)
        db.session.commit()
        payslips = compute_seed_month(self.context, self.rate)
        write_payroll_items(run, payslips)
        write_payroll_items(run, payslips)  # re-run
        self.assertEqual(
            PayrollItem.query.filter_by(payroll_run_id=run.id).count(), 181
        )


if __name__ == "__main__":
    unittest.main()
