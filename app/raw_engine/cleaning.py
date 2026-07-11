"""Normalisation helpers for the raw-hours engine.

The staff-ID join key reuses the existing :func:`normalise_emp_id` so a worker
resolves identically across the roster, the rich seed, and thin monthly files.
:func:`normalise_element` is the raw-engine addition: Excel's ``=`` compares
labels case-insensitively, but Python ``==`` does not — grouping hours by a
raw label silently dropped "Overtime weekday" against "Overtime Weekday" in an
earlier specimen. Every element label is passed through here before matching.
"""
import re

from app.raw_import import normalise_emp_id  # re-exported: single source of truth

__all__ = ["normalise_emp_id", "normalise_element", "coerce_hours", "coerce_rate"]


def normalise_element(label) -> str:
    """Canonicalise a pay-element label for case-insensitive matching:
    upper-case, collapse internal whitespace, strip. "6 TO 6" and "6  to  6"
    both become "6 TO 6"; "Overtime weekday" == "OVERTIME WEEKDAY"."""
    return re.sub(r"\s+", " ", str(label or "").strip()).upper()


def coerce_hours(value) -> float:
    """A cell's hours as a float; blank / non-numeric / negative -> 0.0.
    Blank means zero for the month (never carried forward)."""
    try:
        hours = float(value)
    except (TypeError, ValueError):
        return 0.0
    if hours != hours or hours < 0:  # NaN or negative
        return 0.0
    return hours


def coerce_rate(value) -> float:
    """A rate cell as a float; blank / non-numeric / negative -> 0.0."""
    try:
        rate = float(value)
    except (TypeError, ValueError):
        return 0.0
    if rate != rate or rate < 0:
        return 0.0
    return rate
