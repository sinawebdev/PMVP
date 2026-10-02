"""Where a payslip goes: the roster employee behind an item, and its SMS number.

Shared by the send path and the confirm-step counter, so the number a worker is
counted under is the number they are sent to.
"""
from app.models import Employee
from app.raw_import import normalise_emp_id

from .phones import normalise_gh_mobile

# Where an SMS number came from, in the order the candidates are tried.
SOURCE_ROSTER_PHONE = "roster_phone"
SOURCE_ROSTER_MOMO = "roster_momo"
SOURCE_PAYROLL_MOMO = "payroll_momo"
# No candidate passed: "invalid" when at least one number existed, "none" when
# there was nothing at all. Both are permanent until someone fixes the roster.
SOURCE_INVALID = "invalid"
SOURCE_NONE = "none"


def roster_employee(item):
    """The roster Employee behind this payroll item.

    The item's own employee relationship (set at import time) is preferred —
    and returned regardless of roster status, because a worker deactivated
    after payday still needs the payslip for work already done. Only items
    that never got linked fall back to an active-roster lookup by normalised
    staff_id."""
    employee = getattr(item, "employee", None)
    if employee is not None:
        return employee
    run = getattr(item, "payroll_run", None)
    client_id = run.client_company_id if run else None
    if not client_id or not item.staff_id:
        return None
    return Employee.query.filter_by(
        client_company_id=client_id,
        staff_id=normalise_emp_id(item.staff_id),
        status="Active",
    ).first()


def sms_contact(item, employee=None):
    """``(number, source)`` for an SMS to ``item``'s worker.

    Tries the roster phone, then the roster MoMo number, then the MoMo number on
    the payroll row, and returns the first that ``normalise_gh_mobile`` accepts,
    normalised (``233XXXXXXXXX``). Invalid candidates are skipped. With none
    accepted, ``number`` is None and ``source`` says whether there was a number
    to reject (``invalid``) or nothing at all (``none``).
    """
    employee = employee if employee is not None else roster_employee(item)
    candidates = (
        (SOURCE_ROSTER_PHONE, getattr(employee, "phone", None)),
        (SOURCE_ROSTER_MOMO, getattr(employee, "momo_number", None)),
        (SOURCE_PAYROLL_MOMO, getattr(item, "momo_number", None)),
    )
    saw_a_number = False
    for source, raw in candidates:
        if raw is None or not str(raw).strip():
            continue
        saw_a_number = True
        number = normalise_gh_mobile(raw)
        if number is not None:
            return number, source
    return None, SOURCE_INVALID if saw_a_number else SOURCE_NONE
