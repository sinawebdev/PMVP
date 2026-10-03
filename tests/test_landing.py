"""Public landing page: the desktop download button and the animated mark."""
import os
import re
import unittest
from unittest import mock

os.environ["SKIP_DOTENV"] = "true"
os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["PERSISTENCE_REQUIRED"] = "false"

from app import create_app, https_url_or_none  # noqa: E402

BUTTON = "Download for Windows"


def landing_html(download_url=None):
    """Render / for an anonymous visitor with DESKTOP_DOWNLOAD_URL as given."""
    with mock.patch.dict(os.environ):
        os.environ.pop("DESKTOP_DOWNLOAD_URL", None)
        if download_url is not None:
            os.environ["DESKTOP_DOWNLOAD_URL"] = download_url
        app = create_app()
    response = app.test_client().get("/")
    assert response.status_code == 200, response.status_code
    return app, response.get_data(as_text=True)


class DesktopDownloadButtonTests(unittest.TestCase):
    """The installer is hosted outside the app, so the button exists only when
    DESKTOP_DOWNLOAD_URL names somewhere real -- and the value lands in an href
    on a public page, so only an absolute https:// URL ever renders."""

    def test_no_url_no_button(self):
        _, html = landing_html()
        self.assertNotIn(BUTTON, html)

    def test_button_links_to_the_configured_url(self):
        url = "https://downloads.example.com/Payrolla-Desktop-Setup.exe"
        _, html = landing_html(url)
        self.assertIn(BUTTON, html)
        self.assertIn(f'href="{url}"', html)

    def test_anything_but_absolute_https_renders_no_button(self):
        for value in ("javascript:alert(1)", "http://downloads.example.com/setup.exe",
                      "downloads.example.com/setup.exe", "https://", "https:///setup.exe",
                      "https://example.com/a b.exe", "   "):
            with self.subTest(value=value):
                _, html = landing_html(value)
                self.assertNotIn(BUTTON, html)
                self.assertNotIn(f'href="{value}"', html)

    def test_https_url_or_none(self):
        self.assertEqual(https_url_or_none("  https://x.example/s.exe "), "https://x.example/s.exe")
        self.assertEqual(https_url_or_none("HTTPS://x.example/s.exe"), "HTTPS://x.example/s.exe")
        self.assertIsNone(https_url_or_none(None))
        self.assertIsNone(https_url_or_none("ftp://x.example/s.exe"))


class AnimatedMarkTests(unittest.TestCase):
    """The hero's animated mark is a set of built files (scripts/logo/). A
    template pointing at one that is not there fails quietly in the browser --
    the player falls back to the still, or the still is a broken image -- so
    check the references here instead."""

    def test_every_file_the_hero_mark_uses_exists(self):
        app, html = landing_html()
        hero = html[html.index('class="hero-mark"'):]
        hero = hero[:hero.index("</picture>")]
        urls = re.findall(r'(?:data-video-(?:lg|sm)|srcset|src)="(/static/[^"]+)"', hero)
        urls.append(re.search(r'src="(/static/logo-player\.js[^"]*)"', html).group(1))
        self.assertGreaterEqual(len(urls), 5, urls)
        for url in urls:
            with self.subTest(url=url):
                rel = url.split("?")[0][len("/static/"):]
                self.assertTrue(os.path.isfile(os.path.join(app.static_folder, rel)), url)

    def test_the_still_is_decorative_and_hidden_from_assistive_tech(self):
        _, html = landing_html()
        self.assertRegex(html, r'class="hero-mark" aria-hidden="true"')
        self.assertRegex(html, r'class="hero-mark-still"[^>]*alt=""')


if __name__ == "__main__":
    unittest.main()
