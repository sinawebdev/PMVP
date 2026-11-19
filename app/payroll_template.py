"""The downloadable template for a standard monthly payroll upload.

Someone who signs up and has never seen Payrolla needs to know what file the
standard upload reads. Every header here is an exact alias in
``excel_utils.COLUMN_ALIASES`` (the parser matches exactly, never by substring),
so a filled-in template maps cleanly. Gross pay, PAYE, SSNIT and net pay are left
out on purpose: Payrolla calculates them.

Shaped like ``roster_import.build_template``: headers only on the first sheet,
no example row (a forgotten example would be imported as a real worker), and the
guidance on a sheet named "Instructions", which the importer skips as a meta
sheet (``excel_utils.META_SHEET_NAMES``).
"""
import io

from openpyxl import Workbook
from openpyxl.styles import Font

# (header, kept as text so Excel doesn't drop leading zeros or round long IDs)
PAYROLL_TEMPLATE_COLUMNS = (
    ("Staff ID", True),
    ("Full Name", True),
    ("Department", True),
    ("Job Title", True),
    ("SSNIT Number", True),
    ("Ghana Card Number", True),
    ("Bank Name", True),
    ("Bank Branch", True),
    ("Account Number", True),
    ("Phone", True),
    ("Email", True),
    ("Basic Salary", False),
    ("Transport Allowance", False),
    ("Housing Allowance", False),
    ("Overtime Pay", False),
    ("Other Allowances", False),
    ("Loan Deduction", False),
    ("Other Deductions", False),
)

MAX_TEXT_ROWS = 2000
TEMPLATE_FILENAME = "payrolla_payroll_upload_template.xlsx"
TEMPLATE_MIMETYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def build_template():
    """An .xlsx stream: a "Payroll" sheet with the headers, and "Instructions"."""
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Payroll"
    sheet.append([header for header, _ in PAYROLL_TEMPLATE_COLUMNS])
    for cell in sheet[1]:
        cell.font = Font(bold=True)
    sheet.freeze_panes = "A2"
    for index, (header, as_text) in enumerate(PAYROLL_TEMPLATE_COLUMNS, start=1):
        letter = sheet.cell(row=1, column=index).column_letter
        sheet.column_dimensions[letter].width = max(14, len(header) + 4)
        if as_text:
            for row in range(2, MAX_TEXT_ROWS + 2):
                sheet.cell(row=row, column=index).number_format = "@"

    notes = workbook.create_sheet("Instructions")
    notes.column_dimensions["A"].width = 24
    notes.column_dimensions["B"].width = 74
    lines = [
        ("Payroll sheet", "One row per worker for the month you upload. Leave the header row as it is."),
        ("Required", "Staff ID, Full Name and Basic Salary. Every other column is optional."),
        ("Amounts", "Monthly, in cedis, numbers only (e.g. 2500 or 2,500.00). Leave a column blank "
                    "if it doesn't apply."),
        ("Calculated for you", "Gross pay, PAYE, SSNIT and net pay. Don't add them; Payrolla works "
                               "them out under the current GRA and SSNIT rates."),
        ("Phone", "A Ghana mobile number, e.g. 024 123 4567. Payslips can be texted to it."),
        ("Next step", "Upload the file, check the preview, then confirm. Nothing is saved until you do."),
    ]
    for label, text in lines:
        notes.append([label, text])
        notes.cell(row=notes.max_row, column=1).font = Font(bold=True)

    stream = io.BytesIO()
    workbook.save(stream)
    stream.seek(0)
    return stream
