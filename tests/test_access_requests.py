"""Landing-page access requests -> operator approval -> client admin invite.

The path a new customer takes end to end: they apply on the landing page, the
request shows on the operator's Clients page, approving it prefills and links a
new company, and the onboarding page invites the contact, whose emailed link
sets a password exactly once.
"""

import os
import re
import unittest
from unittest import mock

os.environ["SKIP_DOTENV"] = "true"
os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["PERSISTENCE_REQUIRED"] = "false"

from app import access_requests, create_app, db  # noqa: E402
from app.distribution.channels import SendResult  # noqa: E402
from app.models import AccessRequest, ClientCompany, Notification, User  # noqa: E402


def _request_form(**overrides):
    data = {
        "company_name": "Volta Logistics",
        "contact_name": "Kofi Asante",
        "email": "Kofi@VoltaLogistics.com",
        "phone": "024 555 0101",
        "location": "Tema",
        "employee_band": "11-50",
        "interest": "both",
        "message": "We pay weekly.",
    }
    data.update(overrides)
    return data


class AccessRequestTestCase(unittest.TestCase):
    def setUp(self):
        self.app = create_app()
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()
        self.ctx = self.app.app_context()
        self.ctx.push()
        access_requests.reset()

    def tearDown(self):
        access_requests.reset()
        self.ctx.pop()

    def _login_operator(self):
        admin = User.query.filter(User.role == "admin", User.client_company_id.is_(None)).first()
        self.client.post("/login", data={"email": admin.email, "password": "password123"})
        return admin

    # --- public form ---------------------------------------------------------
    def test_landing_carries_the_request_form(self):
        html = self.client.get("/").get_data(as_text=True)
        self.assertIn('id="request-access"', html)
        self.assertIn('action="/request-access#request-access"', html)

    def test_valid_request_is_stored_and_operators_notified(self):
        before = Notification.query.count()
        resp = self.client.post("/request-access", data=_request_form())
        self.assertEqual(resp.status_code, 302)
        self.assertIn("sent=1", resp.headers["Location"])
        entry = AccessRequest.query.filter_by(company_name="Volta Logistics").one()
        self.assertEqual(entry.email, "kofi@voltalogistics.com")
        self.assertEqual(entry.status, "new")
        self.assertGreater(Notification.query.count(), before)
        thanks = self.client.get("/?sent=1").get_data(as_text=True)
        self.assertIn("Request received.", thanks)

    def test_invalid_request_rerenders_with_values_and_saves_nothing(self):
        resp = self.client.post("/request-access", data=_request_form(email="not-an-email", employee_band="9000"))
        self.assertEqual(resp.status_code, 400)
        html = resp.get_data(as_text=True)
        self.assertIn("Enter an email address we can reply to.", html)
        self.assertIn("Choose a team size from the list.", html)
        self.assertIn('value="Volta Logistics"', html)
        self.assertEqual(AccessRequest.query.count(), 0)

    def test_honeypot_looks_successful_but_stores_nothing(self):
        resp = self.client.post("/request-access", data=_request_form(website="http://spam.example"))
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(AccessRequest.query.count(), 0)

    def test_per_ip_cap(self):
        self.app.config["ACCESS_REQUEST_MAX_PER_HOUR"] = 2
        codes = [self.client.post("/request-access", data=_request_form(company_name=f"Co {i}")).status_code
                 for i in range(3)]
        self.assertEqual(codes, [302, 302, 429])
        self.assertEqual(AccessRequest.query.count(), 2)

    # --- operator review -----------------------------------------------------
    def test_open_request_shows_on_clients_and_approval_links_company(self):
        self.client.post("/request-access", data=_request_form())
        entry = AccessRequest.query.one()
        self._login_operator()

        clients = self.client.get("/clients").get_data(as_text=True)
        self.assertIn("Applications", clients)
        self.assertIn("Volta Logistics", clients)

        prefilled = self.client.get(f"/clients/add?request={entry.id}").get_data(as_text=True)
        self.assertIn('value="Volta Logistics"', prefilled)
        self.assertIn(f'name="request" value="{entry.id}"', prefilled)

        resp = self.client.post("/clients/add", data={
            "request": entry.id, "name": "Volta Logistics", "company_code": "VOLTA",
            "contact_person": "Kofi Asante", "email": "kofi@voltalogistics.com", "status": "Active",
        })
        self.assertEqual(resp.status_code, 302)
        company = ClientCompany.query.filter_by(company_code="VOLTA").one()
        db.session.refresh(entry)
        self.assertEqual(entry.status, "approved")
        self.assertEqual(entry.client_company_id, company.id)
        # Decided requests leave the Applications list.
        self.assertNotIn("Applications", self.client.get("/clients").get_data(as_text=True))
        # The onboarding page prefills the invite from the request.
        onboarding = self.client.get(f"/clients/{company.id}/onboarding").get_data(as_text=True)
        self.assertIn('value="kofi@voltalogistics.com"', onboarding)

    def test_decline_records_reason(self):
        self.client.post("/request-access", data=_request_form())
        entry = AccessRequest.query.one()
        self._login_operator()
        self.client.post(f"/clients/requests/{entry.id}/decline", data={"reason": "Duplicate"})
        db.session.refresh(entry)
        self.assertEqual(entry.status, "declined")
        self.assertEqual(entry.decline_reason, "Duplicate")

    def test_tenant_users_cannot_review_requests(self):
        self.client.post("/request-access", data=_request_form())
        entry = AccessRequest.query.one()
        self.client.post("/login", data={"email": "admin@msc.com", "password": "password123"})
        resp = self.client.get(f"/clients/requests/{entry.id}")
        self.assertNotEqual(resp.status_code, 200)


class InviteTestCase(unittest.TestCase):
    def setUp(self):
        self.app = create_app()
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()
        self.ctx = self.app.app_context()
        self.ctx.push()
        self.company = ClientCompany(name="Invite Co", company_code="INVCO", status="Active")
        db.session.add(self.company)
        db.session.commit()
        admin = User.query.filter(User.role == "admin", User.client_company_id.is_(None)).first()
        self.client.post("/login", data={"email": admin.email, "password": "password123"})
        self.sent = []

    def tearDown(self):
        self.ctx.pop()

    def _sender(self):
        outer = self

        class Capture:
            def send(self, message):
                outer.sent.append(message)
                return SendResult(ok=True, provider="test")

        return mock.patch("app.invites.get_sender", return_value=Capture())

    def _invite(self, email="ama@inviteco.test"):
        with self._sender():
            return self.client.post(f"/clients/{self.company.id}/invite",
                                    data={"invite_name": "Ama Owusu", "invite_email": email})

    def _link(self):
        return re.search(r"(/invite/[^\s]+)", self.sent[-1].body_text).group(1)

    def test_invite_creates_pending_client_admin_and_emails_link(self):
        self.assertTrue(self.company.awaiting_credentials)
        resp = self._invite()
        self.assertEqual(resp.status_code, 302)
        user = User.query.filter_by(email="ama@inviteco.test").one()
        self.assertEqual(user.role, "client_admin")
        self.assertEqual(user.client_company_id, self.company.id)
        self.assertTrue(user.invite_pending)
        self.assertFalse(self.company.awaiting_credentials)
        self.assertEqual(len(self.sent), 1)
        self.assertIn("/invite/", self.sent[0].body_text)

    def test_duplicate_email_is_rejected(self):
        resp = self._invite(email="admin@msc.com")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("Someone already signs in with this email.", resp.get_data(as_text=True))

    def test_link_sets_password_once_and_signs_in(self):
        self._invite()
        link = self._link()
        self.client.get("/logout")
        short = self.client.post(link, data={"password": "short", "confirm": "short"})
        self.assertEqual(short.status_code, 400)
        ok = self.client.post(link, data={"password": "a-long-pass-123", "confirm": "a-long-pass-123"})
        self.assertEqual(ok.status_code, 302)
        user = User.query.filter_by(email="ama@inviteco.test").one()
        self.assertFalse(user.invite_pending)
        self.assertTrue(user.check_password("a-long-pass-123"))
        # Used once: the same link is dead now.
        self.client.get("/logout")
        self.assertEqual(self.client.get(link).status_code, 410)

    def test_resend_invalidates_the_earlier_link(self):
        self._invite()
        first = self._link()
        user = User.query.filter_by(email="ama@inviteco.test").one()
        with self._sender():
            self.client.post(f"/clients/{self.company.id}/invite/{user.id}/resend")
        second = self._link()
        self.assertNotEqual(first, second)
        self.client.get("/logout")
        self.assertEqual(self.client.get(first).status_code, 410)
        self.assertEqual(self.client.get(second).status_code, 200)

    def test_expired_link_is_refused(self):
        self._invite()
        link = self._link()
        self.client.get("/logout")
        with mock.patch("app.invites.INVITE_MAX_AGE", -1):
            self.assertEqual(self.client.get(link).status_code, 410)


if __name__ == "__main__":
    unittest.main()
