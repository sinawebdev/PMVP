"""Search finds pages as well as records, and only ones the person can open.

The page list lives in app.page_search, but who may open each page is read
from the route's own guards. The strongest check here is the walk: for several
kinds of user, every page search offers must actually open for them.
"""

import os
import unittest

os.environ["SKIP_DOTENV"] = "true"
os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["SEED_DEMO_DATA"] = "true"
os.environ["PERSISTENCE_REQUIRED"] = "false"

from app import create_app, db  # noqa: E402
from app.models import ClientCompany, User  # noqa: E402
from app.page_search import COMPANY_PAGES, OPERATOR_PAGES, pages_for  # noqa: E402


class PageSearchTestCase(unittest.TestCase):
    def setUp(self):
        self.app = create_app()
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()
        self.ctx = self.app.app_context()
        self.ctx.push()

    def tearDown(self):
        db.session.remove()
        self.ctx.pop()

    def _login(self, email):
        self.client.get("/logout")
        resp = self.client.post("/login", data={"email": email, "password": "password123"})
        self.assertEqual(resp.status_code, 302)

    def _suggest(self, q, url="/search/suggest"):
        resp = self.client.get(url, query_string={"q": q})
        self.assertEqual(resp.status_code, 200)
        return {g["label"]: [i["label"] for i in g["items"]] for g in resp.get_json()["groups"]}

    def _make_contoso_a_bureau(self):
        self._login("admin@payrolla.com")
        contoso = ClientCompany.query.filter_by(name="Contoso Ltd").one()
        self.client.post(f"/clients/{contoso.id}/make-bureau")

    # --- the list itself ---------------------------------------------------------
    def test_every_listed_endpoint_exists(self):
        listed = {p.endpoint for p in OPERATOR_PAGES + COMPANY_PAGES}
        missing = {e for e in listed if e not in self.app.view_functions}
        self.assertEqual(missing, set())

    def test_every_offered_page_opens_for_the_person_offered_it(self):
        for email in ("admin@payrolla.com", "director@payrolla.com", "operator@payrolla.com",
                      "admin@contoso.com", "payroll@contoso.com"):
            self._login(email)
            user = User.query.filter_by(email=email).one()
            with self.app.test_request_context():
                from flask_login import login_user

                login_user(user)
                offered = [(p.label, p.endpoint) for p in pages_for(user)]
            self.assertTrue(offered)
            for label, endpoint in offered:
                if endpoint == "auth.logout":
                    continue
                with self.app.test_request_context():
                    from flask import url_for

                    path = url_for(endpoint)
                resp = self.client.get(path)
                self.assertEqual(resp.status_code, 200, f"{email} was offered {label} ({path})")

    # --- operator console --------------------------------------------------------
    def test_a_task_word_finds_its_page(self):
        self._login("admin@payrolla.com")
        self.assertEqual(self._suggest("paye")["Go to"][0], "Statutory rates")
        self.assertIn("Upload a payroll", self._suggest("upload")["Go to"])
        self.assertIn("Delivery history", self._suggest("sms")["Go to"])
        self.assertIn("Audit trail and expenses", self._suggest("who changed")["Go to"])

    def test_suggestions_include_clients_and_employees(self):
        self._login("admin@payrolla.com")
        found = self._suggest("contoso")
        self.assertIn("Contoso Ltd", found["Clients"])

    def test_the_results_page_leads_with_pages(self):
        self._login("admin@payrolla.com")
        html = self.client.get("/search", query_string={"q": "tax bands"}).get_data(as_text=True)
        self.assertIn("Go to", html)
        self.assertIn("Statutory rates", html)
        self.assertLess(html.index("Go to"), html.index("Client Companies"))

    def test_a_bureau_is_offered_neither_payrolla_pages_nor_other_firms(self):
        self._make_contoso_a_bureau()
        self._login("admin@contoso.com")
        self.assertNotIn("Go to", self._suggest("licences"))
        self.assertNotIn("Distribution monitor", self._suggest("monitor").get("Go to", []))
        self.assertNotIn("Database health", self._suggest("database").get("Go to", []))
        self.assertNotIn("Clients", self._suggest("acme"))
        self.assertNotIn("Employees", self._suggest("acme"))
        self.assertIn("Contoso Ltd", self._suggest("contoso")["Clients"])
        html = self.client.get("/search", query_string={"q": "acme"}).get_data(as_text=True)
        self.assertNotIn("Acme Manufacturing", html)

    # --- client portal -------------------------------------------------------------
    def test_the_portal_has_search_with_its_own_pages(self):
        self._login("admin@contoso.com")
        self.assertIn('action="/company/search"', self.client.get("/company").get_data(as_text=True))
        found = self._suggest("branding", "/company/search/suggest")
        self.assertEqual(found["Go to"], ["Email branding"])
        self.assertIn("Statutory rates", self._suggest("paye", "/company/search/suggest")["Go to"])

    def test_a_preparer_is_not_offered_admin_pages(self):
        self._login("payroll@contoso.com")
        self.assertNotIn("Go to", self._suggest("branding", "/company/search/suggest"))

    def test_portal_search_finds_only_the_companys_employees(self):
        from app.models import Employee

        contoso = ClientCompany.query.filter_by(name="Contoso Ltd").one()
        acme = ClientCompany.query.filter_by(name="Acme Manufacturing Ltd").one()
        mine = Employee.query.filter_by(client_company_id=contoso.id).first()
        theirs = Employee.query.filter_by(client_company_id=acme.id).first()
        self._login("admin@contoso.com")
        self.assertIn(mine.full_name, self._suggest(mine.staff_id, "/company/search/suggest")["Employees"])
        self.assertNotIn("Employees", self._suggest(theirs.staff_id, "/company/search/suggest"))
        html = self.client.get("/company/search", query_string={"q": mine.full_name}).get_data(as_text=True)
        self.assertIn(mine.staff_id, html)

    def test_a_name_starting_with_the_query_comes_first(self):
        # "ko" is inside Darko and Akosua too; alphabetical order would put
        # Kojo last, and on a real roster off the four-item list entirely.
        self._login("admin@contoso.com")
        self.assertEqual(self._suggest("ko", "/company/search/suggest")["Employees"][0], "Kojo Addai")
        self._login("admin@payrolla.com")  # across clients: Kofi Appiah, Kojo Addai
        first, second = self._suggest("ko")["Employees"][:2]
        self.assertTrue(first.startswith("Ko") and second.startswith("Ko"), (first, second))

    def test_an_operator_cannot_use_the_portal_search(self):
        self._login("admin@payrolla.com")
        self.assertEqual(self.client.get("/company/search/suggest?q=x").status_code, 302)


if __name__ == "__main__":
    unittest.main()
