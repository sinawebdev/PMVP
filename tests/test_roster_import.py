"""Bulk employee import from one Excel sheet, on both portals.

A client admin uploads a roster on the client portal (or an operator does it
for a client in the console). The rows are sorted into new, updated and skipped,
shown in a preview, and only written on confirm. A staff ID already on the
roster updates that person, and empty cells never blank saved details. A tenant
can only ever import into its own company, whatever the request says.
"""

import os
import unittest
from io import BytesIO

os.environ["SKIP_DOTENV"] = "true"
os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["SEED_DEMO_DATA"] = "true"
os.environ["PERSISTENCE_REQUIRED"] = "false"

from openpyxl import Workbook, load_workbook  # noqa: E402

from app import create_app, db, roster_import  # noqa: E402
from app.models import AuditTrail, ClientCompany, Employee, User  # noqa: E402


def _sheet(rows, headers=("Staff ID", "Full name", "Job title", "Phone", "Email",
                          "Bank account number", "Basic salary (GHS)")):
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(list(headers))
    for row in rows:
        sheet.append(list(row))
    stream = BytesIO()
    workbook.save(stream)
    stream.seek(0)
    return stream


class RosterImportTestCase(unittest.TestCase):
    def setUp(self):
        self.app = create_app()
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()
        self.ctx = self.app.app_context()
        self.ctx.push()
        self.contoso_user = User.query.filter_by(email="admin@contoso.com").first()
        self.contoso_id = self.contoso_user.client_company_id
        self.acme_id = User.query.filter_by(email="admin@acme.com").first().client_company_id

    def tearDown(self):
        db.session.remove()
        self.ctx.pop()

    # --- helpers ------------------------------------------------------------
    def _login(self, email, client=None):
        client = client or self.client
        resp = client.post("/login", data={"email": email, "password": "password123"})
        self.assertEqual(resp.status_code, 302)

    def _upload(self, stream, client=None, filename="roster.xlsx", extra=None):
        client = client or self.client
        data = {"file": (stream, filename)}
        data.update(extra or {})
        return client.post("/company/employees/import", data=data,
                           content_type="multipart/form-data")

    def _emp(self, company_id, staff_id):
        return Employee.query.filter_by(client_company_id=company_id, staff_id=staff_id).first()

    # --- the page and the template -------------------------------------------
    def test_employees_page_offers_bulk_import_next_to_add(self):
        self._login("admin@contoso.com")
        html = self.client.get("/company/employees").get_data(as_text=True)
        self.assertIn("/company/employees/import", html)
        self.assertIn("Bulk import", html)
        self.assertEqual(html.count('class="btn primary"'), 1, "one primary action")

    def test_template_downloads_with_every_column_and_no_example_row(self):
        self._login("admin@contoso.com")
        resp = self.client.get("/company/employees/import/template")
        self.assertEqual(resp.status_code, 200)
        workbook = load_workbook(BytesIO(resp.data))
        sheet = workbook.worksheets[0]
        headers = [c.value for c in sheet[1]]
        self.assertEqual(len(headers), len(roster_import.ROSTER_COLUMNS))
        self.assertEqual(headers[0], "Staff ID *")
        self.assertTrue(
            all(cell.value is None for row in sheet.iter_rows(min_row=2) for cell in row),
            "no example row that could be imported by mistake",
        )
        # The template's own headers must map back to every field.
        self.assertEqual(
            {roster_import._ALIASES[roster_import._header_key(h)] for h in headers},
            {f for f, _, _ in roster_import.ROSTER_COLUMNS},
        )

    # --- upload -> preview -> confirm ----------------------------------------
    def test_preview_then_confirm_creates_employees(self):
        self._login("admin@contoso.com")
        resp = self._upload(_sheet([
            ("NEW-1", "Ama Owusu", "Cashier", "024 123 4567", "ama@example.com", "001122", "2,500.00"),
            ("new 2", "Kofi Asante", "", "", "", "", ""),
        ]))
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(resp.headers["Location"].endswith("/company/employees/import/preview"))
        self.assertIsNone(self._emp(self.contoso_id, "NEW-1"), "nothing is saved before confirm")

        preview = self.client.get("/company/employees/import/preview").get_data(as_text=True)
        self.assertIn("Ama Owusu", preview)
        self.assertIn("Add 2 employees", preview)

        resp = self.client.post("/company/employees/import/confirm")
        self.assertEqual(resp.status_code, 302)
        ama = self._emp(self.contoso_id, "NEW-1")
        self.assertIsNotNone(ama)
        self.assertEqual(ama.job_title, "Cashier")
        self.assertEqual(ama.basic_salary, 2500.0)
        self.assertEqual(ama.status, "Active")
        self.assertEqual(ama.bank_account_number, "001122")
        self.assertIsNotNone(self._emp(self.contoso_id, "NEW2"), "staff IDs are normalised")
        audit = AuditTrail.query.filter_by(action="Employees imported").order_by(AuditTrail.id.desc()).first()
        self.assertIsNotNone(audit)
        self.assertIn("2 added", audit.notes)

    def test_update_fills_only_the_cells_given(self):
        existing = Employee.query.filter_by(client_company_id=self.contoso_id).first()
        existing.email = "keep@example.com"
        existing.bank_account_number = "999"
        db.session.commit()
        self._login("admin@contoso.com")
        self._upload(_sheet([(existing.staff_id, existing.full_name, "Supervisor", "", "", "", "")]))
        preview = self.client.get("/company/employees/import/preview").get_data(as_text=True)
        self.assertIn("Job title", preview)
        self.client.post("/company/employees/import/confirm")
        db.session.expire_all()
        updated = db.session.get(Employee, existing.id)
        self.assertEqual(updated.job_title, "Supervisor")
        self.assertEqual(updated.email, "keep@example.com", "an empty cell never blanks a saved value")
        self.assertEqual(updated.bank_account_number, "999")
        self.assertEqual(
            Employee.query.filter_by(client_company_id=self.contoso_id, staff_id=existing.staff_id).count(), 1
        )

    def test_bad_rows_are_skipped_and_listed_the_rest_import(self):
        self._login("admin@contoso.com")
        self._upload(_sheet([
            ("DUP-1", "First Copy", "", "", "", "", ""),
            ("DUP-1", "Second Copy", "", "", "", "", ""),
            ("NONAME", "", "", "", "", "", ""),
            ("", "No Staff Id", "", "", "", "", ""),
            ("BADMAIL", "Bad Mail", "", "", "not-an-email", "", ""),
            ("BADPAY", "Bad Pay", "", "", "", "", "lots"),
            ("OK-1", "Good Row", "", "0201234567", "", "", ""),
        ]))
        staged = roster_import.load_staged(self._session_token(), self.contoso_id)
        issues = {e["staff_id"] or e["name"]: e["issue"] for e in staged["errors"]}
        self.assertIn("more than once", issues["DUP-1"])
        self.assertIn("Full name is blank", issues["NONAME"])
        self.assertIn("Staff ID is blank", issues["No Staff Id"])
        self.assertIn("not an email", issues["BADMAIL"])
        self.assertIn("not an amount", issues["BADPAY"])
        self.assertEqual({r["staff_id"] for r in staged["to_create"]}, {"DUP-1", "OK-1"})
        self.client.post("/company/employees/import/confirm")
        self.assertIsNotNone(self._emp(self.contoso_id, "OK-1"))
        self.assertIsNone(self._emp(self.contoso_id, "BADMAIL"))

    def test_non_mobile_phone_is_kept_but_flagged(self):
        self._login("admin@contoso.com")
        self._upload(_sheet([("LAND-1", "Landline Worker", "", "0302 123456", "", "", "")]))
        staged = roster_import.load_staged(self._session_token(), self.contoso_id)
        self.assertEqual(len(staged["warnings"]), 1)
        self.client.post("/company/employees/import/confirm")
        self.assertEqual(self._emp(self.contoso_id, "LAND-1").phone, "0302 123456")

    def test_missing_required_column_is_refused_up_front(self):
        self._login("admin@contoso.com")
        resp = self._upload(_sheet([("Ama",)], headers=("Full name",)))
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Staff ID", resp.get_data(as_text=True))

    def test_a_non_workbook_is_refused(self):
        self._login("admin@contoso.com")
        resp = self._upload(BytesIO(b"staff id,full name\nA,B\n"), filename="roster.csv")
        self.assertEqual(resp.status_code, 200)
        self.assertIsNone(self._emp(self.contoso_id, "A"))

    def test_discard_saves_nothing(self):
        self._login("admin@contoso.com")
        self._upload(_sheet([("GONE-1", "Never Saved", "", "", "", "", "")]))
        self.client.post("/company/employees/import/discard")
        self.assertIsNone(self._emp(self.contoso_id, "GONE-1"))
        resp = self.client.post("/company/employees/import/confirm")
        self.assertEqual(resp.status_code, 302)
        self.assertIsNone(self._emp(self.contoso_id, "GONE-1"))

    # --- tenant isolation -----------------------------------------------------
    def test_company_in_the_form_is_ignored(self):
        self._login("admin@contoso.com")
        self._upload(_sheet([("SPOOF-1", "Spoof Worker", "", "", "", "", "")]),
                     extra={"client_company_id": str(self.acme_id), "client_id": str(self.acme_id)})
        self.client.post("/company/employees/import/confirm",
                         data={"client_company_id": str(self.acme_id)})
        self.assertIsNotNone(self._emp(self.contoso_id, "SPOOF-1"))
        self.assertIsNone(self._emp(self.acme_id, "SPOOF-1"))

    def test_another_tenants_staged_import_cannot_be_confirmed(self):
        self._login("admin@contoso.com")
        self._upload(_sheet([("CROSS-1", "Cross Worker", "", "", "", "", "")]))
        contoso_token = self._session_token()

        # Single client + logout/login is the project's tenant-switch idiom (two
        # test clients share the app context, and with it the logged-in user).
        self.client.get("/logout")
        self._login("admin@acme.com")
        with self.client.session_transaction() as sess:
            sess["client_emp_import_token"] = contoso_token  # replayed token from another tenant
        resp = self.client.post("/company/employees/import/confirm")
        self.assertEqual(resp.status_code, 302)
        self.assertIsNone(self._emp(self.acme_id, "CROSS-1"))
        self.assertIsNone(self._emp(self.contoso_id, "CROSS-1"))

    def test_operator_cannot_use_the_client_route(self):
        self._login("admin@payrolla.com")
        resp = self.client.get("/company/employees/import")
        self.assertEqual(resp.status_code, 302)

    # --- operator console shares the importer ---------------------------------
    def test_operator_bulk_import_uses_the_shared_importer(self):
        self._login("admin@payrolla.com")
        acme = db.session.get(ClientCompany, self.acme_id)
        resp = self.client.get(f"/employees/clients/{acme.id}/roster-template")
        self.assertEqual(resp.status_code, 200)
        page = self.client.get(f"/employees/clients/{acme.id}/bulk-import").get_data(as_text=True)
        self.assertIn("Ghana Card number", page, "the operator page lists the shared columns")
        resp = self.client.post(
            f"/employees/clients/{acme.id}/bulk-import",
            data={"file": (_sheet([("OPS-1", "Operator Import", "Clerk", "", "", "", "1800")]), "r.xlsx")},
            content_type="multipart/form-data",
        )
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Operator Import", resp.get_data(as_text=True))
        resp = self.client.post(f"/employees/clients/{acme.id}/bulk-import/confirm")
        self.assertEqual(resp.status_code, 302)
        worker = self._emp(acme.id, "OPS-1")
        self.assertIsNotNone(worker)
        self.assertEqual(worker.job_title, "Clerk")
        self.assertEqual(worker.basic_salary, 1800.0)
        self.assertEqual(worker.assigned_client, acme.name)

    def test_operator_confirm_for_a_different_client_is_refused(self):
        self._login("admin@payrolla.com")
        self.client.post(
            f"/employees/clients/{self.acme_id}/bulk-import",
            data={"file": (_sheet([("MOVE-1", "Moved Worker", "", "", "", "", "")]), "r.xlsx")},
            content_type="multipart/form-data",
        )
        self.client.post(f"/employees/clients/{self.contoso_id}/bulk-import/confirm")
        self.assertIsNone(self._emp(self.contoso_id, "MOVE-1"))
        self.assertIsNone(self._emp(self.acme_id, "MOVE-1"))

    # --- unit -------------------------------------------------------------------
    def test_parse_amount(self):
        self.assertEqual(roster_import.parse_amount("GHS 2,500.50"), 2500.5)
        self.assertIsNone(roster_import.parse_amount(None))
        with self.assertRaises(ValueError):
            roster_import.parse_amount("-5")
        with self.assertRaises(ValueError):
            roster_import.parse_amount("abc")

    def test_overlong_cell_is_a_row_error_not_a_failed_confirm(self):
        self._login("admin@contoso.com")
        self._upload(_sheet([("LONG-1", "Long Title", "x" * 200, "", "", "", "")]))
        staged = roster_import.load_staged(self._session_token(), self.contoso_id)
        self.assertEqual(staged["to_create"], [])
        self.assertIn("longer than", staged["errors"][0]["issue"])

    def _session_token(self):
        with self.client.session_transaction() as sess:
            return sess.get("client_emp_import_token")


if __name__ == "__main__":
    unittest.main()
