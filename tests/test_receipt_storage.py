"""Everything beneath the receipt routes: storage, file rules, and the schema.

Three layers, none of which need HTTP or tenant fixtures:

  * :mod:`app.storage` — key validation, path-traversal containment, and the
    save/open/delete/exists contract. This is precisely the surface a Supabase
    backend would have to satisfy, so testing it directly is what makes the
    "swap the backend" claim checkable.
  * :mod:`app.receipts` — which files are accepted and which are refused
    (extension, declared MIME, sniffed magic bytes, size), and the fact that an
    uploaded filename can never reach the filesystem.
  * The ``expense_receipt`` migration — that the table, and the constraints that
    make "one receipt per expense" a schema guarantee, apply and reverse.

The route-level behaviour built on all this — upload, download, delete,
authorization and tenant isolation — lives in ``test_expense_receipts.py``.
"""

import io
import os
import shutil
import tempfile
import unittest

os.environ["SKIP_DOTENV"] = "true"
os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["PERSISTENCE_REQUIRED"] = "false"

from werkzeug.datastructures import FileStorage  # noqa: E402

from app import create_app, db  # noqa: E402
from app.receipts import (  # noqa: E402
    ALLOWED_EXTENSIONS,
    ReceiptValidationError,
    build_storage_key,
    validate_upload,
)
from app.storage import (  # noqa: E402
    LocalStorageBackend,
    StorageError,
    get_storage,
    safe_key,
)

# Minimal byte sequences carrying each format's real signature. Validation
# sniffs the leading bytes, so these are genuine as far as it is concerned —
# and deliberately tiny, keeping the suite fast. Shared with
# test_expense_receipts.py so the two files can never disagree about what a
# valid PNG looks like.
PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
JPEG_BYTES = b"\xff\xd8\xff\xe0" + b"\x00" * 64
PDF_BYTES = b"%PDF-1.4\n%\xc7\xec\x8f\xa2\n" + b"0" * 64


def upload(content, filename, content_type):
    """A form file field, as Werkzeug's test client expects it."""
    return (io.BytesIO(content), filename, content_type)


class StorageTestBase(unittest.TestCase):
    """An app with its own temp storage root — no database fixtures needed."""

    def setUp(self):
        self.storage_root = tempfile.mkdtemp(prefix="receipt_storage_test_")
        self.app = create_app()
        self.app.config["TESTING"] = True
        self.app.config["STORAGE_ROOT"] = self.storage_root
        # Drop the cached backend so it is rebuilt against the temp root.
        self.app.extensions.pop("payrolla_storage", None)
        self.ctx = self.app.app_context()
        self.ctx.push()

    def tearDown(self):
        self.ctx.pop()
        shutil.rmtree(self.storage_root, ignore_errors=True)


class StorageSeamTestCase(StorageTestBase):
    """Keys in, bytes out, nothing escaping the root."""

    def test_safe_key_accepts_a_generated_key(self):
        key = build_storage_key(42, ".pdf")
        self.assertTrue(key.startswith("receipts/42/"))
        self.assertTrue(key.endswith(".pdf"))
        self.assertEqual(safe_key(key), key)

    def test_safe_key_rejects_traversal_and_absolute_paths(self):
        hostile = [
            "../../etc/passwd",
            "receipts/../../secret.pdf",
            "/etc/passwd",
            "receipts\\..\\win.ini",
            "C:/Windows/win.ini",
            "receipts//double.pdf",
            "receipts/./same.pdf",
            "",
            "   ",
            "receipts/nul\x00.pdf",
        ]
        for key in hostile:
            with self.subTest(key=key):
                with self.assertRaises(StorageError):
                    safe_key(key)

    def test_safe_key_rejects_non_strings(self):
        for value in (None, 42, b"receipts/1/x.pdf", ["receipts"]):
            with self.subTest(value=value):
                with self.assertRaises(StorageError):
                    safe_key(value)

    def test_backend_refuses_keys_that_escape_the_root(self):
        backend = LocalStorageBackend(self.storage_root)
        for key in ("../escape.pdf", "receipts/../../escape.pdf"):
            with self.subTest(key=key):
                with self.assertRaises(StorageError):
                    backend.save(key, io.BytesIO(b"x"))
                with self.assertRaises(StorageError):
                    backend.open(key)
                with self.assertRaises(StorageError):
                    backend.delete(key)

    def test_save_open_delete_round_trip(self):
        backend = LocalStorageBackend(self.storage_root)
        key = build_storage_key(1, ".png")
        written = backend.save(key, io.BytesIO(PNG_BYTES))
        self.assertEqual(written, len(PNG_BYTES))
        self.assertTrue(backend.exists(key))
        with backend.open(key) as handle:
            self.assertEqual(handle.read(), PNG_BYTES)
        self.assertTrue(backend.delete(key))
        self.assertFalse(backend.exists(key))
        # Idempotent: deleting again is a no-op, not an error.
        self.assertFalse(backend.delete(key))

    def test_saving_creates_intermediate_directories(self):
        backend = LocalStorageBackend(self.storage_root)
        key = build_storage_key(9999, ".pdf")  # a company dir that does not exist
        backend.save(key, io.BytesIO(PDF_BYTES))
        self.assertTrue(backend.exists(key))

    def test_opening_a_missing_object_raises(self):
        backend = LocalStorageBackend(self.storage_root)
        with self.assertRaises(StorageError):
            backend.open(build_storage_key(1, ".pdf"))

    def test_stored_files_stay_inside_the_configured_root(self):
        backend = LocalStorageBackend(self.storage_root)
        key = build_storage_key(7, ".pdf")
        backend.save(key, io.BytesIO(PDF_BYTES))
        written = [
            os.path.join(base, name)
            for base, _dirs, files in os.walk(self.storage_root)
            for name in files
        ]
        self.assertEqual(len(written), 1)
        self.assertTrue(os.path.abspath(written[0]).startswith(self.storage_root))

    def test_local_backend_streams_rather_than_signing_urls(self):
        # None means "the app must stream it", which is what keeps the tenant
        # check on the download route meaningful.
        self.assertIsNone(get_storage().url(build_storage_key(1, ".pdf")))

    def test_the_configured_backend_is_cached_per_app(self):
        self.assertIs(get_storage(), get_storage())

    def test_unknown_backend_is_refused_loudly(self):
        self.app.config["STORAGE_BACKEND"] = "s3-maybe"
        self.app.extensions.pop("payrolla_storage", None)
        with self.assertRaises(StorageError):
            get_storage()

    def test_storage_keys_are_unique_per_upload(self):
        keys = {build_storage_key(3, ".pdf") for _ in range(50)}
        self.assertEqual(len(keys), 50)

    def test_storage_keys_are_partitioned_by_company(self):
        self.assertTrue(build_storage_key(3, ".pdf").startswith("receipts/3/"))
        self.assertTrue(build_storage_key(4, ".pdf").startswith("receipts/4/"))


class ReceiptValidationTestCase(StorageTestBase):
    """Format and size rules, checked at the function that owns them."""

    def _validated(self, content, filename, content_type):
        return validate_upload(
            FileStorage(
                stream=io.BytesIO(content), filename=filename, content_type=content_type
            )
        )

    def test_every_documented_format_is_accepted(self):
        cases = [
            (PDF_BYTES, "receipt.pdf", "application/pdf", "application/pdf"),
            (PNG_BYTES, "receipt.png", "image/png", "image/png"),
            (JPEG_BYTES, "receipt.jpg", "image/jpeg", "image/jpeg"),
            (JPEG_BYTES, "receipt.jpeg", "image/jpeg", "image/jpeg"),
            # Case in the extension must not matter.
            (PNG_BYTES, "RECEIPT.PNG", "image/png", "image/png"),
            # Browsers vary in how they spell JPEG.
            (JPEG_BYTES, "receipt.jpg", "image/jpg", "image/jpeg"),
        ]
        for content, filename, declared, expected in cases:
            with self.subTest(filename=filename, declared=declared):
                _name, mime, size = self._validated(content, filename, declared)
                self.assertEqual(mime, expected)
                self.assertEqual(size, len(content))

    def test_allowed_extensions_are_exactly_the_documented_four(self):
        self.assertEqual(set(ALLOWED_EXTENSIONS), {".pdf", ".png", ".jpg", ".jpeg"})

    def test_unsupported_formats_are_rejected(self):
        cases = {
            "executable": (b"MZ\x90\x00", "payload.exe", "application/octet-stream"),
            "script": (b"#!/bin/sh\nrm -rf /", "run.sh", "text/x-shellscript"),
            "text": (b"just words", "notes.txt", "text/plain"),
            "spreadsheet": (b"PK\x03\x04", "book.xlsx", "application/vnd.ms-excel"),
            "no extension": (PDF_BYTES, "receipt", "application/pdf"),
            "svg (scriptable)": (b"<svg onload=alert(1)>", "x.svg", "image/svg+xml"),
            "html": (b"<html><script>x</script>", "page.html", "text/html"),
        }
        for label, (content, filename, declared) in cases.items():
            with self.subTest(case=label):
                with self.assertRaises(ReceiptValidationError):
                    self._validated(content, filename, declared)

    def test_a_file_renamed_to_an_allowed_extension_is_rejected(self):
        """The decisive check — extension and declared MIME are both attacker
        controlled, so the leading bytes have the final say."""
        with self.assertRaises(ReceiptValidationError):
            self._validated(b"MZ\x90\x00 not an image", "payload.png", "image/png")

    def test_a_file_that_merely_contains_the_pdf_marker_is_not_a_pdf(self):
        """The magic check is a header check, not a substring search: a file that
        mentions %PDF- further in is not a PDF."""
        smuggled = b"MZ\x90\x00" + b"x" * 200 + b"%PDF-1.4"
        with self.assertRaises(ReceiptValidationError):
            self._validated(smuggled, "sneaky.pdf", "application/pdf")

    def test_a_pdf_behind_a_bom_is_still_accepted(self):
        # Real generators emit a BOM or stray whitespace before the header.
        _name, mime, _size = self._validated(
            b"\xef\xbb\xbf" + PDF_BYTES, "bom.pdf", "application/pdf"
        )
        self.assertEqual(mime, "application/pdf")

    def test_extension_must_match_the_actual_content(self):
        # A real PDF, but claiming to be a PNG: serving it back as image/png
        # would be a lie, so it is refused.
        with self.assertRaises(ReceiptValidationError):
            self._validated(PDF_BYTES, "actually.png", "image/png")

    def test_declared_mime_must_be_plausible(self):
        with self.assertRaises(ReceiptValidationError):
            self._validated(PDF_BYTES, "receipt.pdf", "text/html")

    def test_empty_file_is_rejected(self):
        with self.assertRaises(ReceiptValidationError):
            self._validated(b"", "receipt.pdf", "application/pdf")

    def test_missing_or_unnamed_file_is_rejected(self):
        with self.assertRaises(ReceiptValidationError):
            validate_upload(None)
        with self.assertRaises(ReceiptValidationError):
            self._validated(PDF_BYTES, "", "application/pdf")

    def test_oversized_file_is_rejected_with_the_limit_named(self):
        oversized = PDF_BYTES + b"0" * (10 * 1024 * 1024)
        with self.assertRaises(ReceiptValidationError) as caught:
            self._validated(oversized, "huge.pdf", "application/pdf")
        self.assertIn("10 MB", str(caught.exception))

    def test_a_file_at_the_limit_is_accepted(self):
        limit = self.app.config["RECEIPT_MAX_BYTES"]
        exact = PDF_BYTES + b"0" * (limit - len(PDF_BYTES))
        _name, _mime, size = self._validated(exact, "big.pdf", "application/pdf")
        self.assertEqual(size, limit)

    def test_validation_leaves_the_stream_ready_to_save(self):
        handle = FileStorage(
            stream=io.BytesIO(PNG_BYTES), filename="r.png", content_type="image/png"
        )
        validate_upload(handle)
        self.assertEqual(handle.stream.tell(), 0)
        self.assertEqual(handle.stream.read(), PNG_BYTES)

    def test_hostile_filenames_never_reach_the_storage_key(self):
        for filename in ("../../etc/passwd.pdf", "..\\..\\win.pdf", "a/b/c.pdf"):
            with self.subTest(filename=filename):
                safe_name, _mime, _size = self._validated(
                    PDF_BYTES, filename, "application/pdf"
                )
                self.assertNotIn("/", safe_name)
                self.assertNotIn("\\", safe_name)
                self.assertNotIn("..", safe_name)
                self.assertTrue(safe_name.endswith(".pdf"))

    def test_a_name_with_no_real_extension_is_rejected(self):
        # "..pdf" is a dotfile called "..pdf", not a PDF: splitext finds no
        # extension, so it never reaches the format checks. " .pdf" is the same
        # once the name is trimmed.
        for filename in ("..pdf", "....png", "///.pdf", "receipt", " .pdf"):
            with self.subTest(filename=filename):
                with self.assertRaises(ReceiptValidationError):
                    self._validated(PDF_BYTES, filename, "application/pdf")

    def test_a_name_that_sanitises_away_falls_back_to_a_generic_one(self):
        """secure_filename can strip a name down to nothing usable ("%%%.pdf"
        -> "pdf", which has lost its extension). The download still needs a
        sensible name carrying the right suffix."""
        for filename in ("%%%.pdf", "___.pdf", "..;.pdf"):
            with self.subTest(filename=filename):
                safe_name, _mime, _size = self._validated(
                    PDF_BYTES, filename, "application/pdf"
                )
                self.assertEqual(safe_name, "receipt.pdf")


class ReceiptMigrationTestCase(unittest.TestCase):
    """The expense_receipt table applies and reverses cleanly.

    Additive (a new table), so the risk is reversibility on SQLite rather than
    application. The unique index on expense_id is checked explicitly: "one
    receipt per expense" is a schema guarantee, not just a convention in the
    routes, and a downgrade/upgrade cycle must bring it back.
    """

    BEFORE_RECEIPTS = "a4e7c2b81d95"

    def test_expense_receipt_table_round_trips(self):
        import sqlalchemy as sa
        from flask_migrate import downgrade as fm_downgrade
        from flask_migrate import stamp as fm_stamp
        from flask_migrate import upgrade as fm_upgrade

        tmp = tempfile.mkdtemp()
        dbfile = os.path.join(tmp, "receipt_mig.sqlite")
        previous = os.environ.get("DATABASE_URL")
        os.environ["DATABASE_URL"] = f"sqlite:///{dbfile}"
        try:
            app = create_app()  # create_all builds the current schema
            with app.app_context():
                fm_stamp(revision="head")
                self.assertIn("expense_receipt", sa.inspect(db.engine).get_table_names())

                fm_downgrade(revision=self.BEFORE_RECEIPTS)
                self.assertNotIn(
                    "expense_receipt", sa.inspect(db.engine).get_table_names()
                )

                fm_upgrade(revision="head")
                inspector = sa.inspect(db.engine)
                self.assertIn("expense_receipt", inspector.get_table_names())
                columns = {c["name"] for c in inspector.get_columns("expense_receipt")}
                self.assertTrue(
                    {
                        "expense_id", "client_company_id", "original_filename",
                        "storage_key", "content_type", "byte_size", "uploaded_at",
                        "uploaded_by",
                    }
                    <= columns
                )
                indexes = {i["name"]: i for i in inspector.get_indexes("expense_receipt")}
                self.assertTrue(indexes["ix_expense_receipt_expense_id"]["unique"])
                self.assertIn("ix_expense_receipt_client_company_id", indexes)
        finally:
            os.environ["DATABASE_URL"] = previous or "sqlite:///:memory:"

    def test_single_head(self):
        from alembic.config import Config
        from alembic.script import ScriptDirectory

        cfg = Config()
        cfg.set_main_option("script_location", "migrations")
        self.assertEqual(len(ScriptDirectory.from_config(cfg).get_heads()), 1)


if __name__ == "__main__":
    unittest.main()
