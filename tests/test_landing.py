"""Public landing page: the animated mark."""
import os
import re
import unittest

os.environ["SKIP_DOTENV"] = "true"
os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["PERSISTENCE_REQUIRED"] = "false"

from app import create_app  # noqa: E402


def landing_html():
    """Render / for an anonymous visitor."""
    app = create_app()
    response = app.test_client().get("/")
    assert response.status_code == 200, response.status_code
    return app, response.get_data(as_text=True)


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
