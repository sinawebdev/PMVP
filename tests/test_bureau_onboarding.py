"""Payroll bureaus, part D: how a firm becomes one.

The request-access form now asks whether the firm runs payroll for other
companies. Payrolla approves a "yes" as a payroll bureau and invites its
contact as bureau admin. A firm already onboarded as a single company
(Chrisnat, before bureaus existed) can be converted in place: it becomes the
bureau's first client and its logins move to the bureau.
"""

import os
import re
import unittest

os.environ["SKIP_DOTENV"] = "true"
os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["PERSISTENCE_REQUIRED"] = "false"

from app import access_requests, create_app, db  # noqa: E402
from app.models import AccessRequest, Bureau, ClientCompany, User  # noqa: E402
from app.roles import BUREAU_ADMIN, CLIENT_ADMIN  # noqa: E402

FORM = {
    "company_name": "Delta Payroll Services", "contact_name": "Dede Mensah",
    "email": "dede@delta.example", "phone": "024 123 4567", "location": "Tema",
    "employee_band": "11-50", "interest": "web", "account_type": "bureau", "message": "",
}


class BureauOnboardingTestCase(unittest.TestCase):
    def setUp(self):
        access_requests.reset()
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

    # --- the question on the public form --------------------------------------------
    def test_the_form_asks_and_stores_the_answer(self):
        html = self.client.get("/").get_data(as_text=True)
        self.assertIn("Do you run payroll for other companies?", html)
        self.assertEqual(self.client.post("/request-access", data=FORM).status_code, 302)
        self.assertEqual(AccessRequest.query.filter_by(email="dede@delta.example").one().account_type, "bureau")

    def test_an_old_style_submission_counts_as_a_company(self):
        data = dict(FORM, email="old@example.com")
        del data["account_type"]
        self.client.post("/request-access", data=data)
        self.assertEqual(AccessRequest.query.filter_by(email="old@example.com").one().account_type, "company")

    def test_a_made_up_answer_is_refused(self):
        resp = self.client.post("/request-access", data=dict(FORM, account_type="reseller"))
        self.assertEqual(resp.status_code, 400)

    # --- approving a bureau request ---------------------------------------------------
    def _bureau_request(self):
        self.client.post("/request-access", data=FORM)
        return AccessRequest.query.filter_by(email="dede@delta.example").one()

    def test_approving_creates_the_bureau_and_invites_its_admin(self):
        entry = self._bureau_request()
        self._login("admin@payrolla.com")
        page = self.client.get(f"/clients/requests/{entry.id}").get_data(as_text=True)
        self.assertIn("Approve as payroll bureau", page)
        resp = self.client.post(f"/clients/requests/{entry.id}/approve-bureau")
        self.assertEqual(resp.status_code, 302)
        bureau = Bureau.query.filter_by(name="Delta Payroll Services").one()
        admin = User.query.filter_by(email="dede@delta.example").one()
        self.assertEqual((admin.role, admin.bureau_id, admin.client_company_id), (BUREAU_ADMIN, bureau.id, None))
        self.assertTrue(admin.invite_pending)
        db.session.expire_all()
        self.assertEqual(db.session.get(AccessRequest, entry.id).status, "approved")

        # The invite works, and lands them in the bureau's (empty) operator view.
        from app.invites import make_token

        token = make_token(db.session.get(User, admin.id))
        self.client.get("/logout")
        resp = self.client.post(f"/invite/{token}", data={"password": "a-long-password-1", "confirm": "a-long-password-1"})
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(self.client.get("/dashboard").status_code, 200)
        self.assertNotIn("Contoso Ltd", self.client.get("/clients").get_data(as_text=True))

    def test_an_email_already_in_use_creates_nothing(self):
        entry = self._bureau_request()
        entry.email = "admin@contoso.com"
        db.session.commit()
        self._login("admin@payrolla.com")
        self.client.post(f"/clients/requests/{entry.id}/approve-bureau")
        self.assertIsNone(Bureau.query.filter_by(name="Delta Payroll Services").first())
        self.assertTrue(db.session.get(AccessRequest, entry.id).is_open)

    # --- converting an existing company (the Chrisnat path) --------------------------
    def test_a_company_can_become_a_bureau_with_its_logins(self):
        company = ClientCompany(name="Chrisnat Test Ltd", company_code="CNT")
        db.session.add(company)
        db.session.flush()
        owner = User(name="Mr Test", email="md@chrisnat.example", role=CLIENT_ADMIN,
                     client_company_id=company.id)
        owner.set_password("owner-pass-1")
        db.session.add(owner)
        db.session.commit()

        self._login("admin@payrolla.com")
        self.assertIn("set up as payroll bureau",
                      self.client.get(f"/clients/{company.id}").get_data(as_text=True))
        self.assertEqual(self.client.post(f"/clients/{company.id}/make-bureau").status_code, 302)
        db.session.expire_all()
        bureau = Bureau.query.filter_by(name="Chrisnat Test Ltd").one()
        self.assertEqual(bureau.code, "CNT")
        self.assertEqual(db.session.get(ClientCompany, company.id).bureau_id, bureau.id)
        moved = db.session.get(User, owner.id)
        self.assertEqual((moved.role, moved.bureau_id, moved.client_company_id), (BUREAU_ADMIN, bureau.id, None))

        # Same password, new interface: their own company is now their first client.
        self._login("md@chrisnat.example", "owner-pass-1")
        html = self.client.get("/clients").get_data(as_text=True)
        self.assertIn("Chrisnat Test Ltd", html)
        self.assertNotIn("Contoso Ltd", html)
        self.assertEqual(self.client.get("/statutory-rates/").status_code, 200)

        # Doing it twice does nothing.
        self._login("admin@payrolla.com")
        self.client.post(f"/clients/{company.id}/make-bureau")
        self.assertEqual(Bureau.query.filter_by(name="Chrisnat Test Ltd").count(), 1)

    def test_only_payrolla_staff_can_create_bureaus(self):
        entry = self._bureau_request()
        contoso = ClientCompany.query.filter_by(company_code="CONTOSO").one()
        bureau = Bureau(name="Echo Bureau", code="ECHO")
        db.session.add(bureau)
        db.session.flush()
        echo_admin = User(name="E", email="e@echo.example", role=BUREAU_ADMIN, bureau_id=bureau.id)
        echo_admin.set_password("echo-pass-1")
        db.session.add(echo_admin)
        db.session.commit()
        self._login("e@echo.example", "echo-pass-1")
        self.assertEqual(self.client.post(f"/clients/requests/{entry.id}/approve-bureau").status_code, 404)
        self.assertEqual(self.client.post(f"/clients/{contoso.id}/make-bureau").status_code, 404)
        self._login("admin@contoso.com")
        self.client.post(f"/clients/{contoso.id}/make-bureau")
        self.assertIsNone(db.session.get(ClientCompany, contoso.id).bureau_id)

    def test_unique_codes(self):
        from app.bureaus import unique_bureau_code

        db.session.add(Bureau(name="Zeta One", code="ZETA"))
        db.session.commit()
        self.assertEqual(unique_bureau_code("Zeta Two"), "ZETA-2")
        self.assertTrue(re.fullmatch(r"[A-Z0-9-]+", unique_bureau_code("  ")))


if __name__ == "__main__":
    unittest.main()
