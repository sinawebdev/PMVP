"""Forgot password: an emailed link, the same answer for every address, and a
staff button for someone who phones in (app.password_reset)."""
import os
import re
import unittest
from unittest.mock import patch

os.environ["SKIP_DOTENV"] = "true"
os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["PERSISTENCE_REQUIRED"] = "false"

from app import create_app, db, login_throttle, password_reset  # noqa: E402
from app.models import AuditTrail, Bureau, ClientCompany, User  # noqa: E402
from app.roles import BUREAU_ADMIN, CLIENT_ADMIN  # noqa: E402

TENANT = "admin@contoso.com"
NEW_PASSWORD = "a-fresh-password-9"


class PasswordResetTestCase(unittest.TestCase):
    def setUp(self):
        password_reset.reset()
        login_throttle.reset()
        self.app = create_app()
        self.app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
        self.client = self.app.test_client()
        self.ctx = self.app.app_context()
        self.ctx.push()
        self.sent = []
        patcher = patch("app.password_reset.dispatch", side_effect=self.sent.append)
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        db.session.remove()
        self.ctx.pop()

    def _ask(self, email):
        return self.client.post("/forgot-password", data={"email": email})

    def _link(self, message):
        return re.search(r"(/reset-password/\S+)", message.body_text).group(1)

    def _login(self, email, password):
        self.client.get("/logout")
        return self.client.post("/login", data={"email": email, "password": password})

    # --- asking ------------------------------------------------------------------
    def test_the_login_page_links_to_a_working_form(self):
        self.assertIn('href="/forgot-password"', self.client.get("/login").get_data(as_text=True))
        page = self.client.get("/forgot-password").get_data(as_text=True)
        self.assertIn("Send reset link", page)
        self.assertNotIn("reset by an administrator", page)

    def test_the_answer_is_the_same_whether_or_not_the_account_exists(self):
        known = self._ask(TENANT)
        unknown = self._ask("nobody@nowhere.example")
        self.assertEqual(known.status_code, unknown.status_code, 200)
        strip = lambda r, e: r.get_data(as_text=True).replace(e, "")  # noqa: E731
        self.assertEqual(strip(known, TENANT), strip(unknown, "nobody@nowhere.example"))
        self.assertEqual([m.recipient for m in self.sent], [TENANT])

    def test_a_malformed_address_is_asked_for_again(self):
        self.assertEqual(self._ask("not-an-email").status_code, 400)
        self.assertEqual(self.sent, [])

    def test_requests_are_capped_per_address(self):
        for _ in range(5):
            self._ask(TENANT)
        self.assertEqual(len(self.sent), 3)
        self.assertEqual(AuditTrail.query.filter_by(action="Password reset link sent").count(), 3)

    # --- using the link ------------------------------------------------------------
    def test_the_link_sets_a_new_password_once(self):
        self._ask(TENANT)
        link = self._link(self.sent[0])
        page = self.client.get(link)
        self.assertEqual(page.status_code, 200)
        self.assertIn(TENANT, page.get_data(as_text=True))

        done = self.client.post(link, data={"password": NEW_PASSWORD, "confirm": NEW_PASSWORD})
        self.assertEqual(done.status_code, 302)
        self.assertTrue(done.headers["Location"].endswith("/login"))
        self.assertEqual(self._login(TENANT, NEW_PASSWORD).status_code, 302)
        self.assertNotEqual(self._login(TENANT, "password123").status_code, 302)
        self.assertEqual(AuditTrail.query.filter_by(action="Password reset").count(), 1)

        self.client.get("/logout")
        self.assertEqual(self.client.get(link).status_code, 410)
        self.assertEqual(self.client.post(link, data={"password": "another-one-123",
                                                      "confirm": "another-one-123"}).status_code, 410)

    def test_using_one_link_cancels_the_others_but_asking_does_not(self):
        self._ask(TENANT)
        self._ask(TENANT)
        first, second = (self._link(m) for m in self.sent)
        # Asking twice changed nothing: the password still works and so do both links.
        self.assertEqual(self._login(TENANT, "password123").status_code, 302)
        self.client.get("/logout")
        self.assertEqual(self.client.get(first).status_code, 200)
        self.client.post(second, data={"password": NEW_PASSWORD, "confirm": NEW_PASSWORD})
        self.assertEqual(self.client.get(first).status_code, 410)

    def test_short_or_mismatched_passwords_are_refused(self):
        self._ask(TENANT)
        link = self._link(self.sent[0])
        self.assertEqual(self.client.post(link, data={"password": "short", "confirm": "short"}).status_code, 400)
        self.assertEqual(self.client.post(link, data={"password": NEW_PASSWORD,
                                                      "confirm": NEW_PASSWORD + "x"}).status_code, 400)
        self.assertEqual(self._login(TENANT, "password123").status_code, 302)

    def test_an_expired_or_forged_link_is_refused(self):
        self._ask(TENANT)
        link = self._link(self.sent[0])
        with patch.object(password_reset, "RESET_MAX_AGE", -1):
            self.assertEqual(self.client.get(link).status_code, 410)
        self.assertEqual(self.client.get("/reset-password/not-a-real-token").status_code, 410)

    def test_a_reset_lifts_a_sign_in_lockout(self):
        for _ in range(login_throttle.max_attempts()):
            self._login(TENANT, "wrong-password-0")
        key = login_throttle.account_key(TENANT)
        self.assertGreater(login_throttle.retry_after([key]), 0)
        self._ask(TENANT)
        link = self._link(self.sent[0])
        self.client.post(link, data={"password": NEW_PASSWORD, "confirm": NEW_PASSWORD})
        self.assertEqual(login_throttle.retry_after([key]), 0)

    def test_the_email_goes_out_on_a_thread(self):
        patch.stopall()
        user = User.query.filter_by(email=TENANT).one()
        with self.app.test_request_context("/forgot-password"):
            thread = password_reset.dispatch(password_reset.reset_message(user))
        thread.join(timeout=10)
        self.assertFalse(thread.is_alive())

    # --- staff sending the link ----------------------------------------------------------
    def _company(self):
        return ClientCompany.query.filter_by(company_code="CONTOSO").one()

    def test_payrolla_staff_send_a_link_from_the_onboarding_page(self):
        company = self._company()
        user = User.query.filter_by(email=TENANT).one()
        self._login("admin@payrolla.com", "password123")
        page = self.client.get(f"/clients/{company.id}/onboarding").get_data(as_text=True)
        self.assertIn("Send reset link", page)
        with patch("app.password_reset.get_sender") as sender:
            sender.return_value.send.return_value.ok = True
            resp = self.client.post(f"/clients/{company.id}/logins/{user.id}/send-reset")
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(sender.return_value.send.call_args.args[0].recipient, TENANT)
        self.assertIn("Sent by admin@payrolla.com",
                      AuditTrail.query.filter_by(action="Password reset link sent").one().notes)

    def test_a_login_from_another_company_is_not_found(self):
        acme = ClientCompany.query.filter_by(company_code="ACME").one()
        user = User.query.filter_by(email=TENANT).one()
        self._login("admin@payrolla.com", "password123")
        self.assertEqual(self.client.post(f"/clients/{acme.id}/logins/{user.id}/send-reset").status_code, 404)

    def test_a_bureau_resets_its_clients_logins_and_nobody_elses(self):
        bureau = Bureau(name="Delta Payroll Services", code="DPS")
        db.session.add(bureau)
        db.session.flush()
        client_co = ClientCompany(name="Delta Client Ltd", company_code="DCL", bureau_id=bureau.id)
        db.session.add(client_co)
        db.session.flush()
        clerk = User(name="Kofi Clerk", email="kofi@deltaclient.example", role=CLIENT_ADMIN,
                     client_company_id=client_co.id)
        clerk.set_password("clerk-pass-123")
        staff = User(name="Dede Mensah", email="dede@delta.example", role=BUREAU_ADMIN, bureau_id=bureau.id)
        staff.set_password("bureau-pass-1")
        db.session.add_all([clerk, staff])
        db.session.commit()
        contoso, tenant = self._company(), User.query.filter_by(email=TENANT).one()

        self._login("dede@delta.example", "bureau-pass-1")
        with patch("app.password_reset.get_sender") as sender:
            sender.return_value.send.return_value.ok = True
            ok = self.client.post(f"/clients/{client_co.id}/logins/{clerk.id}/send-reset")
            other = self.client.post(f"/clients/{contoso.id}/logins/{tenant.id}/send-reset")
        self.assertEqual(ok.status_code, 302)
        self.assertEqual(other.status_code, 404)
        self.assertEqual(sender.return_value.send.call_count, 1)

    def test_a_bureau_home_company_lists_the_bureau_logins_for_payrolla_staff(self):
        company = ClientCompany(name="Chrisnat Test Ltd", company_code="CNT")
        db.session.add(company)
        db.session.flush()
        owner = User(name="Mr Test", email="md@chrisnat.example", role=CLIENT_ADMIN,
                     client_company_id=company.id)
        owner.set_password("owner-pass-1")
        db.session.add(owner)
        db.session.commit()
        self._login("admin@payrolla.com", "password123")
        self.client.post(f"/clients/{company.id}/make-bureau")
        page = self.client.get(f"/clients/{company.id}/onboarding").get_data(as_text=True)
        self.assertIn("md@chrisnat.example", page)
        self.assertIn("signs in through Chrisnat Test Ltd", page)
        self.assertIn("Can sign in", page)
        self.assertNotIn("Invite sent", page)
        with patch("app.password_reset.get_sender") as sender:
            sender.return_value.send.return_value.ok = True
            resp = self.client.post(f"/clients/{company.id}/logins/{owner.id}/send-reset")
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(sender.return_value.send.call_args.args[0].recipient, "md@chrisnat.example")


if __name__ == "__main__":
    unittest.main()
