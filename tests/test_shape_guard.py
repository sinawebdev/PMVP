"""Shape Guard — structural workbook detection that stops the wrong importer.

The Book1.xlsx incident: a Raw Hours workbook uploaded through the Standard
importer produced 163 invalid rows. These tests pin the fix — a lightweight,
structural pre-check (``app.raw_engine.detection.classify_workbook``) shared by
both upload paths — at two levels:

  * unit: classification of synthesised Standard / Raw / thin / junk workbooks,
    including the false-positive trap where a *standard* sheet is literally
    named "RAW DATA" and has a NAMES header (is_rich_raw_data alone would
    misfire; the hour-category signal must not);
  * web: the Standard route refuses a Raw Hours file and the Raw route refuses
    a Standard Payroll file, each pointing the user at the correct tab, while a
    valid standard workbook is never misfired.

Self-contained (synthesised workbooks, no decrypted DZ fixture) so it runs in
every environment.
"""
import io
import os
import tempfile
import unittest

os.environ["DATABASE_URL"] = "sqlite:///:memory:"

from openpyxl import Workbook

from app import create_app, db
from app.models import ClientCompany
from app.raw_engine.detection import (
    RAW_HOURS,
    STANDARD_PAYROLL,
    UNKNOWN,
    classify_workbook,
    looks_like_raw_hours,
    looks_like_standard_payroll,
)


def _write(directory, name, rows, title="Sheet1"):
    """Write ``rows`` (list of lists) to a one-sheet workbook and return its path."""
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = title
    for r, row in enumerate(rows, start=1):
        for c, value in enumerate(row, start=1):
            if value not in (None, ""):
                sheet.cell(row=r, column=c, value=value)
    path = os.path.join(directory, name)
    workbook.save(path)
    return path


# Canonical shapes reused by both the unit and web tests.
def _standard_rows():
    """A minimal Standard Payroll sheet: mandatory input columns present."""
    return [
        ["Staff ID", "Employee Name", "Basic Salary", "SSNIT", "PAYE", "Gross Pay"],
        ["AC605", "Sampson Kluvie", 2737.37, 137.0, 382.63, 3000.0],
        ["AC636", "David Tetteh", 1675.14, 83.76, 477.61, 2100.0],
    ]


def _acs_stacked_rows():
    """An ACS-style stacked-header standard sheet — deliberately the trap case:
    the sheet is named 'RAW DATA' and column B row is 'NAMES', so
    is_rich_raw_data() returns True, but there are no hour-category headers."""
    return [
        ["ACS/GMT SHIPPING"],
        ["PAYROLL JANUARY 2026"],
        [], [], [], [], [],
        [None, None, "EARNINGS"],
        ["IRS/NO.", "NAMES", "BASIC", "OVERTIME", "BASIC", "TOTAL"],
        [None, None, "SALARY", "ALLOW", "TAX", "TAX"],
        [],
        ["AC605", "Sampson K. Kluvie", 2737.37, 150.0, 382.63, 382.63],
        ["AC636", "David Kwame Tetteh", 1675.14, 3125.30, 477.61, 477.61],
    ]


def _thin_rows():
    """A monthly thin template: element display labels + adjustment columns."""
    return [
        ["Staff ID", "Name", "Normal hours", "Weekday overtime", "Saturday overtime",
         "Sun/Holiday overtime", "Afternoon allowance", "Night allowance",
         "6to6 day allowance", "Prod/Bonus Allowance", "Loan", "ICU Member"],
        ["DZ048", "GEORGE AKOTO", 40, 0, 0, 0, 0, 0, 0, 0, 0, "Member"],
    ]


def _rich_rows():
    """A rich RAW DATA seed sheet: hour-category element-label row + NAMES."""
    return [
        ["DZVANS COMPANY LIMITED"], [], [], [], [], [], [], [], [],
        [None, None, None, "NORMAL", "WEEK DAY", "SATURDAY", "SUN / HOL",
         "AFT'NOON", "NIGHT", "6 TO 6", "4CREW"],
        [],
        [None, "NAMES"],
        ["DZ048", "GEORGE AKOTO", None, 40, 0, 0, 0, 0, 0, 0, 0],
    ]


class ShapeGuardClassifyTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.dir = self._dir.name

    def tearDown(self):
        self._dir.cleanup()

    def test_minimal_standard_is_standard(self):
        path = _write(self.dir, "std.xlsx", _standard_rows())
        self.assertEqual(classify_workbook(path), STANDARD_PAYROLL)
        self.assertTrue(looks_like_standard_payroll(path))
        self.assertFalse(looks_like_raw_hours(path))

    def test_acs_named_raw_data_is_still_standard_not_raw(self):
        # The trap: 'RAW DATA' sheet + 'NAMES' header would fool is_rich_raw_data,
        # but with no hour-category headers it must classify as STANDARD.
        path = _write(self.dir, "acs.xlsx", _acs_stacked_rows(), title="RAW DATA")
        self.assertFalse(looks_like_raw_hours(path))
        self.assertEqual(classify_workbook(path), STANDARD_PAYROLL)

    def test_rich_raw_workbook_is_raw(self):
        path = _write(self.dir, "rich.xlsx", _rich_rows(), title="RAW DATA")
        self.assertTrue(looks_like_raw_hours(path))
        self.assertEqual(classify_workbook(path), RAW_HOURS)

    def test_thin_template_is_raw(self):
        path = _write(self.dir, "thin.xlsx", _thin_rows(), title="MONTHLY TEMPLATE")
        self.assertTrue(looks_like_raw_hours(path))
        self.assertEqual(classify_workbook(path), RAW_HOURS)

    def test_unrelated_workbook_is_unknown(self):
        path = _write(self.dir, "junk.xlsx", [["Colour", "Qty", "Notes"], ["Red", 3, "x"]])
        self.assertEqual(classify_workbook(path), UNKNOWN)

    def test_missing_or_unopenable_file_is_unknown(self):
        # A path openpyxl cannot read yields no signal, never an exception.
        bad = os.path.join(self.dir, "not_a_workbook.xlsx")
        with open(bad, "wb") as handle:
            handle.write(b"not a real xlsx")
        self.assertFalse(looks_like_raw_hours(bad))
        self.assertEqual(classify_workbook(bad), UNKNOWN)


class ShapeGuardWebTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app()
        self.app.config["TESTING"] = True
        self.http = self.app.test_client()
        self._dir = tempfile.TemporaryDirectory()
        self.dir = self._dir.name
        with self.app.app_context():
            client = ClientCompany(name="SHAPE GUARD CO", status="Active")
            db.session.add(client)
            db.session.commit()
            self.cid = client.id
        self.http.post(
            "/login", data={"email": "admin@chrisnat.local", "password": "password123"}
        )

    def tearDown(self):
        self._dir.cleanup()

    def _bytes(self, rows, title="Sheet1"):
        path = _write(self.dir, "wb.xlsx", rows, title=title)
        with open(path, "rb") as handle:
            return handle.read()

    def test_standard_route_blocks_raw_hours_workbook(self):
        # A Raw Hours workbook uploaded through Standard Upload must stop before
        # any parsing and bounce to the Raw Hour tab — never 163 invalid rows.
        resp = self.http.post(
            "/payroll/runs",
            data={
                "import_mode": "single_client",
                "client_company_id": str(self.cid),
                "month": "January",
                "year": "2026",
                "payroll_file": (io.BytesIO(self._bytes(_thin_rows())), "Book1.xlsx"),
            },
            content_type="multipart/form-data",
        )
        self.assertEqual(resp.status_code, 302)
        self.assertIn("wrong_tab=raw", resp.headers.get("Location", ""))

        followed = self.http.get("/payroll/runs?wrong_tab=raw")
        body = followed.get_data(as_text=True)
        # The alert splits "Raw Hours" into a <strong>, so assert on contiguous
        # rendered fragments: the guidance text and the switch-tab button.
        self.assertIn("Please upload it using", body)
        self.assertIn("Go to Raw Hour Upload", body)

    def test_standard_route_allows_a_standard_workbook(self):
        # A genuine standard workbook must NOT be misfired by the guard.
        resp = self.http.post(
            "/payroll/runs",
            data={
                "import_mode": "single_client",
                "client_company_id": str(self.cid),
                "month": "January",
                "year": "2026",
                "payroll_file": (io.BytesIO(self._bytes(_standard_rows())), "payroll.xlsx"),
            },
            content_type="multipart/form-data",
        )
        # It may redirect to preview or flash a parse error, but it must never be
        # the Shape-Guard redirect.
        self.assertNotIn("wrong_tab=raw", resp.headers.get("Location", ""))

    def test_raw_route_blocks_standard_payroll_workbook(self):
        # A Standard Payroll workbook uploaded through Raw Hour Upload must be
        # refused with a pointer back to Standard Upload.
        resp = self.http.post(
            "/raw/upload",
            data={
                "client_company_id": str(self.cid),
                "month": "January",
                "year": "2026",
                "file": (io.BytesIO(self._bytes(_standard_rows())), "payroll.xlsx"),
            },
            content_type="multipart/form-data",
        )
        self.assertEqual(resp.status_code, 422)
        payload = resp.get_json()
        self.assertEqual(payload["wrong_tab"], "standard")
        self.assertIn("Standard Payroll workbook", payload["error"])


if __name__ == "__main__":
    unittest.main()
