"""The Team page: a company or bureau admin manages who else can sign in.

Covers both planes (client portal and bureau operator interface), the rules
that keep a team safe (no acting on yourself, no roles from the other plane, no
reaching another firm's logins) and what "removed" means: no sign-in, the open
session ends, an unaccepted invite link dies, and the row stays on record.
"""

import os
import unittest
from unittest import mock

os.environ["SKIP_DOTENV"] = "true"
os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["PERSISTENCE_REQUIRED"] = "false"

from app import create_app, db  # noqa: E402
from app.distribution.channels import SendResult  # noqa: E402
from app.models import AuditTrail, ClientCompany, User  # noqa: E402
from app.roles import (  # noqa: E402
    BUREAU_ADMIN,
    BUREAU_OFFICER,
    CLIENT_ADMIN,
    CLIENT_PREPARER,
)

PASSWORD = "password123"


class _Outbox:
    """Stands in for the email sender and keeps what was sent."""

    def __init__(self):
        self.sent = []

    def send(self, message):
        self.sent.append(message)
        return SendResult(ok=True, provider="test")


class TeamTestCase(unittest.TestCase):
    def setUp(self):
        self.app = create_app()
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()
        self.ctx = self.app.app_context()
        self.ctx.push()
        self.outbox = _Outbox()
        patcher = mock.patch("app.invites.get_sender", return_value=self.outbox)
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        db.session.remove()
        self.ctx.pop()

    def _login(self, email, password=PASSWORD, expect=302):
        self.client.get("/logout")
        resp = self.client.post("/login", data={"email": email, "password": password})
        self.assertEqual(resp.status_code, expect, resp.get_data(as_text=True)[:400])
        return resp

    def _user(self, email):
        db.session.expire_all()
        return User.query.filter_by(email=email).one()

    def _company(self, name):
        return ClientCompany.query.filter_by(name=name).one()


class CompanyTeamTests(TeamTestCase):
    """client_admin manages the company's logins in the client portal."""

    def test_admin_sees_the_team_and_the_menu_link(self):
        self._login("admin@contoso.com")
        page = self.client.get("/company/team")
        self.assertEqual(page.status_code, 200)
        html = page.get_data(as_text=True)
        for email in ("admin@contoso.com", "finance@contoso.com", "payroll@contoso.com"):
            self.assertIn(email, html)
        self.assertNotIn("admin@acme.com", html)
        self.assertIn('href="/company/team"', self.client.get("/company").get_data(as_text=True))

    def test_a_preparer_has_no_team_page_and_no_link(self):
        self._login("payroll@contoso.com")
        self.assertEqual(self.client.get("/company/team").status_code, 302)
        self.assertNotIn('href="/company/team"', self.client.get("/company").get_data(as_text=True))

    def test_inviting_a_preparer_creates_the_login_and_emails_them(self):
        self._login("admin@contoso.com")
        resp = self.client.post("/company/team/invite", data={
            "invite_name": "Kofi Asante", "invite_email": "Kofi@Contoso.com", "invite_role": CLIENT_PREPARER})
        self.assertEqual(resp.status_code, 302)
        kofi = self._user("kofi@contoso.com")
        contoso = self._company("Contoso Ltd")
        self.assertEqual((kofi.role, kofi.client_company_id, kofi.bureau_id), (CLIENT_PREPARER, contoso.id, None))
        self.assertTrue(kofi.invite_pending)
        self.assertEqual(len(self.outbox.sent), 1)
        body = self.outbox.sent[0].body_text
        self.assertIn("has added you to Contoso Ltd", body)
        self.assertIn("as a preparer", body)
        self.assertTrue(AuditTrail.query.filter_by(action="Team member invited").count())

        # The link works and lands them in their company's portal.
        from app.invites import make_token

        token = make_token(kofi)
        self.client.get("/logout")
        resp = self.client.post(f"/invite/{token}", data={"password": "a-long-password-1", "confirm": "a-long-password-1"})
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(self.client.get("/company/employees").status_code, 200)

    def test_a_role_from_the_other_plane_is_refused(self):
        self._login("admin@contoso.com")
        for role in (BUREAU_ADMIN, "payrolla_admin", "admin", ""):
            resp = self.client.post("/company/team/invite", data={
                "invite_name": "Esi", "invite_email": "esi@contoso.com", "invite_role": role})
            self.assertEqual(resp.status_code, 400, role)
        self.assertIsNone(User.query.filter_by(email="esi@contoso.com").first())
        self.assertEqual(self.outbox.sent, [])

    def test_an_email_already_in_use_creates_nothing(self):
        self._login("admin@contoso.com")
        resp = self.client.post("/company/team/invite", data={
            "invite_name": "Someone", "invite_email": "admin@acme.com", "invite_role": CLIENT_PREPARER})
        self.assertEqual(resp.status_code, 400)
        self.assertIn("already signs in", resp.get_data(as_text=True))
        self.assertEqual(self._user("admin@acme.com").client_company_id, self._company("Acme Manufacturing Ltd").id)

    def test_changing_a_role(self):
        self._login("admin@contoso.com")
        payroll = self._user("payroll@contoso.com")
        self.client.post(f"/company/team/{payroll.id}/role", data={"role": CLIENT_ADMIN})
        self.assertEqual(self._user("payroll@contoso.com").role, CLIENT_ADMIN)
        self.assertTrue(AuditTrail.query.filter_by(action="Team member role changed").count())
        # A role from the other plane changes nothing.
        self.client.post(f"/company/team/{payroll.id}/role", data={"role": BUREAU_ADMIN})
        self.assertEqual(self._user("payroll@contoso.com").role, CLIENT_ADMIN)

    def test_nobody_can_change_or_remove_themselves(self):
        self._login("admin@contoso.com")
        me = self._user("admin@contoso.com")
        self.client.post(f"/company/team/{me.id}/role", data={"role": CLIENT_PREPARER})
        self.client.post(f"/company/team/{me.id}/remove")
        me = self._user("admin@contoso.com")
        self.assertEqual((me.role, me.disabled_at), (CLIENT_ADMIN, None))

    def test_another_companys_login_is_a_404(self):
        self._login("admin@contoso.com")
        acme = self._user("payroll@acme.com")
        for action in ("remove", "restore", "resend"):
            self.assertEqual(self.client.post(f"/company/team/{acme.id}/{action}").status_code, 404)
        self.assertEqual(self.client.post(f"/company/team/{acme.id}/role", data={"role": CLIENT_ADMIN}).status_code, 404)
        acme = self._user("payroll@acme.com")
        self.assertEqual((acme.role, acme.disabled_at), (CLIENT_PREPARER, None))

    def test_removing_stops_sign_in_and_restoring_brings_it_back(self):
        self._login("admin@contoso.com")
        payroll = self._user("payroll@contoso.com")
        self.client.post(f"/company/team/{payroll.id}/remove")
        self.assertIsNotNone(self._user("payroll@contoso.com").disabled_at)
        self.assertTrue(AuditTrail.query.filter_by(action="Team member removed").count())

        resp = self._login("payroll@contoso.com", expect=200)
        self.assertIn("removed from its team", resp.get_data(as_text=True))
        # A wrong password still gets the ordinary answer: nothing is revealed
        # to someone who doesn't know the password.
        resp = self._login("payroll@contoso.com", password="wrong-password", expect=200)
        self.assertIn("Invalid email or password", resp.get_data(as_text=True))

        self._login("admin@contoso.com")
        self.client.post(f"/company/team/{payroll.id}/restore")
        self.assertIsNone(self._user("payroll@contoso.com").disabled_at)
        self._login("payroll@contoso.com")

    def test_removal_ends_an_open_session(self):
        self._login("payroll@contoso.com")
        self.assertEqual(self.client.get("/company/employees").status_code, 200)
        payroll = self._user("payroll@contoso.com")
        payroll.disabled_at = db.func.now()
        db.session.commit()
        resp = self.client.get("/company/employees")
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/login", resp.headers["Location"])

    def test_removing_a_pending_invite_kills_its_link_and_resend(self):
        self._login("admin@contoso.com")
        self.client.post("/company/team/invite", data={
            "invite_name": "Yaw", "invite_email": "yaw@contoso.com", "invite_role": CLIENT_PREPARER})
        yaw = self._user("yaw@contoso.com")
        from app.invites import make_token

        token = make_token(yaw)
        self.client.post(f"/company/team/{yaw.id}/remove")
        self.client.post(f"/company/team/{yaw.id}/resend")
        self.assertEqual(len(self.outbox.sent), 1)  # the original invite only
        self.client.get("/logout")
        self.assertEqual(self.client.get(f"/invite/{token}").status_code, 410)

    def test_resend_sends_a_fresh_link(self):
        self._login("admin@contoso.com")
        self.client.post("/company/team/invite", data={
            "invite_name": "Abena", "invite_email": "abena@contoso.com", "invite_role": CLIENT_ADMIN})
        abena = self._user("abena@contoso.com")
        self.client.post(f"/company/team/{abena.id}/resend")
        self.assertEqual(len(self.outbox.sent), 2)
        self.assertIn("as an administrator", self.outbox.sent[1].body_text)


class BureauTeamTests(TeamTestCase):
    """bureau_admin manages the bureau's staff in the operator interface."""

    def setUp(self):
        super().setUp()
        # Contoso becomes a bureau the way Chrisnat did: admin and finance become
        # bureau admins, payroll becomes a bureau officer.
        self._login("admin@payrolla.com")
        self.client.post(f"/clients/{self._company('Contoso Ltd').id}/make-bureau")
        self.client.get("/logout")
        self.bureau_id = self._user("admin@contoso.com").bureau_id
        self.assertIsNotNone(self.bureau_id)

    def test_bureau_admin_sees_the_team_and_the_menu_link(self):
        self._login("admin@contoso.com")
        page = self.client.get("/team")
        self.assertEqual(page.status_code, 200)
        html = page.get_data(as_text=True)
        self.assertIn("payroll@contoso.com", html)
        self.assertIn("Officer", html)
        self.assertNotIn("admin@payrolla.com", html)
        self.assertIn('href="/team"', self.client.get("/dashboard").get_data(as_text=True))

    def test_an_officer_and_payrolla_staff_have_no_team_page(self):
        self._login("payroll@contoso.com")
        self.assertNotEqual(self.client.get("/team").status_code, 200)
        # Payrolla's staff: legacy "admin" fails the role guard (a redirect); the
        # superusers pass it and get the 404 for having no bureau.
        for email in ("admin@payrolla.com", "director@payrolla.com"):
            self._login(email)
            self.assertIn(self.client.get("/team").status_code, (302, 404), email)
            self.assertNotIn('href="/team"', self.client.get("/dashboard").get_data(as_text=True))

    def test_inviting_an_officer(self):
        self._login("admin@contoso.com")
        resp = self.client.post("/team/invite", data={
            "invite_name": "Efua Nyarko", "invite_email": "efua@contoso.com", "invite_role": BUREAU_OFFICER})
        self.assertEqual(resp.status_code, 302)
        efua = self._user("efua@contoso.com")
        self.assertEqual((efua.role, efua.bureau_id, efua.client_company_id), (BUREAU_OFFICER, self.bureau_id, None))
        self.assertIn("as an officer", self.outbox.sent[0].body_text)

    def test_a_company_role_is_refused(self):
        self._login("admin@contoso.com")
        resp = self.client.post("/team/invite", data={
            "invite_name": "Esi", "invite_email": "esi@contoso.com", "invite_role": CLIENT_ADMIN})
        self.assertEqual(resp.status_code, 400)
        self.assertIsNone(User.query.filter_by(email="esi@contoso.com").first())

    def test_another_bureaus_staff_and_payrolla_staff_are_out_of_reach(self):
        self._login("admin@payrolla.com")
        self.client.post(f"/clients/{self._company('Acme Manufacturing Ltd').id}/make-bureau")
        acme = self._user("payroll@acme.com")
        staff = self._user("operator@payrolla.com")
        self._login("admin@contoso.com")
        for target in (acme, staff):
            self.assertEqual(self.client.post(f"/team/{target.id}/remove").status_code, 404)
            self.assertEqual(self.client.post(f"/team/{target.id}/role", data={"role": BUREAU_ADMIN}).status_code, 404)
        self.assertIsNone(self._user("payroll@acme.com").disabled_at)
        self.assertIsNone(self._user("operator@payrolla.com").disabled_at)

    def test_promote_then_remove_an_officer(self):
        self._login("admin@contoso.com")
        officer = self._user("payroll@contoso.com")
        self.client.post(f"/team/{officer.id}/role", data={"role": BUREAU_ADMIN})
        self.assertEqual(self._user("payroll@contoso.com").role, BUREAU_ADMIN)
        self.client.post(f"/team/{officer.id}/remove")
        self.assertIsNotNone(self._user("payroll@contoso.com").disabled_at)
        resp = self._login("payroll@contoso.com", expect=200)
        self.assertIn("removed from its team", resp.get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()
