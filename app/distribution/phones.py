"""Ghana mobile numbers: one rule for what counts as a reachable number.

Pure functions, no app context and no database. Called at send time, by the
contact predicate (``Employee.has_contact``) and by the confirm-step counter.
Roster and payroll rows are never rewritten: production data stays exactly as
it was imported, and the normalised form exists only on the way out.

The 9-digit case is the one that matters in production. Every MoMo number on
the live roster lost its leading 0 to Excel, so without restoring it SMS would
reach no one. Restoring it is only safe because the network prefix is checked
too: a 9-digit string that does not start with a known mobile prefix is
rejected rather than guessed at.
"""
import re

# The two digits after the trunk 0 (or after 233) name the network. A number
# whose prefix is not here is not a Ghana mobile number Payrolla sends to.
GH_MOBILE_PREFIXES = frozenset({
    "20", "50",                          # Telecel
    "24", "25", "53", "54", "55", "59",  # MTN
    "26", "27", "56", "57",              # AirtelTigo
})
# 23 (Glo) and 28 (Expresso) are left out by decision (Q1, 2026-10-01):
# Payrolla does not serve those networks.

_SEPARATORS = re.compile(r"[\s\-.()]")
_ASCII_DIGITS = re.compile(r"[0-9]+")


def normalise_gh_mobile(raw):
    """``233XXXXXXXXX`` for a Ghana mobile number, or None if it is not one.

    Accepts ``0XXXXXXXXX``, ``+233XXXXXXXXX``, ``233XXXXXXXXX`` and a bare
    9-digit number (the leading 0 restored), after stripping spaces, dashes,
    dots and brackets. In every form the network prefix must be in
    :data:`GH_MOBILE_PREFIXES`. Everything else is rejected: landlines,
    wrong lengths, letters, other countries.
    """
    if raw is None:
        return None
    value = _SEPARATORS.sub("", str(raw))
    if value.startswith("+"):
        value = value[1:]
        if not value.startswith("233"):
            return None
    # ASCII digits only. str.isdigit() would also accept characters such as
    # superscripts and Arabic-Indic digits, which no network would route.
    if not _ASCII_DIGITS.fullmatch(value):
        return None
    if len(value) == 12 and value.startswith("233"):
        national = value[3:]
    elif len(value) == 10 and value.startswith("0"):
        national = value[1:]
    elif len(value) == 9:
        national = value
    else:
        return None
    if national[:2] not in GH_MOBILE_PREFIXES:
        return None
    return "233" + national


def mask_msisdn(number):
    """``23324***347``: enough to recognise a number, not enough to dial it.

    For templates only. Logs use ``channels.recipient_fingerprint`` instead,
    because a masked number is still partly readable and logs travel further
    than a screen."""
    if not number:
        return ""
    digits = normalise_gh_mobile(number) or re.sub(r"\D", "", str(number))
    if len(digits) < 8:
        return "***"
    return f"{digits[:5]}***{digits[-3:]}"
