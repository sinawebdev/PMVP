"""/desktop/latest.json: what installed desktop copies check for updates.

The desktop app compares its own version with the one here and shows a
download notice when it is behind. Until a release is configured the route is
a 404, which the desktop treats as "no update". Bad settings are dropped rather
than published, because a malformed version would make every install either
nag forever or never notice a real release.
"""

import os
import unittest
from unittest import mock

os.environ["SKIP_DOTENV"] = "true"
os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["PERSISTENCE_REQUIRED"] = "false"

from app import create_app  # noqa: E402
from app.desktop_release import release_config  # noqa: E402

URL = "https://github.com/sinawebdev/PMVP/releases/download/desktop-v0.2.0/Payrolla-Desktop-Setup.exe"
SHA = "7b84dd84" + "0" * 56


def _client(**env):
    keys = ("DESKTOP_DOWNLOAD_URL", "DESKTOP_LATEST_VERSION", "DESKTOP_DOWNLOAD_SHA256",
            "DESKTOP_RELEASED_ON", "DESKTOP_RELEASE_NOTES")
    clean = {k: "" for k in keys}
    clean.update(env)
    with mock.patch.dict(os.environ, clean):
        app = create_app()
    app.config["TESTING"] = True
    return app.test_client()


class LatestManifestTests(unittest.TestCase):
    def test_unconfigured_is_404(self):
        self.assertEqual(_client().get("/desktop/latest.json").status_code, 404)
        self.assertEqual(
            _client(DESKTOP_DOWNLOAD_URL=URL).get("/desktop/latest.json").status_code, 404,
            "a download URL without a version is not a release",
        )

    def test_configured_release_is_published_without_signing_in(self):
        resp = _client(
            DESKTOP_DOWNLOAD_URL=URL, DESKTOP_LATEST_VERSION="0.2.0",
            DESKTOP_DOWNLOAD_SHA256=SHA.upper(), DESKTOP_RELEASED_ON="2026-11-12",
            DESKTOP_RELEASE_NOTES="Bulk employee import and editable rates.",
        ).get("/desktop/latest.json")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json(), {
            "version": "0.2.0", "download_url": URL, "sha256": SHA,
            "released_on": "2026-11-12", "notes": "Bulk employee import and editable rates.",
        })
        self.assertIn("max-age=300", resp.headers["Cache-Control"])

    def test_bad_values_are_dropped_not_published(self):
        config = release_config({
            "DESKTOP_LATEST_VERSION": "v0.2", "DESKTOP_DOWNLOAD_SHA256": "abc",
            "DESKTOP_RELEASED_ON": "12/11/2026", "DESKTOP_RELEASE_NOTES": "  a \n  b  ",
        })
        self.assertIsNone(config["DESKTOP_LATEST_VERSION"])
        self.assertIsNone(config["DESKTOP_DOWNLOAD_SHA256"])
        self.assertIsNone(config["DESKTOP_RELEASED_ON"])
        self.assertEqual(config["DESKTOP_RELEASE_NOTES"], "a b")
        resp = _client(DESKTOP_DOWNLOAD_URL=URL, DESKTOP_LATEST_VERSION="latest").get("/desktop/latest.json")
        self.assertEqual(resp.status_code, 404)

    def test_an_http_download_url_never_reaches_the_manifest(self):
        resp = _client(DESKTOP_DOWNLOAD_URL=URL.replace("https", "http"),
                       DESKTOP_LATEST_VERSION="0.2.0").get("/desktop/latest.json")
        self.assertEqual(resp.status_code, 404)


if __name__ == "__main__":
    unittest.main()
