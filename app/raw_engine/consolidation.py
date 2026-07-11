"""Consolidate raw hours to one figure per (staff, element).

The raw input can carry several lines for the same worker and element (e.g. a
thin file with daily rows); this sums them into a single hours total per element,
normalising the staff key and element label first so case/spacing variants never
split a worker's hours across two buckets.
"""
from app.raw_engine.cleaning import coerce_hours, normalise_element, normalise_emp_id


def consolidate_hours(rows):
    """``rows``: iterable of ``(staff_id, pay_code, hours)``.
    Returns ``{normalised_staff_id: {pay_code: total_hours}}`` — axes derived
    from the data, never a fixed list."""
    consolidated = {}
    for staff_id, pay_code, hours in rows:
        staff = normalise_emp_id(staff_id)
        code = normalise_element(pay_code)
        if not staff or not code:
            continue
        hrs = coerce_hours(hours)
        bucket = consolidated.setdefault(staff, {})
        bucket[code] = bucket.get(code, 0.0) + hrs
    return consolidated


def total_hours(consolidated):
    """Grand total of all hours in a consolidated map — the anchor the hours
    reconciliation (Phase 4) ties the matrix back to."""
    return sum(h for codes in consolidated.values() for h in codes.values())
