"""Public legal pages: privacy policy, terms of service, data protection."""
import os
import unittest
from unittest import mock

os.environ["SKIP_DOTENV"] = "true"
os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["PERSISTENCE_REQUIRED"] = "false"

from app import create_app  # noqa: E402

PAGES = {
    "/privacy": "Privacy policy",
    "/terms": "Terms of service",
    "/data-protection": "Data protection and retention",
}


def get(path, **env):
    """GET path as an anonymous visitor, with extra environment variables set."""
    with mock.patch.dict(os.environ, env):
        app = create_app()
    response = app.test_client().get(path)
    return response.status_code, response.get_data(as_text=True)


class LegalPageTests(unittest.TestCase):
    def test_each_page_is_public(self):
        for path, title in PAGES.items():
            with self.subTest(path=path):
                status, html = get(path)
                self.assertEqual(status, 200)
                self.assertIn(f"<h1>{title}</h1>", html)
                self.assertIn("Sinaforte Technologies", html)

    def test_each_page_names_itself_as_canonical_on_the_live_domain(self):
        for path in PAGES:
            with self.subTest(path=path):
                _, html = get(path)
                self.assertIn(
                    f'<link rel="canonical" href="https://payrollagh.com{path}">', html
                )

    def test_each_page_links_to_the_others(self):
        for path in PAGES:
            with self.subTest(path=path):
                _, html = get(path)
                for other in PAGES:
                    self.assertIn(f'href="{other}"', html)
                self.assertIn(f'href="{path}" aria-current="page"', html)

    def test_contact_address_comes_from_config(self):
        _, html = get("/privacy")
        self.assertIn("mailto:support@payrollagh.com", html)
        _, html = get("/privacy", SUPPORT_EMAIL="privacy@example.com")
        self.assertIn("mailto:privacy@example.com", html)
        self.assertNotIn("support@payrollagh.com", html)

    def test_retention_promise_matches_across_pages(self):
        # The 90-day deletion window is a promise to clients; the three pages
        # must not drift apart on it.
        for path in PAGES:
            with self.subTest(path=path):
                _, html = get(path)
                self.assertIn("90 days", html)

    def test_company_address_is_shown_only_once_it_is_set(self):
        _, html = get("/privacy")
        self.assertNotIn(", at ", html)
        _, html = get("/privacy", COMPANY_ADDRESS="12 Example Road, Koforidua")
        self.assertIn("at 12 Example Road, Koforidua", html)

    def test_pages_do_not_overstate_what_we_do(self):
        # Each of these was on a live page and was not true, or could not be
        # backed up. A claim comes back only with the code or paperwork behind it.
        unbacked = (
            "72 hours",
            "every change",
            "every upload",
            "commit by contract",
            "in line with the data protection principles",
            "at least once a year",
            "WhatsApp",
            "dataprotection.org.gh",
        )
        for path in PAGES:
            _, html = get(path)
            for phrase in unbacked:
                with self.subTest(path=path, phrase=phrase):
                    self.assertNotIn(phrase, html.replace("Every", "every"))


class LandingLinksTests(unittest.TestCase):
    def test_landing_and_login_do_not_advertise_a_channel_that_is_not_live(self):
        for path in ("/", "/login"):
            with self.subTest(path=path):
                _, html = get(path)
                self.assertNotIn("WhatsApp", html)

    def test_landing_does_not_claim_every_edit_is_logged(self):
        _, html = get("/")
        self.assertNotIn("Every upload, edit", html)


    def test_footer_links_to_every_legal_page(self):
        _, html = get("/")
        footer = html[html.index('<footer class="site-footer">'):]
        for path in PAGES:
            self.assertIn(f'href="{path}"', footer)

    def test_access_request_form_points_to_the_privacy_policy(self):
        _, html = get("/")
        form = html[html.index('class="request-form"'):html.index("</form>")]
        self.assertIn('href="/privacy"', form)


if __name__ == "__main__":
    unittest.main()
