"""Validation for uploaded spreadsheets — the workbook counterpart to :mod:`app.receipts`.

:mod:`app.receipts` is the reference implementation for upload validation in this
codebase, and its *shape* is reused here verbatim: an ordered set of checks that
fail on the first problem with a message the user can act on, size measured by
seeking the stream rather than trusting ``Content-Length``, and the decisive test
being the file's actual leading bytes rather than anything the client declared.

What could **not** be reused is its substance. ``receipts.validate_upload`` is
bound to a fixed PDF/PNG/JPEG allow-list, its own ``RECEIPT_MAX_BYTES``, and its
own exception type; and — the part that matters — it has no notion of an archive,
because none of the formats it accepts is one. An ``.xlsx`` *is* a zip, so it
carries a whole class of risk a receipt does not: a workbook that is small on the
wire and enormous once openpyxl expands it. Hence this module rather than a new
branch inside that one.

Two gates, and the order between them is the point:

1. :func:`validate_spreadsheet_upload` — extension, declared MIME, size and
   magic bytes, run against the upload before it is written anywhere.
2. :func:`assert_workbook_within_limits` — entry count, total uncompressed size
   and per-entry compression ratio, run **before the workbook is parsed**. A zip
   bomb is only dangerous once something decompresses it, so this has to sit
   between "we have the bytes" and "openpyxl opens them", never after.
"""

import os
import zipfile

from flask import current_app
from werkzeug.utils import secure_filename

# Extension -> the content type we consider canonical for it. Grouped by what
# the bytes actually are, because that is what decides how each is checked.
ZIP_WORKBOOK_EXTENSIONS = {".xlsx", ".xlsm"}   # OOXML — a zip archive
LEGACY_WORKBOOK_EXTENSIONS = {".xls"}          # BIFF/OLE2 compound file
TEXT_EXTENSIONS = {".csv"}                     # plain text; no signature to check

# The default allow-list, matching the `accept` attribute the standard payroll
# upload forms advertise. The raw-hours importer passes a narrower set.
ALLOWED_EXTENSIONS = ZIP_WORKBOOK_EXTENSIONS | LEGACY_WORKBOOK_EXTENSIONS | TEXT_EXTENSIONS

# Content types a client is allowed to *declare*. Deliberately broad — browsers
# and Excel disagree about the spelling, and an empty value is common — because
# the sniffed bytes below are what actually decide. This only rejects the absurd.
_DECLARED_ALIASES = {
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.ms-excel.sheet.macroenabled.12",
    "application/vnd.ms-excel",
    "application/excel",
    "application/x-excel",
    "application/x-msexcel",
    "application/octet-stream",
    "text/csv",
    "text/plain",
    "application/csv",
    "text/comma-separated-values",
}

# Leading bytes. An OOXML workbook is a zip ("PK\x03\x04"); a legacy .xls is an
# OLE2 compound document. CSV has no signature — anything printable is valid —
# so it is size-checked only.
_ZIP_MAGIC = b"PK\x03\x04"
_OLE2_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"

MAX_FILENAME_LENGTH = 255


class SpreadsheetValidationError(Exception):
    """An upload the user must fix. ``str(exc)`` is shown to them verbatim, so
    every message names the limit and what to do about it."""


def max_bytes():
    """Per-file ceiling for a spreadsheet upload.

    Deliberately well under the global ``MAX_CONTENT_LENGTH`` (16 MB) so an
    oversized workbook gets this explanatory message instead of Werkzeug's bare
    413, which arrives before any view runs and cannot say what was wrong.
    """
    return int(current_app.config.get("SPREADSHEET_MAX_BYTES", 8 * 1024 * 1024))


def max_megabytes():
    return max_bytes() // (1024 * 1024)


def _limits():
    """The zip-bomb thresholds, from config so a deployment can tighten them."""
    cfg = current_app.config
    return (
        int(cfg.get("WORKBOOK_MAX_ENTRIES", 1024)),
        int(cfg.get("WORKBOOK_MAX_UNCOMPRESSED_BYTES", 256 * 1024 * 1024)),
        float(cfg.get("WORKBOOK_MAX_COMPRESSION_RATIO", 200.0)),
    )


def _extension(filename):
    return os.path.splitext(filename or "")[1].lower()


def _stream_size(stream):
    """Byte length of an open stream, restoring its position.

    Measured rather than read from ``Content-Length``: the header is client
    supplied, and Werkzeug's ``content_length`` is 0 for chunked uploads. Same
    reasoning, and same implementation, as :func:`app.receipts._stream_size`.
    """
    position = stream.tell()
    stream.seek(0, os.SEEK_END)
    size = stream.tell()
    stream.seek(position)
    return size


def _check_magic(head, extension):
    """Raise unless ``head`` carries the signature ``extension`` implies."""
    if extension in ZIP_WORKBOOK_EXTENSIONS:
        if not head.startswith(_ZIP_MAGIC):
            # The common real-world cause is a genuine .xls (or a CSV) renamed
            # to .xlsx, so the message points at that rather than crying attack.
            raise SpreadsheetValidationError(
                "That file is not a real .xlsx workbook. If it was saved as an "
                "older Excel format or a CSV, re-save it as .xlsx and try again."
            )
    elif extension in LEGACY_WORKBOOK_EXTENSIONS:
        if not head.startswith(_OLE2_MAGIC):
            # An .xlsx renamed .xls is the usual case and is worth naming, since
            # the fix is simply to upload it under its own extension.
            if head.startswith(_ZIP_MAGIC):
                raise SpreadsheetValidationError(
                    "That file is an .xlsx workbook with an .xls name. "
                    "Rename it to .xlsx and upload it again."
                )
            raise SpreadsheetValidationError(
                "That file is not a readable .xls workbook."
            )
    elif extension in TEXT_EXTENSIONS:
        # A CSV has no signature, but it must not be a disguised binary: a zip or
        # OLE2 file named .csv is never something a user meant to upload.
        if head.startswith(_ZIP_MAGIC) or head.startswith(_OLE2_MAGIC):
            raise SpreadsheetValidationError(
                "That file is an Excel workbook with a .csv name. "
                "Upload it as .xlsx, or export it to real CSV first."
            )


def validate_spreadsheet_upload(file_storage, allowed=None):
    """Check an uploaded spreadsheet and return ``(safe_filename, extension, size)``.

    Raises :class:`SpreadsheetValidationError` with a user-facing message on the
    first problem found. Leaves the stream rewound and ready to save or parse.

    ``allowed`` narrows the accepted extensions for a caller that takes fewer
    than the default (the raw-hours importer is .xlsx only).
    """
    permitted = {e.lower() for e in (allowed or ALLOWED_EXTENSIONS)}

    if file_storage is None or not (file_storage.filename or "").strip():
        raise SpreadsheetValidationError("Choose a file to upload.")

    filename = file_storage.filename.strip()
    extension = _extension(filename)
    if extension not in permitted:
        listed = ", ".join(sorted(permitted))
        raise SpreadsheetValidationError(f"Only {listed} files are supported.")

    declared = (file_storage.mimetype or "").split(";")[0].strip().lower()
    if declared and declared not in _DECLARED_ALIASES:
        raise SpreadsheetValidationError(f"Only {extension} files are supported.")

    stream = file_storage.stream
    size = _stream_size(stream)
    if size <= 0:
        raise SpreadsheetValidationError("That file is empty — choose a different file.")
    if size > max_bytes():
        raise SpreadsheetValidationError(
            f"That file is {size / (1024 * 1024):.1f} MB. "
            f"Uploads must be {max_megabytes()} MB or smaller."
        )

    head = stream.read(max(len(_OLE2_MAGIC), len(_ZIP_MAGIC)) + 8)
    stream.seek(0)
    _check_magic(head, extension)

    # Sanitised for use as a temp-file suffix and for display. secure_filename
    # can return "" for pathological input (e.g. ".."), so fall back to a generic
    # name that still carries the right extension.
    safe_name = secure_filename(filename)[:MAX_FILENAME_LENGTH]
    if not safe_name or _extension(safe_name) != extension:
        safe_name = f"upload{extension}"
    return safe_name, extension, size


def assert_workbook_within_limits(path):
    """Raise :class:`SpreadsheetValidationError` if the workbook at ``path`` looks
    like a decompression bomb. Call this **before** handing the file to openpyxl
    or pandas.

    A 4 MB upload that expands to several GB costs nothing to send and takes the
    process down; the global ``MAX_CONTENT_LENGTH`` cannot see it, because the
    danger is in the expansion ratio rather than the transfer size. Three
    thresholds, because a bomb has to defeat all three and a real workbook trips
    none — measured against this repo's own exports, which run 2-5x on 9-17
    entries, against a ceiling of 200x on 1024:

      entries    — a bomb hides its payload across many members
      total size — the absolute ceiling on what one upload may expand to
      ratio      — the per-entry tell; DEFLATE tops out near 1032:1, so anything
                   above 200:1 is not a spreadsheet

    Non-zip inputs (.xls, .csv) return quietly: they are not archives, so there
    is nothing here to check. Sizes come from the zip's central directory, which
    is metadata the uploader controls — it is authoritative enough to catch a
    bomb built by any standard tool, and the per-file cap above bounds what a
    handcrafted lie could still deliver.
    """
    if not zipfile.is_zipfile(path):
        return

    max_entries, max_uncompressed, max_ratio = _limits()
    try:
        with zipfile.ZipFile(path) as archive:
            infos = archive.infolist()
    except zipfile.BadZipFile:
        raise SpreadsheetValidationError(
            "That file could not be read as an .xlsx workbook — it may be corrupt."
        )

    if len(infos) > max_entries:
        raise SpreadsheetValidationError(
            f"That workbook has {len(infos)} internal parts (limit {max_entries}) "
            "and was refused as unsafe to open."
        )

    total = 0
    for info in infos:
        total += info.file_size
        if total > max_uncompressed:
            raise SpreadsheetValidationError(
                f"That workbook expands to over {max_uncompressed // (1024 * 1024)} MB "
                "and was refused as unsafe to open."
            )
        # Ratio is only meaningful once an entry is big enough for the header
        # overhead to stop dominating; tiny entries routinely look extreme.
        if info.compress_size > 1024 and info.file_size / info.compress_size > max_ratio:
            raise SpreadsheetValidationError(
                "That workbook contains a part compressed far beyond what a "
                "spreadsheet produces, and was refused as unsafe to open."
            )
