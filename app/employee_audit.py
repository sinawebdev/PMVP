"""Every change to an employee record, in the audit trail.

The trail used to record employee changes in a few places only (a client-portal
save, a pay-type flip, a hard delete), so the public pages could not honestly
say "every change is logged". Worse, the change that matters most for payroll
fraud, a worker's bank account quietly swapped, could happen through a payroll
upload (app.payroll), a raw-hours upload (app.raw_engine.store), a roster import
(app.roster_import) or either edit form, and none of them said so.

So this is done once, at the database session, not in each route: after every
flush, each Employee that was added or changed gets one AuditTrail row saying
what changed, old value to new. A code path added later is covered without
anyone remembering to call anything.

Rules:

* Only changes made by a signed-in person in a web request are recorded. Seeding,
  demo resets and CLI scripts run outside a request and are not user activity.
* Identity and payment numbers (bank account, MoMo, SSNIT, Ghana Card, TIN) are
  shown by their last four characters only. The trail is read by the company's
  own people and by its bureau, and it records THAT a number changed and which
  number it became, which is what an investigation needs, without copying
  every worker's full account number into a second table.
* Deletes are not handled here. The one route that deletes an employee
  (employees.delete) records its own row, with the reason it was allowed.
* An import names where its changes came from (``set_change_source``), so the
  row reads "Via payroll upload for March 2026." rather than leaving the reader
  to guess. The label lasts for the rest of the request, because the changes
  are only written at the flush, which is often after the import function has
  returned.
"""
from datetime import date

from flask import g, has_request_context
from sqlalchemy import event, inspect

# Column -> label, in the order changes are listed. Columns not named here
# (ids, timestamps, the tenant binding) are bookkeeping, not employee data.
TRACKED_FIELDS = (
    ("full_name", "Name"),
    ("staff_id", "Staff ID"),
    ("status", "Status"),
    ("basic_salary", "Basic salary"),
    ("pay_type", "Pay type"),
    ("bank_name", "Bank"),
    ("bank_branch", "Bank branch"),
    ("bank_account_number", "Bank account"),
    ("momo_number", "MoMo number"),
    ("phone", "Phone"),
    ("email", "Email"),
    ("ssnit_number", "SSNIT number"),
    ("ghana_card_number", "Ghana Card"),
    ("tin", "TIN"),
    ("department", "Department"),
    ("job_title", "Job title"),
    ("employment_type", "Employment type"),
    ("service_line", "Service line"),
    ("preferred_channel", "Payslip channel"),
    ("icu_member", "ICU member"),
    ("tax_relief_monthly", "Monthly tax relief"),
    ("tax_relief_approval_ref", "Tax relief approval"),
    ("tax_relief_category", "Tax relief category"),
    ("tax_relief_valid_from", "Tax relief from"),
    ("tax_relief_valid_to", "Tax relief to"),
    ("overtime_is_junior", "Junior staff (overtime)"),
    ("overtime_qualifying_annual_income", "Overtime qualifying income"),
    ("overtime_income_year", "Overtime income year"),
    ("pf_scheme_approved", "Approved provident fund"),
    ("pf_scheme_reference", "Provident fund reference"),
)
MASKED_FIELDS = frozenset(
    {"bank_account_number", "momo_number", "ssnit_number", "ghana_card_number", "tin"}
)
MONEY_FIELDS = frozenset({"basic_salary", "tax_relief_monthly", "overtime_qualifying_annual_income"})


def set_change_source(label):
    """Name where this request's employee changes come from, e.g.
    ``set_change_source("payroll upload for March 2026")``. No-op outside a
    request (seeding, CLI), where nothing is recorded anyway."""
    if has_request_context():
        g._employee_change_source = label


def _masked(value):
    text = str(value).strip()
    return "••" + text[-4:] if len(text) > 4 else "••••"


def _shown(field, value):
    if value is None or value == "":
        return "blank"
    if field in MASKED_FIELDS:
        return _masked(value)
    if field in MONEY_FIELDS:
        try:
            return f"{float(value):,.2f}"
        except (TypeError, ValueError):
            return str(value)
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


def _same(field, old, new):
    if field in MONEY_FIELDS:
        try:
            return round(float(old or 0), 2) == round(float(new or 0), 2)
        except (TypeError, ValueError):
            pass
    return (old if old != "" else None) == (new if new != "" else None)


def describe_changes(employee):
    """['Bank account ••1234 → ••5678', ...] for a dirty, persistent employee."""
    state = inspect(employee)
    changes = []
    for field, label in TRACKED_FIELDS:
        history = state.attrs[field].history
        if not history.has_changes():
            continue
        old = history.deleted[0] if history.deleted else None
        new = history.added[0] if history.added else None
        if _same(field, old, new):
            continue
        changes.append(f"{label} {_shown(field, old)} → {_shown(field, new)}")
    return changes


def _status_action(employee, changes):
    """A plainer action name when the only change is the status."""
    if len(changes) == 1 and changes[0].startswith("Status "):
        return "Employee deactivated" if employee.status != "Active" else "Employee reactivated"
    return "Employee updated"


def _signed_in_user():
    if not has_request_context():
        return None
    from flask_login import current_user

    return current_user if getattr(current_user, "is_authenticated", False) else None


def _after_flush(session, flush_context):
    from app.models import AuditTrail, Employee

    user = _signed_in_user()
    if user is None:
        return
    source = getattr(g, "_employee_change_source", None)
    suffix = f" Via {source}." if source else ""
    rows = []
    for obj in session.new:
        if isinstance(obj, Employee):
            rows.append(("Employee added", obj, f"{obj.full_name} ({obj.staff_id}) added.{suffix}"))
    for obj in session.dirty:
        if not isinstance(obj, Employee) or obj in session.deleted:
            continue
        changes = describe_changes(obj)
        if changes:
            rows.append((_status_action(obj, changes), obj,
                         f"{obj.full_name} ({obj.staff_id}): {'; '.join(changes)}.{suffix}"))
    for action, employee, notes in rows:
        session.add(AuditTrail(
            user_id=user.id, user_role=user.role, action=action,
            related_record_type="Employee", related_record_id=employee.id, notes=notes,
        ))


def register(db):
    """Listen on the app's session. Rows added here are pending until the next
    flush, which commit() runs before it finishes. Idempotent: create_app() runs
    once per test, and a second listener would write every row twice."""
    if not event.contains(db.session, "after_flush", _after_flush):
        event.listen(db.session, "after_flush", _after_flush)
