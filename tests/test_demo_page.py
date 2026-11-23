"""The staff-only Demo workspace page (app.demo_page).

Who can reach it, what the form accepts, that the three invites go to the
addresses typed, and that the demo says what it is on every page.
"""

import os
import unittest
from unittest import mock

os.environ["SKIP_DOTENV"] = "true"
os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["SEED_DEMO_DATA"] = "true"
os.environ["PERSISTENCE_REQUIRED"] = "false"

from app import create_app, db  # noqa: E402
from app.distribution.channels import SendResult  # noqa: E402
from app.models import Bureau, ClientCompany, User  # noqa: E402

FORM = {
    "bureau_admin_name": "Bea Admin", "bureau_admin_email": "Bureau@Demo-Test.example.com",
    "client_admin_name": "Cal Admin", "client_admin_email": "client@demo-test.example.com",
    "client_preparer_name": "Pat Preparer", "client_preparer_email": "preparer@demo-test.example.com",
}
PASSWORD = "a-long-password-1"


class Outbox:
    def __init__(self):
        self.sent = []

    def send(self, message):
        self.sent.append(message)
        return SendResult(ok=True, provider="test")


class DemoPageTestCase(unittest.TestCase):
    def setUp(self):
        self.app = create_app()
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()
        self.ctx = self.app.app_context()
        self.ctx.push()
        self.outbox = Outbox()
        patcher = mock.patch("app.invites.get_sender", return_value=self.outbox)
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        db.session.remove()
        self.ctx.pop()

    def _login(self, email, password="password123"):
        self.client.get("/logout")
        resp = self.client.post("/login", data={"email": email, "password": password})
        self.assertEqual(resp.status_code, 302, email)

    def _set_up(self):
        self._login("admin@payrolla.com")
        resp = self.client.post("/clients/demo/setup", data=FORM)
        self.assertEqual(resp.status_code, 302)
        return Bureau.query.filter(Bureau.is_demo.is_(True)).one()

    def _activate(self, email):
        user = User.query.filter_by(email=email).one()
        user.set_password(PASSWORD)
        db.session.commit()

    def _real_bureau_admin(self):
        bureau = Bureau(name="Real Bureau Ltd", code="REAL")
        db.session.add(bureau)
        db.session.flush()
        admin = User(name="Real Admin", email="admin@real-bureau.example.com", role="bureau_admin",
                     bureau_id=bureau.id)
        admin.set_password("password123")
        db.session.add(admin)
        db.session.commit()
        return admin.email

    # --- who reaches it ------------------------------------------------------------
    def test_staff_who_manage_clients_can_open_it(self):
        for email in ("admin@payrolla.com", "director@payrolla.com", "operator@payrolla.com"):
            with self.subTest(email=email):
                self._login(email)
                html = self.client.get("/clients/demo").get_data(as_text=True)
                self.assertIn("Set up demo workspace", html)
                self.assertIn("/clients/demo", self.client.get("/clients").get_data(as_text=True))

    def test_other_staff_are_turned_away(self):
        self._login("payroll@payrolla.com")
        self.assertEqual(self.client.get("/clients/demo").status_code, 302)
        self.assertEqual(self.client.post("/clients/demo/setup", data=FORM).status_code, 302)
        self.assertEqual(Bureau.query.count(), 0)

    def test_bureaus_and_companies_never_see_it(self):
        bureau = self._set_up()
        self._activate("bureau@demo-test.example.com")
        self._activate("client@demo-test.example.com")
        real = self._real_bureau_admin()
        demo_user = User.query.filter_by(email="client@demo-test.example.com").one()
        paths = ("/clients/demo", "/clients/demo/setup", "/clients/demo/refresh", "/clients/demo/remove",
                 f"/clients/demo/logins/{demo_user.id}/resend")
        for email, password in ((real, "password123"), ("bureau@demo-test.example.com", PASSWORD)):
            self._login(email, password)
            for path in paths:
                with self.subTest(email=email, path=path):
                    resp = self.client.get(path) if path == "/clients/demo" else self.client.post(path)
                    self.assertEqual(resp.status_code, 404)
        self._login("client@demo-test.example.com", PASSWORD)
        resp = self.client.get("/clients/demo")
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/company", resp.headers["Location"])
        self.assertIsNotNone(db.session.get(Bureau, bureau.id))

    # --- the form ------------------------------------------------------------------
    def test_set_up_invites_the_three_addresses_typed(self):
        bureau = self._set_up()
        recipients = sorted(m.recipient for m in self.outbox.sent)
        self.assertEqual(recipients, ["bureau@demo-test.example.com", "client@demo-test.example.com",
                                      "preparer@demo-test.example.com"])
        preparer_email = next(m for m in self.outbox.sent if m.recipient.startswith("preparer"))
        self.assertIn("as a payroll preparer", preparer_email.body_text)
        users = User.query.filter(User.email.like("%@demo-test.example.com")).all()
        self.assertTrue(all(u.invite_pending for u in users))
        html = self.client.get("/clients/demo").get_data(as_text=True)
        self.assertIn("Volta Harbour Logistics (Demo)", html)
        self.assertIn("A client with its own portal logins", html)
        self.assertIn("Refresh demo data", html)
        self.assertNotIn("Set up demo workspace", html)
        self.assertTrue(bureau.is_demo)

    def test_bad_form_input_creates_nothing(self):
        self._login("admin@payrolla.com")
        cases = {
            "a bad address": dict(FORM, client_admin_email="not-an-email"),
            "the same address twice": dict(FORM, client_preparer_email="client@demo-test.example.com"),
            "an address already signing in": dict(FORM, bureau_admin_email="admin@contoso.com"),
            "a missing name": dict(FORM, client_admin_name=""),
        }
        for label, form in cases.items():
            with self.subTest(label):
                resp = self.client.post("/clients/demo/setup", data=form)
                self.assertEqual(resp.status_code, 400)
                self.assertIn("is-invalid", resp.get_data(as_text=True))
        self.assertEqual(Bureau.query.count(), 0)
        self.assertEqual(ClientCompany.query.filter(ClientCompany.is_demo.is_(True)).count(), 0)
        self.assertEqual(self.outbox.sent, [])

    # --- resend --------------------------------------------------------------------
    def test_resend_only_for_a_pending_demo_login(self):
        self._set_up()
        self.outbox.sent.clear()
        bureau_admin = User.query.filter_by(email="bureau@demo-test.example.com").one()
        resp = self.client.post(f"/clients/demo/logins/{bureau_admin.id}/resend")
        self.assertEqual(resp.status_code, 302)
        self.assertEqual([m.recipient for m in self.outbox.sent], ["bureau@demo-test.example.com"])

        contoso = User.query.filter_by(email="admin@contoso.com").one()
        self.assertEqual(self.client.post(f"/clients/demo/logins/{contoso.id}/resend").status_code, 404)

        bureau_admin = db.session.get(User, bureau_admin.id)
        bureau_admin.activated_at = bureau_admin.invited_at
        db.session.commit()
        self.client.post(f"/clients/demo/logins/{bureau_admin.id}/resend")
        self.assertEqual(len(self.outbox.sent), 1)

    # --- refresh and remove ----------------------------------------------------------
    def test_refresh_and_remove_from_the_page(self):
        self._set_up()
        self.assertEqual(self.client.post("/clients/demo/refresh").status_code, 302)
        self.client.post("/clients/demo/remove")  # no tick: nothing happens
        self.assertEqual(Bureau.query.filter(Bureau.is_demo.is_(True)).count(), 1)
        self.client.post("/clients/demo/remove", data={"confirm": "remove"})
        self.assertEqual(Bureau.query.filter(Bureau.is_demo.is_(True)).count(), 0)
        self.assertEqual(User.query.filter(User.email.like("%@demo-test.example.com")).count(), 0)
        self.assertIn("Set up demo workspace", self.client.get("/clients/demo").get_data(as_text=True))
        from app.models import AuditTrail

        actions = {a.action for a in AuditTrail.query.filter(AuditTrail.action.like("Demo workspace%"))}
        self.assertEqual(actions, {"Demo workspace set up", "Demo workspace refreshed", "Demo workspace removed"})

    # --- the demo says what it is ----------------------------------------------------
    def test_both_shells_say_it_is_a_demo(self):
        self._set_up()
        self._activate("bureau@demo-test.example.com")
        self._activate("client@demo-test.example.com")
        self._login("bureau@demo-test.example.com", PASSWORD)
        self.assertIn("Demo workspace.", self.client.get("/dashboard").get_data(as_text=True))
        self._login("client@demo-test.example.com", PASSWORD)
        self.assertIn("Demo company.", self.client.get("/company").get_data(as_text=True))
        self._login("admin@contoso.com")
        self.assertNotIn("Demo company.", self.client.get("/company").get_data(as_text=True))

    def test_page_search_offers_it_to_staff_only(self):
        self._login("admin@payrolla.com")
        groups = self.client.get("/search/suggest", query_string={"q": "demo"}).get_json()["groups"]
        pages = next((g for g in groups if g["label"] == "Go to"), {"items": []})
        self.assertIn("Demo workspace", [i["label"] for i in pages["items"]])
        email = self._real_bureau_admin()
        self._login(email)
        groups = self.client.get("/search/suggest", query_string={"q": "demo"}).get_json()["groups"]
        labels = [i["label"] for g in groups for i in g["items"]]
        self.assertNotIn("Demo workspace", labels)


if __name__ == "__main__":
    unittest.main()
