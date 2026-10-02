"""SMS Phase 1: the boot guards, and no SMS on a desktop install.

Each guard fails closed. The one with a history is PUBLIC_BASE_URL: the
distribution worker builds messages outside any request, so without it every
SMS would go out with no payslip link at all.
"""
import os
import tempfile
import unittest

os.environ["SKIP_DOTENV"] = "true"
os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["SEED_DEMO_DATA"] = "true"
os.environ["PERSISTENCE_REQUIRED"] = "false"

from app import create_app, db  # noqa: E402
from app.distribution.channels import sendable_channels  # noqa: E402
from app.distribution.service import resolve_channel  # noqa: E402
from app.models import DistributionBatch, PayrollRun, User  # noqa: E402


class _Obj:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class _EnvCase(unittest.TestCase):
    """Saves and restores every environment variable these tests rewrite."""

    ENV_KEYS = (
        "APP_ENV", "FLASK_ENV", "RENDER", "RAILWAY_ENVIRONMENT", "SECRET_KEY",
        "PAYSLIP_TOKEN_KEY", "DATABASE_URL", "PERSISTENCE_REQUIRED", "AUTO_INIT_DB",
        "SEED_DEMO_DATA", "DISTRIBUTION_WORKER_INLINE", "PAYROLLA_INSTANCE_PATH",
        "PUBLIC_BASE_URL", "SMS_BACKEND", "SMS_SENDER_ID", "SASUSYNC_API_KEY",
        "SASUSYNC_BASE_URL", "SASUSYNC_SANDBOX", "SASUSYNC_WEBHOOK_SECRET",
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

    def _env(self, base, overrides):
        for key in self.ENV_KEYS:
            os.environ.pop(key, None)
        os.environ.update(SKIP_DOTENV="true", **base)
        for key, value in overrides.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def development(self, **overrides):
        self._env({
            "FLASK_ENV": "development", "DATABASE_URL": "sqlite:///:memory:",
            "PERSISTENCE_REQUIRED": "false", "AUTO_INIT_DB": "false",
        }, overrides)

    def production(self, **overrides):
        """A production boot that never touches a database: no schema init and
        no inline worker thread."""
        self._env({
            "FLASK_ENV": "production", "DATABASE_URL": "postgresql://u:p@localhost/db",
            "AUTO_INIT_DB": "false", "SECRET_KEY": "x" * 64,
            "PAYSLIP_TOKEN_KEY": "y" * 64, "DISTRIBUTION_WORKER_INLINE": "false",
        }, overrides)

    def desktop(self, **overrides):
        self._env({
            "APP_ENV": "desktop", "SECRET_KEY": "desktop-session-key",
            "PAYSLIP_TOKEN_KEY": "desktop-payslip-key", "AUTO_INIT_DB": "false",
            "PAYROLLA_INSTANCE_PATH": self._tmp.name,
        }, overrides)

    def assertRefuses(self, *fragments):
        with self.assertRaises(RuntimeError) as caught:
            create_app()
        for fragment in fragments:
            self.assertIn(fragment, str(caught.exception))


SASUSYNC_LIVE = dict(
    SMS_BACKEND="sasusync", PUBLIC_BASE_URL="https://pmvp-v1.onrender.com",
    SMS_SENDER_ID="Payrolla", SASUSYNC_API_KEY="key",
    SASUSYNC_BASE_URL="https://sms.sasusync.com",
)


class PublicBaseUrlGuardTests(_EnvCase):
    def test_a_real_backend_without_it_refuses_to_boot_in_any_environment(self):
        for backend in ("sasusync", "hubtel"):
            for environment in (self.development, self.production):
                with self.subTest(backend=backend, env=environment.__name__):
                    environment(**{**SASUSYNC_LIVE, "SMS_BACKEND": backend,
                                   "PUBLIC_BASE_URL": None})
                    self.assertRefuses("PUBLIC_BASE_URL")

    def test_production_requires_https(self):
        self.production(**{**SASUSYNC_LIVE, "PUBLIC_BASE_URL": "http://pmvp-v1.onrender.com"})
        self.assertRefuses("https://")

    def test_development_accepts_a_plain_http_host(self):
        self.development(SMS_BACKEND="sasusync", PUBLIC_BASE_URL="http://localhost:5000")
        self.assertEqual(create_app().config["SMS_BACKEND"], "sasusync")

    def test_the_console_backend_needs_nothing(self):
        self.production(SMS_BACKEND="console")
        self.assertIsNone(create_app().config["PUBLIC_BASE_URL"])
        self.production()  # SMS_BACKEND unset is the console default
        create_app()


class SasuSyncCredentialGuardTests(_EnvCase):
    def test_production_refuses_each_missing_credential(self):
        for missing in ("SASUSYNC_API_KEY", "SASUSYNC_BASE_URL", "SMS_SENDER_ID"):
            with self.subTest(missing=missing):
                self.production(**{**SASUSYNC_LIVE, missing: None})
                self.assertRefuses(missing)

    def test_production_boots_when_all_are_set(self):
        self.production(**SASUSYNC_LIVE)
        config = create_app().config
        self.assertEqual(config["SASUSYNC_API_KEY"], "key")
        self.assertTrue(config["SASUSYNC_SANDBOX"], "sandbox must be the default")

    def test_going_live_is_explicit(self):
        self.production(**SASUSYNC_LIVE, SASUSYNC_SANDBOX="false")
        self.assertFalse(create_app().config["SASUSYNC_SANDBOX"])

    def test_development_tolerates_missing_credentials(self):
        """The sender reports "not configured" at send time instead."""
        self.development(SMS_BACKEND="sasusync", PUBLIC_BASE_URL="http://localhost")
        create_app()


class DesktopGuardTests(_EnvCase):
    def test_desktop_refuses_every_real_sms_backend(self):
        for backend in ("sasusync", "hubtel"):
            with self.subTest(backend=backend):
                self.desktop(**{**SASUSYNC_LIVE, "SMS_BACKEND": backend})
                self.assertRefuses("desktop", "SMS_BACKEND")

    def test_desktop_boots_on_the_console_backend(self):
        self.desktop(SMS_BACKEND="console")
        self.assertTrue(create_app().config["IS_DESKTOP"])


class DesktopBlocksSmsTests(unittest.TestCase):
    """The runtime half: no SMS option, no SMS routing, no SMS enqueue. Booted
    as development with the seeded demo data and IS_DESKTOP switched on, since a
    real desktop boot has no schema to post against."""

    def setUp(self):
        self.app = create_app()
        self.app.config.update(TESTING=True, WTF_CSRF_ENABLED=False, IS_DESKTOP=True)
        self.client = self.app.test_client()
        self.ctx = self.app.app_context()
        self.ctx.push()
        tenant = User.query.filter_by(email="admin@msc.com").first()
        self.run = PayrollRun.query.filter_by(
            client_company_id=tenant.client_company_id, status="Approved"
        ).first()
        self.assertIsNotNone(self.run, "expected a seeded Approved MSC run")

    def tearDown(self):
        db.session.remove()
        self.ctx.pop()

    def _login(self, email):
        response = self.client.post("/login", data={"email": email, "password": "password123"})
        self.assertEqual(response.status_code, 302)

    def _batches(self):
        return DistributionBatch.query.filter_by(payroll_run_id=self.run.id).count()

    def test_sms_is_not_a_sendable_channel(self):
        self.assertNotIn("sms", sendable_channels())
        self.app.config["IS_DESKTOP"] = False
        self.assertIn("sms", sendable_channels())

    def test_auto_routing_skips_sms(self):
        employee = _Obj(preferred_channel="sms", phone="0241234567",
                        momo_number=None, email=None)
        item = _Obj(employee=employee, momo_number=None, email=None)
        self.assertNotEqual(resolve_channel(item), "sms")
        self.app.config["IS_DESKTOP"] = False
        self.assertEqual(resolve_channel(item), "sms")

    def test_neither_send_page_offers_sms(self):
        self._login("admin@payrolla.com")
        operator = self.client.get(f"/distribution/run/{self.run.id}").get_data(as_text=True)
        self.assertIn('value="email"', operator)
        self.assertNotIn('value="sms"', operator)
        self.client.get("/logout")
        self._login("admin@msc.com")
        tenant = self.client.get(f"/company/runs/{self.run.id}/distribute").get_data(as_text=True)
        self.assertIn('value="email"', tenant)
        self.assertNotIn('value="sms"', tenant)

    def test_an_explicit_sms_send_is_refused_at_enqueue(self):
        self._login("admin@payrolla.com")
        before = self._batches()
        for path, data in (
            (f"/distribution/run/{self.run.id}/send", {"channel": "sms", "nonce": "n1"}),
            (f"/distribution/run/{self.run.id}/resend-failed", {"channel": "sms", "nonce": "n2"}),
            (f"/distribution/run/{self.run.id}/schedule",
             {"channel": "sms", "scheduled_for": "2999-01-01T09:00"}),
        ):
            with self.subTest(path=path):
                response = self.client.post(path, data=data, follow_redirects=True)
                self.assertIn("SMS isn&#39;t available in the desktop app",
                              response.get_data(as_text=True))
        self.assertEqual(self._batches(), before)

    def test_an_explicit_sms_send_is_refused_on_the_tenant_portal(self):
        self._login("admin@msc.com")
        before = self._batches()
        response = self.client.post(
            f"/company/runs/{self.run.id}/distribute/send",
            data={"channel": "sms", "nonce": "t1"}, follow_redirects=True,
        )
        self.assertIn("SMS isn&#39;t available in the desktop app", response.get_data(as_text=True))
        self.assertEqual(self._batches(), before)

    def test_auto_and_email_still_queue(self):
        self._login("admin@payrolla.com")
        before = self._batches()
        self.client.post(f"/distribution/run/{self.run.id}/send",
                         data={"channel": "auto", "nonce": "a1"})
        self.assertEqual(self._batches(), before + 1)


if __name__ == "__main__":
    unittest.main()
