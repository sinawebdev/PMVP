"""Short payslip links for SMS: ``/s/<code>``.

A signed ``/p/<token>`` link is too long for a one-part SMS, so an SMS carries a
12-character random code instead. The code is a bearer secret for one payslip:

* **Random, not derived.** 12 base62 characters from ``secrets`` is 71.5 bits;
  guessing one live code is out of reach whatever the rate limit.
* **Never stored.** Only ``HMAC-SHA256(PAYSLIP_TOKEN_KEY, code)`` is kept, so a
  database read does not hand out working links. The plain code exists only in
  the outgoing SMS. Rotating the key kills every code, exactly as it kills every
  signed ``/p/`` token.
* **Expiring.** Each code carries its own ``expires_at`` (PAYSLIP_SMS_LINK_DAYS).
* **Revocable, two ways.** ``revoked_at`` kills one code. Each code also records
  the item's ``payslip_token_version`` when it was minted, so the existing
  :func:`app.distribution.tokens.revoke_payslip_links` kills short links too.

Re-sending a delivery mints a fresh code (the old one cannot be recovered from
its hash) and revokes that delivery's earlier codes. A delivery whose last send
ended ``unknown`` is never re-sent automatically, so its code, which may already
be on the worker's phone, stays valid until someone settles it.
"""
import hashlib
import hmac
import re
import secrets
import string
from datetime import datetime, timedelta, timezone

from flask import current_app, has_request_context, request

from app import db
from app.models import PayrollItem, utc_now

from .tokens import _version_of

CODE_LENGTH = 12
_ALPHABET = string.ascii_letters + string.digits
_CODE = re.compile(rf"[A-Za-z0-9]{{{CODE_LENGTH}}}")


class PayslipLink(db.Model):
    __tablename__ = "payslip_link"

    id = db.Column(db.Integer, primary_key=True)
    code_hash = db.Column(db.String(64), nullable=False, unique=True, index=True)
    # CASCADE so deleting a run (which bulk-deletes its deliveries and items)
    # takes its links with it, with no change to the delete paths.
    payroll_item_id = db.Column(
        db.Integer,
        db.ForeignKey("payroll_item.id", name="fk_payslip_link_payroll_item_id",
                      ondelete="CASCADE"),
        nullable=False, index=True,
    )
    payslip_delivery_id = db.Column(
        db.Integer,
        db.ForeignKey("payslip_delivery.id", name="fk_payslip_link_payslip_delivery_id",
                      ondelete="CASCADE"),
        index=True,
    )
    token_version = db.Column(db.Integer, nullable=False, default=0)
    expires_at = db.Column(db.DateTime, nullable=False)
    revoked_at = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=utc_now)


def _aware(value):
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def code_hash(code):
    key = str(current_app.config["PAYSLIP_TOKEN_KEY"]).encode("utf-8")
    return hmac.new(key, code.encode("utf-8"), hashlib.sha256).hexdigest()


def _public_base():
    base = current_app.config.get("PUBLIC_BASE_URL")
    if not base and has_request_context():
        base = request.url_root
    return base.rstrip("/") if base else None


def revoke_delivery_links(delivery_id, *, now=None):
    """Revoke every live code minted for one delivery. The caller commits."""
    return PayslipLink.query.filter(
        PayslipLink.payslip_delivery_id == delivery_id,
        PayslipLink.revoked_at.is_(None),
    ).update({PayslipLink.revoked_at: now or datetime.now(timezone.utc)},
             synchronize_session=False)


def mint_payslip_link(item, delivery=None, *, now=None):
    """Mint a new code for ``item`` and return it in plain text, the only time it
    exists. Revokes ``delivery``'s earlier codes first. The caller commits, and
    must commit before the code is sent, or a crash loses a code already on a
    phone."""
    now = now or datetime.now(timezone.utc)
    days = int(current_app.config.get("PAYSLIP_SMS_LINK_DAYS", 30))
    delivery_id = getattr(delivery, "id", None)
    if delivery_id is not None:
        revoke_delivery_links(delivery_id, now=now)
    code = "".join(secrets.choice(_ALPHABET) for _ in range(CODE_LENGTH))
    db.session.add(PayslipLink(
        code_hash=code_hash(code),
        payroll_item_id=item.id,
        payslip_delivery_id=delivery_id,
        token_version=_version_of(item),
        expires_at=now + timedelta(days=days),
        created_at=now,
    ))
    return code


def short_payslip_url(item, delivery=None):
    """``{PUBLIC_BASE_URL}/s/<code>`` for a fresh code, or None (and nothing
    minted) when no public host is known, as with ``public_payslip_url``."""
    base = _public_base()
    if not base:
        return None
    return f"{base}/s/{mint_payslip_link(item, delivery)}"


def resolve_payslip_link(code, *, now=None):
    """The PayrollItem a code opens, or None for any reason at all: malformed,
    unknown, expired, revoked, or minted before the item's links were revoked.
    A malformed code is refused before the database is asked."""
    if not isinstance(code, str) or not _CODE.fullmatch(code):
        return None
    link = PayslipLink.query.filter_by(code_hash=code_hash(code)).first()
    if link is None or link.revoked_at is not None:
        return None
    if _aware(link.expires_at) <= (now or datetime.now(timezone.utc)):
        return None
    item = db.session.get(PayrollItem, link.payroll_item_id)
    if item is None or link.token_version != _version_of(item):
        return None
    return item


def revoke_code(code, *, now=None):
    """Revoke one code. True if a live code was revoked. The caller commits."""
    if not isinstance(code, str) or not _CODE.fullmatch(code):
        return False
    link = PayslipLink.query.filter_by(code_hash=code_hash(code)).first()
    if link is None or link.revoked_at is not None:
        return False
    link.revoked_at = now or datetime.now(timezone.utc)
    return True
