"""Operations & Onboarding, Phase 3 — professional login identities.

The demo roster is now enterprise-shaped: platform staff on @payrolla.com and
client staff on their own company domain. The load-bearing claim is that ONLY
identities changed — every role string, and therefore every permission check,
is exactly what it was. These tests pin both halves of that claim.
"""

import os
import unittest

os.environ["SKIP_DOTENV"] = "true"
os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["SEED_DEMO_DATA"] = "true"
os.environ["PERSISTENCE_REQUIRED"] = "false"

from app import create_app  # noqa: E402
from app.models import ClientCompany, User  # noqa: E402
from app.roles import (  # noqa: E402
    CLIENT_ADMIN,
    CLIENT_PREPARER,
    PLATFORM_ROLES,
    TENANT_ROLES,
    is_platform_user,
    is_tenant_user,
)
from app.seed import DEMO_COMPANIES, PLATFORM_USERS  # noqa: E402

EXPECTED_PLATFORM = {
    "admin@payrolla.com": "admin",
    "operator@payrolla.com": "payrolla_admin",
    "support@payrolla.com": "payrolla_reviewer",
    "director@payrolla.com": "md",
    "payroll@payrolla.com": "payroll_officer",
    "accounts@payrolla.com": "accounts_officer",
    "operations@payrolla.com": "operations_supervisor",
}

EXPECTED_TENANT = {
    "admin@msc.com": ("MSC Limited", CLIENT_ADMIN),
    "finance@msc.com": ("MSC Limited", CLIENT_ADMIN),
    "payroll@msc.com": ("MSC Limited", CLIENT_PREPARER),
    "admin@acme.com": ("Acme Manufacturing Ltd", CLIENT_ADMIN),
    "finance@acme.com": ("Acme Manufacturing Ltd", CLIENT_ADMIN),
    "payroll@acme.com": ("Acme Manufacturing Ltd", CLIENT_PREPARER),
}


class SeedIdentityTestCase(unittest.TestCase):
    def setUp(self):
        self.app = create_app()
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()
        self.ctx = self.app.app_context()
        self.ctx.push()

    def tearDown(self):
        self.ctx.pop()

    # --- platform plane ------------------------------------------------------
    def test_platform_roster_is_professional_and_role_preserving(self):
        for email, role in EXPECTED_PLATFORM.items():
            with self.subTest(email=email):
                user = User.query.filter_by(email=email).first()
                self.assertIsNotNone(user, f"{email} should be seeded")
                self.assertEqual(user.role, role)
                self.assertIn(role, PLATFORM_ROLES)
                self.assertIsNone(user.client_company_id)
                self.assertTrue(is_platform_user(user))

    def test_every_platform_role_still_has_a_login(self):
        seeded_roles = {role for _n, _e, role in PLATFORM_USERS}
        self.assertEqual(seeded_roles, set(EXPECTED_PLATFORM.values()))
        # Nothing in the legacy platform vocabulary silently lost its account
        # except 'viewer', which never had one.
        self.assertEqual(PLATFORM_ROLES - seeded_roles, {"viewer"})

    # --- tenant plane --------------------------------------------------------
    def test_tenant_roster_is_bound_to_its_own_company(self):
        for email, (company_name, role) in EXPECTED_TENANT.items():
            with self.subTest(email=email):
                user = User.query.filter_by(email=email).first()
                self.assertIsNotNone(user, f"{email} should be seeded")
                self.assertEqual(user.role, role)
                self.assertIn(role, TENANT_ROLES)
                self.assertTrue(is_tenant_user(user))
                company = ClientCompany.query.filter_by(name=company_name).first()
                self.assertIsNotNone(company)
                self.assertEqual(user.client_company_id, company.id)

    def test_demo_companies_are_exactly_the_two_professional_tenants(self):
        names = {c.name for c in ClientCompany.query.all()}
        self.assertEqual(names, {"MSC Limited", "Acme Manufacturing Ltd"})
        codes = {c.company_code for c in ClientCompany.query.all()}
        self.assertEqual(codes, {"MSC", "ACME"})
        for spec in DEMO_COMPANIES:
            company = ClientCompany.query.filter_by(name=spec["name"]).first()
            self.assertTrue(company.address, f"{spec['name']} should carry an address")
            self.assertTrue(company.email, f"{spec['name']} should carry an email")

    # --- obsolete identities are gone ---------------------------------------
    def test_no_demo_flavoured_identity_survives(self):
        for user in User.query.all():
            with self.subTest(email=user.email):
                self.assertNotIn(".local", user.email)
                self.assertNotIn(".demo", user.email)
                self.assertNotEqual(user.role, "client_user")

    # --- the accounts actually work -----------------------------------------
    def test_each_seeded_identity_can_sign_in_to_its_own_plane(self):
        for email in list(EXPECTED_PLATFORM) + list(EXPECTED_TENANT):
            with self.subTest(email=email):
                resp = self.client.post(
                    "/login", data={"email": email, "password": "password123"}
                )
                self.assertEqual(resp.status_code, 302)
                expected = "/company" if email in EXPECTED_TENANT else "/dashboard"
                self.assertTrue(
                    resp.headers["Location"].endswith(expected),
                    f"{email} landed on {resp.headers['Location']}",
                )
                self.client.get("/logout")

    # --- login hints ---------------------------------------------------------
    def test_login_page_hints_list_the_seeded_accounts(self):
        self.app.config["SHOW_DEMO_LOGINS"] = True
        body = self.client.get("/login").get_data(as_text=True)
        self.assertIn("Demo sign-in accounts", body)
        for email in ("admin@payrolla.com", "admin@msc.com", "payroll@acme.com"):
            self.assertIn(email, body)

    def test_login_hints_are_suppressed_when_disabled(self):
        self.app.config["SHOW_DEMO_LOGINS"] = False
        body = self.client.get("/login").get_data(as_text=True)
        self.assertNotIn("Demo sign-in accounts", body)
        self.assertNotIn("admin@payrolla.com", body)


if __name__ == "__main__":
    unittest.main()
