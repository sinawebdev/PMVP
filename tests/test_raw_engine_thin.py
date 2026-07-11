"""GATE 3 — Raw Hours Engine thin monthly upload.

A thin file (hours + this month's adjustments) joined to seeded context must
reproduce the Phase 2 payroll exactly; blank cells cost as 0; an unknown Staff
ID is blocked with a clear message; and a raise takes effect only after a rich
re-upload (there is no rate column on the thin file), never from a thin column.
"""
import os
import tempfile
import unittest
from datetime import date

os.environ["DATABASE_URL"] = "sqlite:///:memory:"

from app import create_app, db
from app.models import (
    ClientCompany,
    Employee,
    PayrollRun,
    StatutoryRate,
    WageRateProfile,
)
from app.raw_engine.run import compute_seed_month
from app.raw_engine.seed import parse_rich_workbook
from app.raw_engine.store import persist_seed
from app.raw_engine.thin import (
    ThinEmployeeInput,
    ThinFormatError,
    join_and_compute,
    parse_thin_workbook,
    thin_header,
    write_thin_workbook,
)

FIXTURE = os.path.join(
    os.path.dirname(__file__), "fixtures", "DZ-PAYROLL JAN 2026.xlsx"
)


@unittest.skipUnless(
    os.path.exists(FIXTURE),
    "Decrypted DZ specimen missing — see tests/test_raw_engine_seed.py.",
)
class RawEngineThinTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app()
        self.app.config["TESTING"] = True
        self.ctx = self.app.app_context()
        self.ctx.push()
        self.client = ClientCompany(name="DZVANS COMPANY LIMITED", status="Active")
        db.session.add(self.client)
        db.session.commit()
        self.context = parse_rich_workbook(FIXTURE, self.client.id)
        persist_seed(self.context, preserve=False)  # seed the company
        self.rate = StatutoryRate.active_for(date(2026, 1, 1))
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        db.session.rollback()
        self.ctx.pop()

    def _run(self):
        run = PayrollRun(
            month="January", year=2026, status="Draft",
            upload_type="raw", client_company_id=self.client.id,
        )
        db.session.add(run)
        db.session.commit()
        return run

    def _thin_from_seed(self, employees=None):
        """Write a thin workbook carrying each seed employee's hours + the
        thin-supported adjustments, and return its path."""
        records = [
            ThinEmployeeInput(
                staff_id=e.staff_id, name=e.full_name, hours=dict(e.raw_hours),
                bonus=e.bonus, loan=e.loan, welfare=e.welfare,
                other_deduction=e.other_deduction, pay_difference=e.pay_difference,
            )
            for e in (employees or self.context.employees)
        ]
        path = os.path.join(self.tmp, "thin.xlsx")
        return write_thin_workbook(path, records)

    # ---- thin reproduces Phase 2 ----------------------------------------

    def test_thin_matches_phase2_for_seed_month(self):
        """Thin file over the seeded company == the Phase 2 seed-month payroll,
        per employee (DZ has no provident/donations/other-allowance, which the
        thin template omits, so the two are identical)."""
        phase2 = compute_seed_month(self.context, self.rate)
        inputs, _warn = parse_thin_workbook(self._thin_from_seed())
        result = join_and_compute(inputs, self._run(), self.rate)

        self.assertEqual(result.blocked, [])
        self.assertEqual(len(result.payslips), 181)
        for staff_id, slip2 in phase2.items():
            with self.subTest(staff=staff_id):
                slip3 = result.payslips[staff_id]
                self.assertAlmostEqual(slip3.gross_pay, slip2.gross_pay, delta=0.001)
                self.assertAlmostEqual(slip3.ordinary_paye, slip2.ordinary_paye, delta=0.001)
                self.assertAlmostEqual(slip3.icu, slip2.icu, delta=0.001)
                self.assertAlmostEqual(slip3.net_pay, slip2.net_pay, delta=0.001)

    # ---- blank = 0, no carry-forward ------------------------------------

    def test_blank_cells_cost_as_zero(self):
        """George Akoto with blank hours and blank adjustments: gross = basic
        1800, no bonus, and no welfare carried over from the seed month."""
        george = Employee.query.filter_by(
            client_company_id=self.client.id, full_name="GEORGE AKOTO"
        ).one()
        rec = ThinEmployeeInput(staff_id=george.staff_id, name="GEORGE AKOTO")
        path = os.path.join(self.tmp, "blank.xlsx")
        write_thin_workbook(path, [rec])
        inputs, _ = parse_thin_workbook(path)
        result = join_and_compute(inputs, self._run(), self.rate)

        slip = result.payslips[george.staff_id]
        self.assertAlmostEqual(slip.basic_wage, 1800.0, delta=0.001)
        self.assertAlmostEqual(slip.gross_pay, 1800.0, delta=0.001)
        self.assertEqual(slip.bonus, 0.0)
        self.assertEqual(slip.welfare, 0.0)   # not carried from the seed month's 100
        self.assertEqual(slip.overtime_pay, 0.0)

    # ---- unknown staff ID blocked ---------------------------------------

    def test_unknown_staff_id_is_blocked(self):
        rec = ThinEmployeeInput(staff_id="GHOST99", name="Nobody Here",
                                hours={"NORMAL": 40})
        path = os.path.join(self.tmp, "ghost.xlsx")
        write_thin_workbook(path, [rec])
        inputs, _ = parse_thin_workbook(path)
        result = join_and_compute(inputs, self._run(), self.rate)

        self.assertNotIn("GHOST99", result.payslips)
        self.assertEqual(len(result.blocked), 1)
        blocked = result.blocked[0]
        self.assertEqual(blocked["staff_id"], "GHOST99")
        self.assertIn("rich", blocked["reason"].lower())
        self.assertIn("seed", blocked["reason"].lower())

    # ---- raise needs a rich re-upload -----------------------------------

    def test_raise_only_via_rich_reupload_never_thin_column(self):
        """The thin template carries no rate column, so a client cannot raise a
        rate through it; a raise takes effect only when the stored
        WageRateProfile changes (a rich re-upload)."""
        # No rate/basic column exists on the thin template.
        header = {h.upper() for h in thin_header()}
        self.assertNotIn("RATE", header)
        self.assertNotIn("BASIC WAGE", header)
        self.assertNotIn("DAILY RATE", header)

        woode = Employee.query.filter_by(
            client_company_id=self.client.id, full_name="RICHARD WOODE"
        ).one()
        rec = ThinEmployeeInput(staff_id=woode.staff_id, hours={"NORMAL": 40})
        path = os.path.join(self.tmp, "woode.xlsx")
        write_thin_workbook(path, [rec])
        inputs, _ = parse_thin_workbook(path)

        before = join_and_compute(inputs, self._run(), self.rate).payslips[
            woode.staff_id
        ].basic_wage

        # A rich re-upload gives a 10% raise: the stored NORMAL rate changes.
        normal = WageRateProfile.query.filter_by(
            employee_id=woode.id, pay_code="NORMAL"
        ).one()
        normal.hourly_rate = round(normal.hourly_rate * 1.10, 6)
        db.session.commit()

        after = join_and_compute(inputs, self._run(), self.rate).payslips[
            woode.staff_id
        ].basic_wage
        self.assertAlmostEqual(after, before * 1.10, delta=0.02)
        self.assertGreater(after, before)

    # ---- format guard ----------------------------------------------------

    def test_missing_staff_id_column_fails_loud(self):
        import openpyxl

        wb = openpyxl.Workbook()
        ws = wb.active
        ws.append(["Name", "Normal hours", "Loan"])  # no Staff ID column
        ws.append(["Somebody", 40, 0])
        path = os.path.join(self.tmp, "noid.xlsx")
        wb.save(path)
        with self.assertRaises(ThinFormatError):
            parse_thin_workbook(path)


if __name__ == "__main__":
    unittest.main()
