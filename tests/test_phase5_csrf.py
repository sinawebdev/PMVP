"""Phase 5 — CSRF enforcement (Workstream D1).

The suite globally disables CSRF (tests/__init__.py) so the existing route tests
can POST without a token; this module re-enables it on its own app instance to
prove enforcement is real and that the provider webhooks are exempt.
"""
import os
import re
import unittest

os.environ["SKIP_DOTENV"] = "true"
os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["SEED_DEMO_DATA"] = "true"
os.environ["PERSISTENCE_REQUIRED"] = "false"

from app import create_app  # noqa: E402


class CsrfEnforcementTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app()
        # Re-enable CSRF (the suite disables it globally via env).
        self.app.config["WTF_CSRF_ENABLED"] = True
        self.client = self.app.test_client()

    def _csrf_token(self):
        # The public login page renders the session token into the meta tag.
        html = self.client.get("/login").get_data(as_text=True)
        m = re.search(r'name="csrf-token" content="([^"]+)"', html)
        self.assertIsNotNone(m, "csrf-token meta tag not rendered")
        return m.group(1)

    def test_post_without_token_is_rejected(self):
        resp = self.client.post("/login", data={"email": "a@b.c", "password": "x"})
        self.assertEqual(resp.status_code, 400)

    def test_post_with_form_token_passes_csrf(self):
        token = self._csrf_token()
        resp = self.client.post("/login", data={
            "email": "admin@chrisnat.local", "password": "password123",
            "csrf_token": token,
        })
        self.assertNotEqual(resp.status_code, 400, "valid form token should pass CSRF")

    def test_post_with_header_token_passes_csrf(self):
        token = self._csrf_token()
        resp = self.client.post(
            "/login",
            data={"email": "admin@chrisnat.local", "password": "password123"},
            headers={"X-CSRFToken": token},
        )
        self.assertNotEqual(resp.status_code, 400, "valid header token should pass CSRF")

    def test_provider_webhook_is_csrf_exempt(self):
        # Configured webhook: a POST with no CSRF token must not be blocked by CSRF
        # (it is gated by signature instead -> 403, never a 400 CSRF failure).
        self.app.config["WHATSAPP_VERIFY_TOKEN"] = "verify-me"
        self.app.config["WHATSAPP_APP_SECRET"] = "s3cr3t"
        resp = self.client.post("/distribution/webhooks/whatsapp", json={})
        self.assertNotEqual(resp.status_code, 400)
        self.assertEqual(resp.status_code, 403)  # signature check, not CSRF


if __name__ == "__main__":
    unittest.main()
