"""Operations & Onboarding, Phase 1 — the client onboarding experience.

Covers the operator workflow: Add Company -> fill details -> save -> the company
appears immediately in Client Companies, and the operator lands on an onboarding
summary that spells out the MANUAL Supabase credential step. The load-bearing
invariant here is that onboarding is independent of authentication — saving a
company must never create a User row.
"""

import os
import unittest

os.environ["SKIP_DOTENV"] = "true"
os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["PERSISTENCE_REQUIRED"] = "false"

from app import create_app, db  # noqa: E402
from app.models import AuditTrail, ClientCompany, User  # noqa: E402
from app.routes import normalise_company_code  # noqa: E402


class ClientOnboardingTestCase(unittest.TestCase):
    def setUp(self):
        self.app = create_app()
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()
        self.ctx = self.app.app_context()
        self.ctx.push()

    def tearDown(self):
        self.ctx.pop()

    def _login_admin(self):
        admin = User.query.filter(
            User.role == "admin", User.client_company_id.is_(None)
        ).first()
        self.assertIsNotNone(admin, "seed should include a platform admin")
        resp = self.client.post(
            "/login", data={"email": admin.email, "password": "password123"}
        )
        self.assertEqual(resp.status_code, 302)
        return admin

    def _form(self, **overrides):
        data = {
            "name": "Harbour Freight Ltd",
            "company_code": "hfl",
            "contact_person": "Ama Mensah",
            "email": "accounts@harbourfreight.com",
            "phone": "0244000111",
            "address": "12 Liberation Road, Airport City",
            "location": "Accra",
            "service_type": "Freight handling",
            "status": "Active",
            "notes": "Invoices monthly in arrears.",
        }
        data.update(overrides)
        return data

    # --- code normalisation --------------------------------------------------
    def test_company_code_is_normalised(self):
        self.assertEqual(normalise_company_code("  msc  "), "MSC")
        self.assertEqual(normalise_company_code("acme ghana"), "ACME-GHANA")
        self.assertEqual(normalise_company_code("acme__gh"), "ACME-GH")
        self.assertEqual(normalise_company_code("-acme-"), "ACME")
        self.assertEqual(normalise_company_code(None), "")

    # --- the happy path ------------------------------------------------------
    def test_add_company_saves_all_onboarding_fields(self):
        self._login_admin()
        resp = self.client.post("/clients/add", data=self._form())
        self.assertEqual(resp.status_code, 302)

        company = ClientCompany.query.filter_by(name="Harbour Freight Ltd").first()
        self.assertIsNotNone(company)
        self.assertEqual(company.company_code, "HFL")  # normalised to uppercase
        self.assertEqual(company.contact_person, "Ama Mensah")
        self.assertEqual(company.email, "accounts@harbourfreight.com")
        self.assertEqual(company.phone, "0244000111")
        self.assertEqual(company.address, "12 Liberation Road, Airport City")
        self.assertEqual(company.status, "Active")
        self.assertEqual(company.notes, "Invoices monthly in arrears.")
        # Lands on the onboarding summary, not back on the list.
        self.assertTrue(resp.headers["Location"].endswith(f"/clients/{company.id}/onboarding"))

    def test_new_company_appears_immediately_in_client_companies(self):
        self._login_admin()
        self.client.post("/clients/add", data=self._form())
        page = self.client.get("/clients")
        self.assertEqual(page.status_code, 200)
        body = page.get_data(as_text=True)
        self.assertIn("Harbour Freight Ltd", body)
        self.assertIn("HFL", body)

    def test_onboarding_summary_shows_the_four_key_fields_and_the_notice(self):
        self._login_admin()
        self.client.post("/clients/add", data=self._form())
        company = ClientCompany.query.filter_by(company_code="HFL").first()
        page = self.client.get(f"/clients/{company.id}/onboarding")
        self.assertEqual(page.status_code, 200)
        body = page.get_data(as_text=True)
        for expected in ("Harbour Freight Ltd", "HFL", "accounts@harbourfreight.com", "Active"):
            self.assertIn(expected, body)
        self.assertIn(
            "Provision authentication credentials in Supabase Auth before the "
            "client can sign in.",
            body,
        )

    def test_onboarding_records_an_audit_entry(self):
        self._login_admin()
        self.client.post("/clients/add", data=self._form())
        company = ClientCompany.query.filter_by(company_code="HFL").first()
        entry = AuditTrail.query.filter_by(
            related_record_type="ClientCompany", related_record_id=company.id
        ).first()
        self.assertIsNotNone(entry)
        self.assertEqual(entry.action, "Client company onboarded")

    # --- company creation is independent of authentication -------------------
    def test_creating_a_company_creates_no_user(self):
        self._login_admin()
        before = User.query.count()
        self.client.post("/clients/add", data=self._form())
        self.assertEqual(User.query.count(), before)
        self.assertIsNone(
            User.query.filter_by(email="accounts@harbourfreight.com").first()
        )

    # --- validation ----------------------------------------------------------
    def test_missing_name_or_code_is_rejected_without_saving(self):
        self._login_admin()
        for field in ("name", "company_code"):
            with self.subTest(field=field):
                before = ClientCompany.query.count()
                resp = self.client.post("/clients/add", data=self._form(**{field: ""}))
                self.assertEqual(resp.status_code, 200)  # re-rendered, not redirected
                self.assertIn("required", resp.get_data(as_text=True))
                self.assertEqual(ClientCompany.query.count(), before)

    def test_duplicate_company_code_is_rejected(self):
        self._login_admin()
        self.client.post("/clients/add", data=self._form())
        resp = self.client.post(
            "/clients/add", data=self._form(name="Another Company", company_code="HFL")
        )
        self.assertEqual(resp.status_code, 200)
        self.assertIn("already in use", resp.get_data(as_text=True))
        self.assertIsNone(ClientCompany.query.filter_by(name="Another Company").first())

    def test_malformed_code_and_email_are_rejected(self):
        self._login_admin()
        resp = self.client.post("/clients/add", data=self._form(company_code="!!"))
        self.assertEqual(resp.status_code, 200)
        self.assertIn("2-20 letters", resp.get_data(as_text=True))

        resp = self.client.post("/clients/add", data=self._form(email="not-an-email"))
        self.assertEqual(resp.status_code, 200)
        self.assertIn("valid email address", resp.get_data(as_text=True))

    def test_rejected_submission_is_echoed_back_to_the_operator(self):
        self._login_admin()
        resp = self.client.post("/clients/add", data=self._form(name=""))
        body = resp.get_data(as_text=True)
        # The long fields the operator would hate to retype survive the round trip.
        self.assertIn("12 Liberation Road, Airport City", body)
        self.assertIn("Invoices monthly in arrears.", body)

    # --- editing -------------------------------------------------------------
    def test_editing_a_company_keeps_its_own_name_and_code_valid(self):
        self._login_admin()
        self.client.post("/clients/add", data=self._form())
        company = ClientCompany.query.filter_by(company_code="HFL").first()
        resp = self.client.post(
            f"/clients/{company.id}/edit", data=self._form(status="Inactive")
        )
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(resp.headers["Location"].endswith("/clients"))
        db.session.refresh(company)
        self.assertEqual(company.status, "Inactive")

    # --- authorization -------------------------------------------------------
    def test_onboarding_pages_require_a_platform_operator(self):
        anon = self.client.get("/clients/add")
        self.assertEqual(anon.status_code, 302)
        self.assertIn("/login", anon.headers["Location"])


if __name__ == "__main__":
    unittest.main()
