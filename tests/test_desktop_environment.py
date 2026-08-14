"""The desktop environment (``APP_ENV=desktop``) and the relocatable paths it needs.

Payrolla Desktop is a packaged single-firm install: a bundled Flask process
serving 127.0.0.1 over plain HTTP against a local SQLite file. It fits neither
existing environment, and the interesting property of these tests is *which half
of each* it inherits:

  * **Not production** — it must be allowed to run on SQLite and to set a session
    cookie without ``Secure``, or it cannot boot and cannot log anyone in.
  * **Held to production's contracts** — signing keys must be supplied, not
    generated per process, and the boot-time seed must never run.

That second half is the one worth pinning. Every guard here was, before this
change, something development was silently allowed to do; on a desktop install
each is a shipped defect (known credentials in a customer's database, payslip
links that die on restart). These tests fail if desktop is ever quietly demoted
back to "development with a different name".
"""
import os
import tempfile
import unittest

os.environ["SKIP_DOTENV"] = "true"
os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["PERSISTENCE_REQUIRED"] = "false"

import app as app_module  # noqa: E402
from app import create_app  # noqa: E402


class _DesktopEnvCase(unittest.TestCase):
    """Saves and restores every environment variable these tests rewrite."""

    ENV_KEYS = (
        "APP_ENV", "FLASK_ENV", "RENDER", "RAILWAY_ENVIRONMENT",
        "SECRET_KEY", "PAYSLIP_TOKEN_KEY", "AUTO_INIT_DB", "SEED_DEMO_DATA",
        "DATABASE_URL", "PERSISTENCE_REQUIRED",
        "PAYROLLA_INSTANCE_PATH", "EXPORT_FOLDER", "STORAGE_ROOT",
    )

    def setUp(self):
        self._saved = {key: os.environ.get(key) for key in self.ENV_KEYS}
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

    def tearDown(self):
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def desktop_env(self, **overrides):
        """A minimally valid desktop environment, with per-test overrides.

        A key set to None is removed, which is how the "refuses without it"
        tests express themselves.
        """
        for key in self.ENV_KEYS:
            os.environ.pop(key, None)
        os.environ.update(
            SKIP_DOTENV="true",
            APP_ENV="desktop",
            SECRET_KEY="desktop-session-key",
            PAYSLIP_TOKEN_KEY="desktop-payslip-key",
            AUTO_INIT_DB="false",
            PAYROLLA_INSTANCE_PATH=self._tmp.name,
        )
        for key, value in overrides.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


class DesktopIsItsOwnEnvironmentTests(_DesktopEnvCase):
    """Desktop is declared, never inferred, and is not production."""

    def test_desktop_is_detected_and_is_not_production(self):
        self.desktop_env()
        self.assertTrue(app_module.detect_is_desktop())
        self.assertFalse(app_module.detect_is_production())

    def test_the_claim_is_case_and_whitespace_insensitive(self):
        for value in ("desktop", "DESKTOP", " Desktop "):
            with self.subTest(value=value):
                self.desktop_env(APP_ENV=value)
                self.assertTrue(app_module.detect_is_desktop())

    def test_nothing_else_is_ever_mistaken_for_desktop(self):
        """No heuristic: a packaged install is launched by something that knows."""
        for value in (None, "", "production", "development", "wat"):
            with self.subTest(value=value):
                self.desktop_env(APP_ENV=value)
                self.assertFalse(app_module.detect_is_desktop())

    def test_the_app_exposes_both_flags(self):
        self.desktop_env()
        config = create_app().config
        self.assertTrue(config["IS_DESKTOP"])
        self.assertFalse(config["IS_PRODUCTION"])


class DesktopKeepsTheDeploymentHalfTests(_DesktopEnvCase):
    """The relaxations desktop genuinely needs, and nothing more."""

    def test_sqlite_is_allowed_without_a_database_url(self):
        """As production this could not boot at all — persistence demands Postgres."""
        self.desktop_env()
        config = create_app().config
        self.assertTrue(config["SQLALCHEMY_DATABASE_URI"].startswith("sqlite"))

    def test_the_session_cookie_is_not_secure_only(self):
        """Over http://127.0.0.1 a Secure cookie is never returned, so login fails."""
        self.desktop_env()
        self.assertFalse(create_app().config["SESSION_COOKIE_SECURE"])

    def test_demo_logins_are_never_advertised_even_if_asked_for(self):
        """Desktop faces a paying firm's staff; development's escape hatch is closed."""
        self.desktop_env(SEED_DEMO_DATA="true", SHOW_DEMO_LOGINS="true")
        self.addCleanup(os.environ.pop, "SHOW_DEMO_LOGINS", None)
        self.assertFalse(create_app().config["SHOW_DEMO_LOGINS"])


class DesktopSigningKeyTests(_DesktopEnvCase):
    """Keys must be supplied and distinct — a desktop app restarts constantly."""

    def test_it_refuses_to_boot_without_a_secret_key(self):
        self.desktop_env(SECRET_KEY=None)
        with self.assertRaises(RuntimeError) as caught:
            create_app()
        self.assertIn("SECRET_KEY", str(caught.exception))

    def test_it_refuses_to_boot_without_a_payslip_token_key(self):
        """A per-process value would kill every link already sent, on every launch."""
        self.desktop_env(PAYSLIP_TOKEN_KEY=None)
        with self.assertRaises(RuntimeError) as caught:
            create_app()
        self.assertIn("PAYSLIP_TOKEN_KEY", str(caught.exception))

    def test_it_refuses_a_payslip_key_that_merely_copies_the_session_key(self):
        self.desktop_env(SECRET_KEY="same", PAYSLIP_TOKEN_KEY="same")
        with self.assertRaises(RuntimeError) as caught:
            create_app()
        self.assertIn("must differ", str(caught.exception))

    def test_supplied_keys_are_used_verbatim_so_they_survive_a_restart(self):
        self.desktop_env()
        first, second = create_app().config, create_app().config
        self.assertEqual(first["SECRET_KEY"], "desktop-session-key")
        self.assertEqual(first["PAYSLIP_TOKEN_KEY"], "desktop-payslip-key")
        self.assertEqual(first["SECRET_KEY"], second["SECRET_KEY"])
        self.assertEqual(first["PAYSLIP_TOKEN_KEY"], second["PAYSLIP_TOKEN_KEY"])

    def test_development_still_gets_per_process_keys(self):
        """The relaxation development relies on must survive this change."""
        self.desktop_env(APP_ENV="development", SECRET_KEY=None, PAYSLIP_TOKEN_KEY=None)
        self.assertTrue(create_app().config["SECRET_KEY"])


class DesktopNeverSeedsAtBootTests(_DesktopEnvCase):
    """seed_default_data writes the demo roster regardless of SEED_DEMO_DATA."""

    def test_auto_init_db_is_refused(self):
        self.desktop_env(AUTO_INIT_DB="true")
        with self.assertRaises(RuntimeError) as caught:
            create_app()
        self.assertIn("AUTO_INIT_DB", str(caught.exception))

    def test_the_default_is_refused_too_not_just_an_explicit_true(self):
        """AUTO_INIT_DB defaults to true, so an unset variable is the real risk."""
        self.desktop_env(AUTO_INIT_DB=None)
        with self.assertRaises(RuntimeError):
            create_app()

    def test_the_boot_seed_would_have_written_published_credentials(self):
        """Why the guard exists, pinned so the seed's shape can't drift unnoticed.

        If this ever fails because the roster moved behind SEED_DEMO_DATA, the
        guard above is no longer load-bearing and should be reconsidered — not
        deleted silently.
        """
        from app import seed

        self.assertTrue(seed.PLATFORM_USERS)
        self.assertTrue(seed.DEMO_PASSWORD)


class RelocatablePathTests(_DesktopEnvCase):
    """Every writable location must follow PAYROLLA_INSTANCE_PATH out of the bundle."""

    def test_all_three_instance_paths_move_together(self):
        self.desktop_env()
        app = create_app()
        root = os.path.realpath(self._tmp.name)
        self.assertEqual(os.path.realpath(app.instance_path), root)
        # realpath both sides: on Windows the temp dir arrives as an 8.3 short
        # name ("HPLAPT~1") and only one side of the comparison expands it.
        located = {
            "database": app.config["SQLALCHEMY_DATABASE_URI"].replace("sqlite:///", ""),
            "import sessions": app.config["IMPORT_SESSION_FOLDER"],
            "storage": app.config["STORAGE_ROOT"],
        }
        for label, path in located.items():
            with self.subTest(location=label):
                self.assertEqual(os.path.realpath(os.path.dirname(path)), root)

    def test_export_folder_is_overridable(self):
        """The one writable path that had no override, so it stayed in the bundle."""
        target = os.path.join(self._tmp.name, "exports")
        self.desktop_env(EXPORT_FOLDER=target)
        self.assertEqual(
            os.path.realpath(create_app().config["EXPORT_FOLDER"]),
            os.path.realpath(target),
        )

    def test_an_unset_export_folder_keeps_the_historical_location(self):
        self.desktop_env(EXPORT_FOLDER=None)
        self.assertEqual(
            os.path.realpath(create_app().config["EXPORT_FOLDER"]),
            os.path.realpath(
                os.path.join(os.path.dirname(app_module.__file__), "..", "exports")
            ),
        )

    def test_an_unset_instance_path_leaves_flask_defaults_alone(self):
        """The web app must be untouched by all of this."""
        self.desktop_env(APP_ENV="development", PAYROLLA_INSTANCE_PATH=None)
        app = create_app()
        self.assertEqual(
            os.path.realpath(app.instance_path),
            os.path.realpath(
                os.path.join(os.path.dirname(app_module.__file__), "..", "instance")
            ),
        )


if __name__ == "__main__":
    unittest.main()
