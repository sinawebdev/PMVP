"""GSM-7, the alphabet an SMS is billed in.

One SMS part holds 160 GSM-7 characters ("septets"). A single character outside
GSM-7, such as a curly quote or an em dash, switches the whole message to UCS-2
and cuts the part to 70 characters, so a payslip SMS that looks like one part
gets billed as three. The extension characters (``^{}\\[~]|€`` and form feed)
are GSM-7 but cost two septets each, because each is sent as an escape plus a
character.

Pure functions, no app context.
"""
import re
import unicodedata

# GSM 03.38 basic table, in code-point order, minus the escape at 0x1B.
GSM7_BASIC = frozenset(
    "@£$¥èéùìòÇ\nØø\rÅå"
    "Δ_ΦΓΛΩΠΨΣΘΞÆæßÉ"
    " !\"#¤%&'()*+,-./"
    "0123456789:;<=>?"
    "¡ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§"
    "¿abcdefghijklmnopqrstuvwxyzäöñüà"
)
GSM7_EXTENSION = frozenset("\f^{}\\[~]|€")

# Lookalikes that have a GSM-7 equivalent. Replaced before anything is dropped,
# so "O’Neil – Ltd" keeps its apostrophe and its dash.
_REPLACEMENTS = str.maketrans({
    "‘": "'", "’": "'", "‚": "'", "‛": "'", "′": "'",
    "“": '"', "”": '"', "„": '"', "‟": '"', "″": '"',
    "‐": "-", "‑": "-", "‒": "-", "–": "-", "—": "-",
    "―": "-", "−": "-",
    " ": " ", " ": " ", " ": " ", " ": " ", " ": " ",
})
_WHITESPACE = re.compile(r"\s+")


def septets(text):
    """Septets ``text`` costs in GSM-7, or None if any character is not GSM-7."""
    total = 0
    for char in text:
        if char in GSM7_BASIC:
            total += 1
        elif char in GSM7_EXTENSION:
            total += 2
        else:
            return None
    return total


def clean_for_sms(text):
    """``text`` reduced to GSM-7, for names that go into an SMS.

    Curly quotes become straight, dashes become ``-``, odd spaces become plain
    ones, accents are removed (NFKD), and anything still outside GSM-7 is
    dropped. Whitespace (newlines included) collapses to single spaces.
    """
    value = str(text or "").translate(_REPLACEMENTS)
    value = unicodedata.normalize("NFKD", value)
    value = "".join(char for char in value if not unicodedata.combining(char))
    value = _WHITESPACE.sub(" ", value)
    kept = "".join(
        char for char in value if char in GSM7_BASIC or char in GSM7_EXTENSION
    )
    return _WHITESPACE.sub(" ", kept).strip()


def fit(text, budget, *, whole_words=False):
    """The longest prefix of ``text`` costing at most ``budget`` septets,
    trailing spaces removed. ``text`` must already be GSM-7. With
    ``whole_words``, a cut that would split a word backs off to the last space
    instead, unless the prefix is a single word."""
    used, end = 0, 0
    for index, char in enumerate(text):
        cost = 2 if char in GSM7_EXTENSION else 1
        if used + cost > budget:
            break
        used += cost
        end = index + 1
    cut = text[:end]
    if whole_words and end < len(text) and text[end] != " " and " " in cut:
        cut = cut[:cut.rindex(" ")]
    return cut.rstrip(" -,;:")
