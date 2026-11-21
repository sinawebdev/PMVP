"""Every change to an employee record lands in the audit trail.

Recorded once, at the session (app.employee_audit), so the operator forms, the
client-portal forms, the roster import and payroll uploads are all covered by
the same rule: one row per employee per save, naming each field that changed,
with identity and payment numbers cut to their last four characters.
"""

import os
import unittest
from io import BytesIO

os.environ["SKIP_DOTENV"] = "true"
os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["SEED_DEMO_DATA"] = "true"
os.environ["PERSISTENCE_REQUIRED"] = "false"

from flask_login import login_user  # noqa: E402
from openpyxl import Workbook  # noqa: E402

from app import create_app, db  # noqa: E402
from app.employee_audit import describe_changes, set_change_source  # noqa: E402
from app.models import AuditTrail, ClientCompany, Employee, User  # noqa: E402


class EmployeeAuditTestCase(unittest.TestCase):
    def setUp(self):
        self.app = create_app()
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()
        self.ctx = self.app.app_context()
        self.ctx.push()
        self.contoso = ClientCompany.query.filter_by(name="Contoso Ltd").one()
        # Set up outside any request: seeding-style changes are not recorded.
        self.emp = Employee.query.filter_by(client_company_id=self.contoso.id).order_by(Employee.id).first()
        self.emp.bank_account_number = "0011223344"
        self.emp.phone = "0241112222"
        self.emp.status = "Active"
        db.session.commit()
        self.emp_id = self.emp.id
        self.start = db.session.query(db.func.max(AuditTrail.id)).scalar() or 0

    def tearDown(self):
        db.session.remove()
        self.ctx.pop()

    def _login(self, email):
        self.client.get("/logout")
        resp = self.client.post("/login", data={"email": email, "password": "password123"})
        self.assertEqual(resp.status_code, 302)

    def _rows(self, action=None):
        db.session.expire_all()
        query = AuditTrail.query.filter(AuditTrail.id > self.start, AuditTrail.related_record_type == "Employee")
        if action:
            query = query.filter(AuditTrail.action == action)
        return query.order_by(AuditTrail.id).all()

    def _employee(self):
        db.session.expire_all()
        return db.session.get(Employee, self.emp_id)

    def _operator_form(self, emp, **changes):
        form = {
            "full_name": emp.full_name, "email": emp.email or "", "phone": emp.phone or "",
            "department": emp.department or "", "bank_name": emp.bank_name or "",
            "bank_branch": emp.bank_branch or "", "bank_account": emp.bank_account_number or "",
            "status": emp.status, "tax_relief": str(emp.tax_relief_monthly or 0),
        }
        form.update(changes)
        return form

    # --- operator console ---------------------------------------------------------
    def test_operator_edit_names_each_change_and_masks_the_account(self):
        self._login("admin@payrolla.com")
        emp = self._employee()
        resp = self.client.post(f"/employees/clients/{self.contoso.id}/edit/{emp.id}",
                                data=self._operator_form(emp, bank_account="9988776655", phone="0549990000"))
        self.assertEqual(resp.status_code, 302)
        rows = self._rows("Employee updated")
        self.assertEqual(len(rows), 1, [r.notes for r in self._rows()])
        notes = rows[0].notes
        self.assertIn("Bank account ••3344 → ••6655", notes)
        self.assertIn("Phone 0241112222 → 0549990000", notes)
        self.assertNotIn("9988776655", notes)
        self.assertNotIn("0011223344", notes)
        self.assertEqual(rows[0].related_record_id, emp.id)
        self.assertEqual(rows[0].user_id, User.query.filter_by(email="admin@payrolla.com").one().id)

    def test_saving_with_no_changes_writes_nothing(self):
        self._login("admin@payrolla.com")
        emp = self._employee()
        self.client.post(f"/employees/clients/{self.contoso.id}/edit/{emp.id}", data=self._operator_form(emp))
        self.assertEqual(self._rows(), [])

    def test_operator_deactivate_and_reactivate(self):
        self._login("admin@payrolla.com")
        self.client.post(f"/employees/clients/{self.contoso.id}/deactivate/{self.emp_id}")
        self.client.post(f"/employees/clients/{self.contoso.id}/reactivate/{self.emp_id}")
        actions = [r.action for r in self._rows()]
        self.assertEqual(actions, ["Employee deactivated", "Employee reactivated"])

    def test_operator_add_is_linked_to_the_new_employee(self):
        self._login("admin@payrolla.com")
        self.client.post(f"/employees/clients/{self.contoso.id}/add",
                         data={"staff_id": "AUD-1", "full_name": "Abena Audit", "tax_relief": "0"})
        new = Employee.query.filter_by(client_company_id=self.contoso.id, staff_id="AUD-1").one()
        rows = self._rows("Employee added")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].related_record_id, new.id)
        self.assertIn("Abena Audit (AUD-1) added", rows[0].notes)

    # --- client portal ------------------------------------------------------------
    def test_client_portal_changes_are_recorded_once(self):
        self._login("admin@contoso.com")
        emp = self._employee()
        self.client.post(f"/company/employees/{emp.id}/edit", data={
            "staff_id": emp.staff_id, "full_name": emp.full_name, "phone": emp.phone,
            "bank_account": emp.bank_account_number, "basic_salary": "4,750.00",
            "status": "Active", "tax_relief": str(emp.tax_relief_monthly or 0),
            "email": emp.email or "", "momo_number": emp.momo_number or "",
            "department": emp.department or "", "job_title": emp.job_title or "",
            "ssnit_number": emp.ssnit_number or "", "ghana_card_number": emp.ghana_card_number or "",
            "tin": emp.tin or "", "bank_name": emp.bank_name or "", "bank_branch": emp.bank_branch or "",
        })
        rows = self._rows()
        self.assertEqual([r.action for r in rows], ["Employee updated"])
        self.assertIn("Basic salary", rows[0].notes)
        self.assertIn("→ 4,750.00", rows[0].notes)

        self.client.post(f"/company/employees/{emp.id}/deactivate")
        self.assertEqual(self._rows()[-1].action, "Employee deactivated")
        # The company's own audit page shows it.
        self.assertIn("Employee deactivated", self.client.get("/company/audit").get_data(as_text=True))

    # --- imports ------------------------------------------------------------------
    def test_roster_import_records_each_employee_and_its_source(self):
        self._login("admin@contoso.com")
        workbook = Workbook()
        sheet = workbook.active
        sheet.append(["Staff ID", "Full name", "Bank account number"])
        sheet.append([self._employee().staff_id, self._employee().full_name, "5555666677"])
        sheet.append(["IMP-9", "Yaw Import", "1231231234"])
        stream = BytesIO()
        workbook.save(stream)
        stream.seek(0)
        self.client.post("/company/employees/import", data={"file": (stream, "roster.xlsx")},
                         content_type="multipart/form-data")
        self.client.post("/company/employees/import/confirm")
        updated = self._rows("Employee updated")
        added = self._rows("Employee added")
        self.assertEqual(len(updated), 1)
        self.assertIn("Bank account ••3344 → ••6677", updated[0].notes)
        self.assertIn("Via employee list import (roster.xlsx).", updated[0].notes)
        self.assertEqual(len(added), 1)
        self.assertIn("Yaw Import (IMP-9) added", added[0].notes)

    def test_a_payroll_upload_change_names_its_source(self):
        # The upload path itself is covered by the payroll tests; what matters
        # here is that a change made under its label says so.
        user = User.query.filter_by(email="admin@payrolla.com").one()
        with self.app.test_request_context("/payroll/runs/new", method="POST"):
            login_user(user)
            set_change_source("payroll upload for March 2026")
            emp = db.session.get(Employee, self.emp_id)
            emp.bank_account_number = "4444333322"
            db.session.commit()
        rows = self._rows("Employee updated")
        self.assertEqual(len(rows), 1)
        self.assertIn("Bank account ••3344 → ••3322. Via payroll upload for March 2026.", rows[0].notes)

    def test_changes_outside_a_request_are_not_recorded(self):
        emp = self._employee()
        emp.phone = "0200000000"
        db.session.commit()
        self.assertEqual(self._rows(), [])

    def test_describe_changes_treats_blank_and_none_alike(self):
        emp = self._employee()
        emp.email = None
        db.session.commit()
        emp.email = ""
        self.assertEqual(describe_changes(emp), [])
        db.session.rollback()


if __name__ == "__main__":
    unittest.main()
