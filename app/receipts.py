"""Expense receipts — validation, storage-key layout, attach and detach.

The one place that turns an uploaded file into an :class:`~app.models.ExpenseReceipt`
row plus a stored object, and the one place that removes both. Routes call
:func:`attach_receipt` / :func:`detach_receipt` and never touch
:mod:`app.storage` themselves, so the rules below cannot be half-applied by a
caller that forgot one.

What is enforced, and why each check exists:

  * **Extension** must be one of :data:`ALLOWED_EXTENSIONS`. Cheap first filter,
    and it is what determines the stored key's suffix.
  * **Declared MIME** must match that extension. Catches a ``.pdf`` sent as
    ``image/png`` — inconsistent metadata is not something a real upload does.
  * **Magic bytes** must match too. The decisive check: the first two are both
    attacker-controlled, so the file's *actual* leading bytes are what stop a
    script renamed to ``.png``. The MIME finally recorded is derived from the
    sniffed content, never from the client's header.
  * **Size** must be within ``RECEIPT_MAX_BYTES`` (10 MB) and non-empty,
    measured by seeking the stream rather than trusting ``Content-Length``.

Filenames never reach the filesystem. The stored key is
``receipts/<company_id>/<uuid4><ext>`` — uuid-based, with an extension drawn
from our own allow-list, so no part of it is user text. The original filename is
kept in the database purely to name the download, and is sanitised with
``secure_filename`` before being stored.
"""

import os
import uuid

from flask import current_app
from werkzeug.utils import secure_filename

from app import db
from app.models import ExpenseReceipt
from app.storage import StorageError, get_storage

# Extension -> the MIME type we record for it. Also the browser-facing accept
# list. JPEG carries two spellings; both normalise to image/jpeg.
ALLOWED_EXTENSIONS = {
    ".pdf": "application/pdf",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
}

# What the <input accept="..."> advertises. A convenience for the browser's file
# picker only — never a security control, since the client chooses what to send.
ACCEPT_ATTRIBUTE = ".pdf,.png,.jpg,.jpeg,application/pdf,image/png,image/jpeg"

# Content types a client is allowed to *declare*. Broader than the values above
# because browsers vary (some send image/jpg, some application/x-pdf); the
# recorded type still comes from sniffing, so this only rejects the absurd.
_DECLARED_ALIASES = {
    "application/pdf": "application/pdf",
    "application/x-pdf": "application/pdf",
    "image/png": "image/png",
    "image/jpeg": "image/jpeg",
    "image/jpg": "image/jpeg",
    "image/pjpeg": "image/jpeg",
}

# Leading bytes that identify each format. PDFs get a short tolerance window
# (not a scan of the whole head) because some generators emit a BOM or stray
# whitespace before %PDF-; anything further in is not a PDF header, it is a file
# that merely contains the string.
_PDF_MAGIC = b"%PDF-"
_PDF_MAGIC_WINDOW = 8
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_JPEG_MAGIC = b"\xff\xd8\xff"

MAX_FILENAME_LENGTH = 255


class ReceiptValidationError(Exception):
    """An upload the user must fix. ``str(exc)`` is shown to them verbatim, so
    every message names the limit and what to do about it."""


def max_bytes():
    return int(current_app.config.get("RECEIPT_MAX_BYTES", 10 * 1024 * 1024))


def max_megabytes():
    return max_bytes() // (1024 * 1024)


def _extension(filename):
    return os.path.splitext(filename or "")[1].lower()


def _sniff(head):
    """The content type implied by a file's leading bytes, or None."""
    if head.startswith(_PNG_MAGIC):
        return "image/png"
    if head.startswith(_JPEG_MAGIC):
        return "image/jpeg"
    if _PDF_MAGIC in head[:_PDF_MAGIC_WINDOW + len(_PDF_MAGIC)]:
        return "application/pdf"
    return None


def _stream_size(stream):
    """Byte length of an open stream, restoring its position.

    Measured rather than read from ``Content-Length``: the header is client
    supplied, and Werkzeug's ``content_length`` is 0 for chunked uploads.
    """
    position = stream.tell()
    stream.seek(0, os.SEEK_END)
    size = stream.tell()
    stream.seek(position)
    return size


def validate_upload(file_storage):
    """Check an uploaded receipt and return ``(safe_filename, content_type, size)``.

    Raises :class:`ReceiptValidationError` with a user-facing message on the
    first problem found. Leaves the stream rewound and ready to save.
    """
    if file_storage is None or not (file_storage.filename or "").strip():
        raise ReceiptValidationError("Choose a receipt file to upload.")

    filename = file_storage.filename.strip()
    extension = _extension(filename)
    if extension not in ALLOWED_EXTENSIONS:
        raise ReceiptValidationError(
            "Receipts must be a PDF, PNG, JPG or JPEG file."
        )

    declared = (file_storage.mimetype or "").split(";")[0].strip().lower()
    if declared and declared not in _DECLARED_ALIASES:
        raise ReceiptValidationError(
            "Receipts must be a PDF, PNG, JPG or JPEG file."
        )

    stream = file_storage.stream
    size = _stream_size(stream)
    if size <= 0:
        raise ReceiptValidationError("That file is empty — choose a different receipt.")
    if size > max_bytes():
        raise ReceiptValidationError(
            f"That receipt is {size / (1024 * 1024):.1f} MB. "
            f"Receipts must be {max_megabytes()} MB or smaller."
        )

    head = stream.read(1024)
    stream.seek(0)
    sniffed = _sniff(head)
    if sniffed is None:
        raise ReceiptValidationError(
            "That file is not a readable PDF, PNG, JPG or JPEG."
        )
    # The extension must describe the real content: a PDF renamed .png would
    # otherwise be served back with an image content type.
    if sniffed != ALLOWED_EXTENSIONS[extension]:
        raise ReceiptValidationError(
            "That file's contents do not match its extension — re-save it and try again."
        )

    # Sanitised for storage in the DB and for the download filename. secure_filename
    # can return "" for pathological input (e.g. "..", "/"), so fall back to a
    # generic name that still carries the right extension.
    safe_name = secure_filename(filename)[:MAX_FILENAME_LENGTH]
    if not safe_name or _extension(safe_name) != extension:
        safe_name = f"receipt{extension}"
    return safe_name, sniffed, size


def build_storage_key(client_company_id, extension):
    """``receipts/<company_id>/<uuid4><ext>`` — no user-supplied text anywhere.

    Partitioning by company keeps one tenant's objects under one prefix, which
    is what makes a bucket-policy or per-tenant lifecycle rule expressible when
    this moves to Supabase Storage.
    """
    return f"receipts/{int(client_company_id)}/{uuid.uuid4().hex}{extension}"


def attach_receipt(expense, file_storage, uploaded_by=None):
    """Validate, store, and link a receipt to ``expense``.

    Replaces an existing receipt (the form offers one attachment slot), deleting
    the superseded object so storage does not accumulate orphans. Returns the
    new :class:`~app.models.ExpenseReceipt`.

    The caller commits. If the commit fails the stored object is orphaned rather
    than the row dangling — the safe direction, and recoverable by a sweep.
    """
    safe_name, content_type, _size = validate_upload(file_storage)
    extension = _extension(safe_name)
    key = build_storage_key(expense.client_company_id, extension)

    storage = get_storage()
    file_storage.stream.seek(0)
    written = storage.save(key, file_storage.stream, content_type=content_type)

    previous = expense.receipt
    if previous is not None:
        previous_key = previous.storage_key
        db.session.delete(previous)
        db.session.flush()
        try:
            storage.delete(previous_key)
        except StorageError:
            # The replacement is already stored and linked; failing to remove the
            # superseded file must not fail the upload.
            current_app.logger.warning(
                "Could not delete superseded receipt %s", previous_key
            )

    receipt = ExpenseReceipt(
        expense_id=expense.id,
        # Taken from the expense, never from the request — the same rule the
        # expense routes apply to client_company_id.
        client_company_id=expense.client_company_id,
        original_filename=safe_name,
        storage_key=key,
        content_type=content_type,
        byte_size=written,
        uploaded_by=uploaded_by,
    )
    db.session.add(receipt)
    expense.receipt = receipt
    return receipt


def detach_receipt(expense):
    """Remove ``expense``'s receipt row and its stored object.

    Returns the removed filename, or None when there was nothing attached (so a
    double-submitted delete is a no-op, not an error). The caller commits.
    """
    receipt = expense.receipt
    if receipt is None:
        return None
    key, name = receipt.storage_key, receipt.original_filename
    db.session.delete(receipt)
    expense.receipt = None
    db.session.flush()
    try:
        get_storage().delete(key)
    except StorageError:
        # Row is gone either way; a leftover object is sweepable, a dangling row
        # is not.
        current_app.logger.warning("Could not delete stored receipt %s", key)
    return name
