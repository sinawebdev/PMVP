"""Money arithmetic helpers: decimal.Decimal with explicit ROUND_HALF_UP.

Python's built-in round() uses banker's rounding (round-half-to-even), which
diverges from standard accounting convention exactly at .xx5 boundaries
(56.125 -> 56.12 instead of 56.13). Every money figure in the payroll
calculators is produced through these helpers instead.

Rounding points deliberately match the source workbooks: full precision is
carried WITHIN each statutory component (SSF, ordinary PAYE, overtime tax,
bonus tax), each component is rounded to 2dp exactly once where the workbook
rounds its cell, and totals are sums of the rounded components. Rounding only
at the very end instead would break the verified AC636 fixture (net pay and
total tax reconcile only against component-rounded figures).
"""
from decimal import Decimal, ROUND_HALF_UP

TWO_PLACES = Decimal("0.01")


def D(value):
    """Convert to Decimal via str() so float representation noise never leaks
    into the arithmetic (Decimal(str(0.055)) == Decimal('0.055'))."""
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value or 0))


def round_half_up(value):
    """Quantize to 2 decimal places, halves away from zero."""
    return D(value).quantize(TWO_PLACES, rounding=ROUND_HALF_UP)


def money(value):
    """2dp ROUND_HALF_UP value as a float for storage in the Float columns.
    The quantized Decimal is exact; the float conversion error is far below
    a pesewa and disappears again on display formatting."""
    return float(round_half_up(value))
