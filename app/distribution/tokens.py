"""Signed, expiring, revocable links to a single worker's payslip — no login required.

This is the "merge" of the competitors' self-service portal with our push model:
the SMS / WhatsApp / email we send carries a tokenized link that opens the worker's
own payslip on a phone, with no password.

Three properties, and each exists because the previous version lacked it:

* **Signed with its own key.** The token is signed with ``PAYSLIP_TOKEN_KEY``,
  *not* ``SECRET_KEY``. It used to share the session key, which meant one leaked
  value both forged session cookies and minted payslip links for any item id.
  ``itsdangerous``'s ``salt`` does not help here — that is domain separation
  between uses of the *same* key, not key separation — so the keys are genuinely
  distinct, and production refuses to boot if the payslip key is missing or is a
  copy of the session key (``app/__init__.py``).

* **Short lived.** ``PAYSLIP_LINK_MAX_AGE`` defaults to 5 days, down from 30. A
  payslip link is read within hours of delivery in practice; a month-long window
  was exposure bought for almost no convenience.

* **Revocable.** The payload carries the ``payslip_token_version`` the item held
  when the link was issued, and :func:`resolve_payslip_item` honours the link
  only while that still matches the row. A signed token is otherwise irrevocable
  by construction — nothing stores it, so there is nothing to delete — and
  before this the only answer to "that went to the wrong number" was to wait out
  the expiry. :func:`revoke_payslip_links` increments the column, which
  invalidates every link already issued for that one payslip and nothing else.

Rotating ``PAYSLIP_TOKEN_KEY`` invalidates every outstanding link at once. That
is the intended behaviour: there is one key, no overlap window.
"""
from flask import current_app, has_request_context, request
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from app import db
from app.models import PayrollItem

_SALT = "payslip-public-link"
_DEFAULT_MAX_AGE = 60 * 60 * 24 * 5  # 5 days


def _serializer():
    # Deliberately NOT SECRET_KEY — see the module docstring. create_app
    # guarantees this key exists and differs from the session key.
    return URLSafeTimedSerializer(current_app.config["PAYSLIP_TOKEN_KEY"], salt=_SALT)


def _version_of(item):
    """The revocation counter for an item, tolerating a row that predates the
    column (treated as 0, which is what the migration backfills)."""
    return int(getattr(item, "payslip_token_version", 0) or 0)


def _identify(item_or_id):
    """``(item_id, version)`` from either a PayrollItem or a bare id.

    Callers that already hold the item (the distribution send does) pass it in
    and cost no query; a bare id is looked up. An id with no row issues at
    version 0 rather than raising — the token simply will not resolve later.
    """
    if hasattr(item_or_id, "id"):
        return int(item_or_id.id), _version_of(item_or_id)
    item_id = int(item_or_id)
    item = db.session.get(PayrollItem, item_id)
    return item_id, _version_of(item) if item is not None else 0


def issue_payslip_token(item_or_id) -> str:
    item_id, version = _identify(item_or_id)
    return _serializer().dumps({"item": item_id, "v": version})


def verify_payslip_token(token):
    """Return the PayrollItem id for a structurally valid, unexpired token.

    Checks the signature and the age only. It does **not** check revocation,
    because that needs the row — use :func:`resolve_payslip_item` for anything
    that actually serves a payslip.
    """
    max_age = int(current_app.config.get("PAYSLIP_LINK_MAX_AGE", _DEFAULT_MAX_AGE))
    try:
        data = _serializer().loads(token, max_age=max_age)
    except (BadSignature, SignatureExpired):
        return None
    if not isinstance(data, dict):
        return None
    return data.get("item")


def resolve_payslip_item(token):
    """The :class:`~app.models.PayrollItem` a token authorises, or ``None``.

    The single entry point for serving a tokenised payslip: signature, expiry,
    existence and revocation are all decided here, so a caller cannot serve a
    payslip having checked only some of them. Returns None for every failure —
    an expired link, a forged one, and a revoked one are indistinguishable to
    the reader, which is the correct amount to tell them.
    """
    max_age = int(current_app.config.get("PAYSLIP_LINK_MAX_AGE", _DEFAULT_MAX_AGE))
    try:
        data = _serializer().loads(token, max_age=max_age)
    except (BadSignature, SignatureExpired):
        return None
    if not isinstance(data, dict) or "item" not in data:
        return None

    item = db.session.get(PayrollItem, data["item"])
    if item is None:
        return None
    # A token minted before this column existed carries no "v". It is not
    # treated as version 0 — it is refused, because such a token was signed with
    # the old SECRET_KEY anyway and cannot reach here with a valid signature.
    # Being explicit means a future payload change fails closed as well.
    if "v" not in data:
        return None
    if int(data["v"]) != _version_of(item):
        return None
    return item


def revoke_payslip_links(item):
    """Invalidate every link already issued for ``item``. The caller commits.

    Revocation is an increment: existing tokens carry the old value and stop
    matching, and the next link issued carries the new one. Nothing else is
    affected — not other workers, not other runs, not the signing key.
    """
    item.payslip_token_version = _version_of(item) + 1
    db.session.add(item)
    return item.payslip_token_version


def public_payslip_url(item_or_id):
    """Absolute ``/p/<token>`` URL for a payslip, or ``None`` if no base URL is known.

    Prefers the configured ``PUBLIC_BASE_URL`` (so links in messages match the real
    public host); falls back to the current request's host. Outside a request and with
    no config (e.g. unit tests), returns ``None`` so callers simply omit the link.
    """
    base = current_app.config.get("PUBLIC_BASE_URL")
    if not base and has_request_context():
        base = request.url_root.rstrip("/")
    if not base:
        return None
    return f"{base.rstrip('/')}/p/{issue_payslip_token(item_or_id)}"
