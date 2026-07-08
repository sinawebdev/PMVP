"""Phase 2 of the compute-engine brief: scalable header mapping.

The acs 1.xlsx mis-parse had two ingredients: a 3-row stacked header keyed on
one row (so "BASIC" matched both the salary and the tax column), and a row
reader that silently last-wins when two columns claim the same field. These
tests pin the fixes: composite headers anchored on the staff-ID data row, and
strict uniqueness/mandatory enforcement that refuses to guess.
"""
import os
import tempfile
import unittest

from openpyxl import Workbook

from app.excel_utils import (
    extract_payroll_sheet,
    map_columns,
    mapping_conflicts,
)


def write_workbook(directory, filename, rows, sheet_title="RAW DATA"):
    """rows: list of lists, one per Excel row (None cells stay empty)."""
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = sheet_title
    for row_index, row in enumerate(rows, start=1):
        for col_index, value in enumerate(row, start=1):
            if value not in (None, ""):
                sheet.cell(row=row_index, column=col_index, value=value)
    path = os.path.join(directory, filename)
    workbook.save(path)
    return path


class StackedHeaderTestCase(unittest.TestCase):
    """ACS-shaped sheet: group row, label row, sub-label row, blank row, then
    data anchored by staff IDs. Keyed on one row, "BASIC" is ambiguous; the
    composite header makes BASIC SALARY and BASIC TAX distinct."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.temp_dir.cleanup()

    def _acs_shaped_path(self):
        return write_workbook(
            self.temp_dir.name,
            "acs_shaped.xlsx",
            [
                ["ACS/GMT SHIPPING"],                                   # 1 title
                ["PAYROLL JANUARY 2026"],                               # 2
                [], [], [], [], [],                                     # 3-7
                [None, None, "EARNINGS"],                               # 8 group
                ["IRS/NO.", "NAMES", "BASIC", "OVERTIME", "BASIC", "TOTAL"],  # 9 label
                [None, None, "SALARY", "ALLOW", "TAX", "TAX"],          # 10 sub-label
                [],                                                     # 11 blank
                ["AC605", "Sampson K. Kluvie", 2737.37, 150.00, 382.63, 382.63],  # 12
                ["AC636", "David Kwame Tetteh", 1675.14, 3125.30, 477.61, 477.61],  # 13
            ],
        )

    def test_composite_header_separates_basic_salary_from_basic_tax(self):
        extraction = extract_payroll_sheet(self._acs_shaped_path())

        self.assertEqual(extraction["detected_header_row"], 9)
        self.assertEqual(extraction["data_start_row"], 12)
        self.assertEqual(extraction["mapping"]["BASIC SALARY"], "basic_salary")
        # The tax columns are engine outputs: never mapped, never read.
        self.assertEqual(extraction["mapping"]["BASIC TAX"], "unmapped")
        self.assertEqual(extraction["mapping"]["TOTAL TAX"], "unmapped")
        self.assertEqual(extraction["mapping"]["OVERTIME ALLOW"], "overtime_pay")
        self.assertEqual(extraction["mapping_errors"], [])

        rows = extraction["mapped_rows"]
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["staff_id"], "AC605")
        # THE regression: basic must come from column C, not the tax column.
        self.assertEqual(rows[0]["basic_salary"], 2737.37)
        self.assertEqual(rows[1]["basic_salary"], 1675.14)

    def test_flat_sheet_without_staff_id_anchor_is_unchanged(self):
        """Legacy sheets (numeric staff numbers, single header row) must keep
        the exact pre-Phase-2 behavior: data starts right below the header."""
        path = write_workbook(
            self.temp_dir.name,
            "flat.xlsx",
            [
                ["Staff No", "Employee Name", "Basic Salary", "Take Home"],
                ["001", "Kofi Mensah", 1000, 900],
                ["002", "Ama Serwaa", 1200, 1050],
            ],
        )
        extraction = extract_payroll_sheet(path)

        self.assertEqual(extraction["detected_header_row"], 1)
        self.assertEqual(extraction["data_start_row"], 2)
        self.assertEqual(extraction["mapping"]["Basic Salary"], "basic_salary")
        self.assertEqual(extraction["mapping_errors"], [])
        rows = extraction["mapped_rows"]
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["staff_id"], "001")
        self.assertEqual(rows[0]["basic_salary"], 1000.0)


class MappingUniquenessTestCase(unittest.TestCase):
    """One field, one column. A mapping that binds a field to several columns
    (or leaves a mandatory field unbound) must be refused with the specific
    collision — silent last-wins is what corrupted run 9."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_duplicate_labels_are_refused_never_last_wins(self):
        path = write_workbook(
            self.temp_dir.name,
            "duplicates.xlsx",
            [
                ["STAFF ID", "NAME", "BASIC", "BASIC", "TAX", "TAX", "TAX",
                 "TAX", "SSNIT", "SSNIT"],
                ["AC001", "Ama Serwaa", 1000, 999, 50, 50, 50, 50, 55, 55],
            ],
        )
        extraction = extract_payroll_sheet(path)

        # Duplicate columns stay distinct (suffixed), so the conflict is
        # visible instead of pandas-mangled and silently clobbered.
        self.assertIn("BASIC", extraction["mapping"])
        self.assertIn("BASIC (2)", extraction["mapping"])
        errors = extraction["mapping_errors"]
        self.assertEqual(len(errors), 3)  # basic_salary, paye, ssnit
        joined = " ".join(errors)
        self.assertIn("basic_salary", joined)
        self.assertIn("paye", joined)
        self.assertIn("ssnit", joined)
        self.assertIn("refusing to guess", joined)
        self.assertIn("4 columns", joined)  # the TAX x4 collision

    def test_mandatory_fields_must_be_mapped(self):
        errors = mapping_conflicts(
            map_columns(["STAFF ID", "NAME", "GROSS PAY"])
        )
        self.assertEqual(len(errors), 1)
        self.assertIn("basic_salary", errors[0])
        self.assertIn("Mandatory", errors[0])

        # Nothing mapped at all: every mandatory field is reported.
        errors = mapping_conflicts({})
        self.assertEqual(len(errors), 3)
        joined = " ".join(errors)
        for field in ("staff_id", "full_name", "basic_salary"):
            self.assertIn(field, joined)

    def test_clean_mapping_produces_no_errors(self):
        mapping = map_columns(
            ["Staff No", "Employee Name", "Basic Salary", "Transport",
             "Meal Allowance", "Welfare Supplies", "IOU Deduction"]
        )
        self.assertEqual(mapping_conflicts(mapping), [])


if __name__ == "__main__":
    unittest.main()
