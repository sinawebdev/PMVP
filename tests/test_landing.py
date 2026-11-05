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


class HeroAndFeaturesTests(unittest.TestCase):
    """The hero copy, its Montserrat face and the features bento."""

    def test_hero_heading_carries_the_search_words_and_the_slogan_follows(self):
        _, html = landing_html()
        self.assertEqual(html.count("<h1"), 1)
        h1 = re.search(r'<div class="hero-intro"><h1 id="hero-title">(.*?)</h1>', html, re.S).group(1)
        self.assertIn("Payroll software for Ghana", h1)
        display = re.search(r'<p class="hero-display">(.*?)</p>', html, re.S).group(1)
        self.assertIn("Payday, sorted.", display)
        self.assertIn('class="hero-accent">Every cedi, every payslip.</span>', display)
        self.assertEqual(html.count('<ul class="hero-facts">'), 1)

    def test_montserrat_is_preloaded_and_every_face_file_exists(self):
        app, html = landing_html()
        preload = re.search(
            r'<link rel="preload" href="(/static/fonts/montserrat-[^"]+)" as="font" type="font/woff2" crossorigin>',
            html,
        )
        self.assertIsNotNone(preload, "Montserrat preload link missing")
        with open(os.path.join(app.static_folder, "fonts.css"), encoding="utf-8") as handle:
            faces = re.findall(r"font-family:Montserrat;src:url\('([^']+)'\)", handle.read())
        self.assertEqual(len(faces), 2, faces)
        for rel in faces + [preload.group(1)[len("/static/"):]]:
            with self.subTest(font=rel):
                path = os.path.join(app.static_folder, rel)
                self.assertTrue(os.path.isfile(path), rel)
                with open(path, "rb") as font:
                    self.assertEqual(font.read(4), b"wOF2", rel)

    def test_features_render_six_cards_with_decorative_illustrations(self):
        _, html = landing_html()
        section = html[html.index('id="features"'):]
        section = section[:section.index("</section>")]
        self.assertEqual(section.count('<article class="bento-card'), 6)
        self.assertEqual(section.count("<h3>"), 6)
        for mini in ("mini-slip", "mini-flags", "mini-channels", "mini-files", "mini-fields", "mini-timeline"):
            with self.subTest(mini=mini):
                self.assertRegex(section, rf'class="{mini}" aria-hidden="true"')


class SearchAndSharingTests(unittest.TestCase):
    """robots.txt, sitemap.xml, favicon.ico, and the landing page's canonical,
    Open Graph, Twitter and JSON-LD tags. Every absolute URL uses SITE_URL,
    never the request host (which arrives as http:// behind Render)."""

    SITE = "https://payrollagh.com"

    def setUp(self):
        self.app, self.html = landing_html()
        self.client = self.app.test_client()

    def _meta(self, attr, name):
        match = re.search(rf'<meta {attr}="{re.escape(name)}" content="([^"]*)">', self.html)
        self.assertIsNotNone(match, f"{attr}={name} missing")
        return match.group(1)

    def _static_file(self, url):
        self.assertTrue(url.startswith(self.SITE + "/static/"), url)
        rel = url[len(self.SITE + "/static/"):].split("?")[0]
        return os.path.join(self.app.static_folder, rel)

    def test_site_url_defaults_to_the_live_domain(self):
        self.assertEqual(self.app.config["SITE_URL"], self.SITE)

    def test_robots_allows_crawling_and_names_the_sitemap(self):
        response = self.client.get("/robots.txt")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, "text/plain")
        body = response.get_data(as_text=True)
        self.assertIn("User-agent: *", body)
        self.assertNotIn("Disallow: /\n", body)
        self.assertIn(f"Sitemap: {self.SITE}/sitemap.xml", body)

    def test_sitemap_lists_the_landing_page_on_the_live_domain(self):
        import xml.etree.ElementTree as ET

        response = self.client.get("/sitemap.xml")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, "application/xml")
        ns = {"s": "http://www.sitemaps.org/schemas/sitemap/0.9"}
        locs = [el.text for el in ET.fromstring(response.data).findall("s:url/s:loc", ns)]
        self.assertIn(self.SITE + "/", locs)
        self.assertTrue(all(loc.startswith(self.SITE + "/") for loc in locs), locs)

    def test_favicon_ico_is_served(self):
        response = self.client.get("/favicon.ico")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data[:4], b"\x00\x00\x01\x00")  # ICO header
        response.close()

    def test_canonical_and_share_tags_point_at_the_live_domain(self):
        self.assertIn(f'<link rel="canonical" href="{self.SITE}/">', self.html)
        self.assertEqual(self._meta("property", "og:url"), self.SITE + "/")
        self.assertEqual(self._meta("name", "twitter:card"), "summary_large_image")
        self.assertIn("Ghana", self._meta("name", "description"))
        for attr, name in (("property", "og:title"), ("property", "og:description"),
                           ("name", "twitter:title"), ("name", "twitter:description")):
            with self.subTest(tag=name):
                self.assertTrue(self._meta(attr, name).strip())

    def test_share_image_exists_and_is_small_enough_for_whatsapp(self):
        image = self._meta("property", "og:image")
        self.assertEqual(self._meta("name", "twitter:image"), image)
        path = self._static_file(image)
        self.assertTrue(os.path.isfile(path), image)
        # WhatsApp drops link-preview images over roughly 300 KB.
        self.assertLess(os.path.getsize(path), 300 * 1024)

    def test_structured_data_parses_and_names_the_organization(self):
        import json

        blocks = [json.loads(raw) for raw in re.findall(
            r'<script type="application/ld\+json">(.*?)</script>', self.html, re.S)]
        graph = next(block["@graph"] for block in blocks if "@graph" in block)
        types = {node["@type"]: node for node in graph}
        self.assertEqual({"Organization", "WebSite", "SoftwareApplication"}, set(types))
        self.assertEqual(types["WebSite"]["url"], self.SITE + "/")
        self.assertTrue(os.path.isfile(self._static_file(types["Organization"]["logo"])))

    def test_icons_and_hero_slides_exist(self):
        for href in re.findall(r'<link rel="(?:icon|apple-touch-icon)"[^>]*href="(/static/[^"]+)"', self.html) \
                + re.findall(r'<img src="(/static/img/hero-[^"]+)"', self.html):
            with self.subTest(href=href):
                rel = href.split("?")[0][len("/static/"):]
                self.assertTrue(os.path.isfile(os.path.join(self.app.static_folder, rel)), href)


if __name__ == "__main__":
    unittest.main()
