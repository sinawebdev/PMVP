"""Signed, expiring links to a single worker's payslip — no login required.

This is the "merge" of the competitors' self-service portal with our push model:
the SMS / WhatsApp / email we send carries a tokenized link that opens the worker's
own payslip on a phone, with no password. The token is a signed (not encrypted)
``itsdangerous`` value over the SECRET_KEY, scoped to one ``PayrollItem`` and time-
limited by ``PAYSLIP_LINK_MAX_AGE``; tampering or expiry simply fails to load.
"""
from flask import current_app, has_request_context, request
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

_SALT = "payslip-public-link"
_DEFAULT_MAX_AGE = 60 * 60 * 24 * 30  # 30 days


def _serializer():
    return URLSafeTimedSerializer(current_app.config["SECRET_KEY"], salt=_SALT)


def issue_payslip_token(item_id) -> str:
    return _serializer().dumps({"item": int(item_id)})


def verify_payslip_token(token):
    """Return the PayrollItem id for a valid, unexpired token, else ``None``."""
    max_age = int(current_app.config.get("PAYSLIP_LINK_MAX_AGE", _DEFAULT_MAX_AGE))
    try:
        data = _serializer().loads(token, max_age=max_age)
    except (BadSignature, SignatureExpired):
        return None
    if not isinstance(data, dict):
        return None
    return data.get("item")


def public_payslip_url(item_id):
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
    return f"{base.rstrip('/')}/p/{issue_payslip_token(item_id)}"
