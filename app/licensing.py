"""Issue Payrolla Desktop trial licences from the web app.

A desktop licence is an Ed25519 signature over a fixed list of fields; the
desktop app checks it offline at every launch (payrolla-desktop,
``desktop/licensing.py``). This module is the issuing half, so the server can
send a trial licence the moment an operator approves an access request.

**The payload format is copied, not shared.** The desktop repository vendors
this one, not the other way round, so the web app cannot import the desktop's
code. ``signing_payload`` below must stay byte-for-byte identical to the
desktop's; ``tests/test_desktop_licences.py`` pins it with a golden signature
produced by the desktop's own ``sign()``, so a drift fails CI here instead of
on a customer's machine.

**The key is a separate trial key.** ``TRIAL_LICENCE_SIGNING_KEY`` is not the
master key Sinaforte signs paid licences with. Public desktop builds from
2026-11-06 trust both; if the server key ever leaks, the next build drops it
and the master key is untouched.
"""

import base64
import os
import re
from datetime import date

PAYLOAD_VERSION = "v2"
_PREFIX = "payrolla-desktop"
SIGNED_FIELDS = (
    "install_id",
    "license_expiry",
    "firm_name",
    "license_id",
    "plan",
    "issued",
    "max_employees",
    "features",
)
_DELIMITER = "|"
_FEATURE_SEPARATOR = ","

TRIAL_PLAN = "Trial"
# Professional's feature set: a trial should show the product as it will be bought.
TRIAL_FEATURES = (
    "payroll", "payslips", "employee_management", "exports",
    "bank_listing", "statutory_filings", "approvals", "audit_log",
    "notifications",
)
# Matches desktop/firm_config.py: the install id becomes a directory name.
INSTALL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class LicenseError(Exception):
    """A licence could not be built from the given terms."""


def canonical_features(features):
    if features is None:
        return ""
    if isinstance(features, str):
        raise LicenseError("features must be a list of strings, not a single string.")
    cleaned = []
    for feature in features:
        text = str(feature).strip()
        if not text:
            continue
        if _FEATURE_SEPARATOR in text or _DELIMITER in text:
            raise LicenseError(f"feature {text!r} may not contain ',' or '|'.")
        cleaned.append(text)
    return _FEATURE_SEPARATOR.join(sorted(set(cleaned)))


def canonical_max_employees(value):
    try:
        count = int(str(value).strip())
    except (TypeError, ValueError):
        raise LicenseError(f"max_employees must be a whole number, got {value!r}.")
    if count < 1:
        raise LicenseError(f"max_employees must be at least 1, got {count}.")
    return str(count)


def signing_payload(terms):
    """The exact bytes a licence signature covers. Identical to the desktop's."""
    parts = [_PREFIX, PAYLOAD_VERSION]
    for field in SIGNED_FIELDS:
        if field == "features":
            value = canonical_features(terms.get("features"))
        elif field == "max_employees":
            value = canonical_max_employees(terms.get("max_employees"))
        else:
            value = str(terms.get(field) or "").strip()
            if not value:
                raise LicenseError(f"licence field {field!r} is empty.")
        if _DELIMITER in value:
            raise LicenseError(f"licence field {field!r} may not contain '|': {value!r}")
        parts.append(value)
    return _DELIMITER.join(parts).encode("utf8")


def _b64url(raw):
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64url_decode(value):
    text = str(value or "").strip()
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def sign(private_key_b64, terms):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    key = Ed25519PrivateKey.from_private_bytes(_b64url_decode(private_key_b64))
    return _b64url(key.sign(signing_payload(terms)))


def public_key_of(private_key_b64):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    key = Ed25519PrivateKey.from_private_bytes(_b64url_decode(private_key_b64))
    return _b64url(key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw))


def verify(public_key_b64, document):
    """True when ``document`` carries a valid signature for its own terms."""
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    key = Ed25519PublicKey.from_public_bytes(_b64url_decode(public_key_b64))
    try:
        key.verify(_b64url_decode(document["license_key"]), signing_payload(document))
        return True
    except (InvalidSignature, KeyError, ValueError):
        return False


# --- issuing ------------------------------------------------------------------

def slugify(company):
    slug = re.sub(r"[^A-Za-z0-9]+", "-", str(company or "").strip().lower()).strip("-")
    return slug or "firm"


def mint_install_id(company):
    """Firm slug plus randomness, the same shape the Sinaforte tool mints."""
    suffix = _b64url(os.urandom(4)).lower().replace("-", "").replace("_", "")[:6]
    candidate = f"{slugify(company)[:50]}-{suffix}"
    if not INSTALL_ID_RE.match(candidate):
        raise LicenseError(f"generated an unusable install_id: {candidate!r}")
    return candidate


def mint_license_id(issued):
    """``TR-<year>-XXXXXX``: TR so a trial is recognisable from its id alone."""
    return f"TR-{issued.year}-{_b64url(os.urandom(4)).upper()[:6]}"


def clean_firm_name(name):
    """A firm name that can be signed: no '|', no control characters, bounded."""
    text = re.sub(r"[\x00-\x1f\x7f|]", " ", str(name or ""))
    text = re.sub(r"\s+", " ", text).strip()[:160]
    if not text:
        raise LicenseError("the firm name is empty.")
    return text


def issue_trial(private_key_b64, firm_name, expiry, max_employees, issued=None):
    """A complete, signed licence document (what the desktop saves as firm.json)."""
    issued = issued or date.today()
    if expiry <= issued:
        raise LicenseError(f"expiry {expiry} is not after the issue date {issued}.")
    firm = clean_firm_name(firm_name)
    terms = {
        "install_id": mint_install_id(firm),
        "license_expiry": expiry.isoformat(),
        "firm_name": firm,
        "license_id": mint_license_id(issued),
        "plan": TRIAL_PLAN,
        "issued": issued.isoformat(),
        "max_employees": int(max_employees),
        "features": list(TRIAL_FEATURES),
    }
    document = dict(terms, license_key=sign(private_key_b64, terms),
                    data_dir="%LOCALAPPDATA%/Payrolla")
    if not verify(public_key_of(private_key_b64), document):
        raise LicenseError("the licence just signed does not verify.")
    return document
