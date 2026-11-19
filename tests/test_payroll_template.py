"""The standard payroll upload template.

Someone who signs up cold needs to know what file the standard upload reads. The
template is offered on both upload pages, and a filled-in copy has to go
through the real importer with every column mapped.
"""

import io
import os
import tempfile
import unittest

os.environ["SKIP_DOTENV"] = "true"
os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["PERSISTENCE_REQUIRED"] = "false"

from openpyxl import load_workbook  # noqa: E402

from app import create_app, db  # noqa: E402
from app.models import Bureau, ClientCompany, User  # noqa: E402
from app.payroll_template import PAYROLL_TEMPLATE_COLUMNS, TEMPLATE_FILENAME, build_template  # noqa: E402
from app.roles import BUREAU_ADMIN  # noqa: E402

XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


class PayrollTemplateTestCase(unittest.TestCase):
    def setUp(self):
        self.app = create_app()
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()
        self.ctx = self.app.app_context()
        self.ctx.push()

    def tearDown(self):
        db.session.remove()
        self.ctx.pop()

    def _login(self, email, password="password123"):
        self.client.get("/logout")
        resp = self.client.post("/login", data={"email": email, "password": password})
        self.assertEqual(resp.status_code, 302)

    def _assert_template(self, resp):
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.mimetype, XLSX)
        self.assertIn(TEMPLATE_FILENAME, resp.headers.get("Content-Disposition", ""))
        book = load_workbook(io.BytesIO(resp.data))
        self.assertEqual(book.sheetnames, ["Payroll", "Instructions"])
        headers = [cell.value for cell in book["Payroll"][1]]
        self.assertEqual(headers, [header for header, _ in PAYROLL_TEMPLATE_COLUMNS])
        # No example row to import by mistake (the rows below are only text-formatted).
        filled = [row for row in book["Payroll"].iter_rows(min_row=2, values_only=True) if any(row)]
        self.assertEqual(filled, [])

    def test_operator_bureau_and_company_users_can_download_it(self):
        self._login("admin@payrolla.com")
        self.assertIn("/payroll/runs/template", self.client.get("/payroll/runs/new").get_data(as_text=True))
        self._assert_template(self.client.get("/payroll/runs/template"))

        bureau = Bureau(name="Delta Payroll Services", code="DPS")
        db.session.add(bureau)
        db.session.flush()
        staff = User(name="Dede", email="dede@delta.example", role=BUREAU_ADMIN, bureau_id=bureau.id)
        staff.set_password("bureau-pass-1")
        db.session.add(staff)
        db.session.commit()
        self._login("dede@delta.example", "bureau-pass-1")
        self._assert_template(self.client.get("/payroll/runs/template"))

        self._login("admin@contoso.com")
        self.assertIn("/company/runs/template", self.client.get("/company/runs/upload").get_data(as_text=True))
        self._assert_template(self.client.get("/company/runs/template"))
        self.assertNotEqual(self.client.get("/payroll/runs/template").status_code, 200)

    def test_a_filled_template_imports_with_every_column_mapped(self):
        from app.payroll import build_single_payload

        book = load_workbook(build_template())
        sheet = book["Payroll"]
        rows = [
            ["T001", "Ama Owusu", "Finance", "Accountant", "C012345678901", "GHA-123456789-0",
             "GCB Bank", "Koforidua", "1020300045001", "0241234567", "", 4500, 300, 200, 0, 0, 0, 0],
            ["T002", "Kofi Mensah", "Operations", "Driver", "", "", "", "", "", "", "", 2500,
             None, None, None, None, None, None],
        ]
        # Typed straight under the header, as someone filling it in Excel would.
        for row_index, values in enumerate(rows, start=2):
            for col_index, value in enumerate(values, start=1):
                sheet.cell(row=row_index, column=col_index, value=value)
        handle, path = tempfile.mkstemp(suffix=".xlsx")
        os.close(handle)
        try:
            book.save(path)
            contoso = ClientCompany.query.filter_by(company_code="CONTOSO").one()
            payload, error = build_single_payload(path, "filled.xlsx", contoso, "November", 2026)
        finally:
            os.remove(path)
        self.assertIsNone(error)
        self.assertEqual(payload["matched_sheet_name"], "Payroll")
        self.assertEqual(payload["unmapped_columns"], [])
        self.assertEqual(payload["mapping_errors"], [])
        rows = payload["mapped_rows"]
        self.assertEqual([row["staff_id"] for row in rows], ["T001", "T002"])
        self.assertEqual([row["full_name"] for row in rows], ["Ama Owusu", "Kofi Mensah"])
        self.assertEqual([float(row["basic_salary"]) for row in rows], [4500.0, 2500.0])


if __name__ == "__main__":
    unittest.main()
