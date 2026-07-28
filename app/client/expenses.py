"""Client (tenant) expense management — self-service CRUD over a company's own
operational expenses.

Expenses used to be read-only in the portal: the operator recorded them from the
oversight console and the client could only look. A client company knows its own
utilities, fuel and maintenance spend far better than the bureau does, so this
module lets them record it themselves.

Every route follows the established tenant-plane contract:

  * reads go through :func:`app.tenancy.tenant_query`, object lookups through
    :func:`app.tenancy.tenant_get_or_404` — another tenant's expense id is a 404,
    never a 403 that confirms the row exists;
  * ``client_company_id`` is forced to the active tenant on save and never taken
    from the form, so a client can only ever create expenses under their own
    company;
  * mutation is gated to the same tenant roles that may prepare a payroll run
    (``client_admin`` / ``client_preparer``); viewing is any tenant user. No new
    permission concept is introduced.

Totals computed here feed :mod:`app.analytics`, which is what the company
dashboard's "Total expenses" stat and expenses-vs-payroll donut already read —
so a newly recorded expense shows up in the dashboard on the next load with no
cache to invalidate.

Attached to ``client_bp`` at import time; :mod:`app.client` imports this module
at the bottom of its own file (the same pattern as ``raw`` and ``reports``).
"""

from datetime import date, datetime

from flask import flash, redirect, render_template, request, url_for
from flask_login import current_user

from app import db
from app.analytics import expense_summary
from app.audit import record_audit
from app.client import _company, client_bp
from app.models import Expense
from app.permissions import EXPENSE_ROLES
from app.tenancy import tenant_get_or_404, tenant_query, tenant_required, tenant_role_required

# The operational spend categories a client company records. Deliberately a
# short, closed list: a free-text category makes the dashboard breakdown
# meaningless within a month ("Fuel", "fuel", "Fuel " become three slices).
EXPENSE_CATEGORIES = (
    "Utilities",
    "Fuel",
    "Internet",
    "Maintenance",
    "Office Supplies",
    "Travel",
    "Miscellaneous",
)

# What a client may record against an expense. The operator-side approval
# vocabulary (Pending/Approved) is not exposed here — a client recording their
# own spend is not an approval workflow.
EXPENSE_STATUS = "Recorded"


def _parse_date(value):
    try:
        return datetime.strptime((value or "").strip(), "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None


def _parse_amount(value):
    """Positive money from a typed amount, or None when it isn't one.

    Returns None for blanks, non-numeric text, and zero/negative amounts — all
    of which are form errors, not silently-zero expenses."""
    try:
        amount = round(float(str(value or "").replace(",", "").strip()), 2)
    except (TypeError, ValueError):
        return None
    return amount if amount > 0 else None


def validate_expense_form(form):
    """(values, errors) for a submitted expense.

    ``values`` carries parsed, normalised fields (``expense_date`` as a date,
    ``amount`` as a float) plus ``raw``, the untouched submission echoed back
    when the form is re-rendered. An empty ``errors`` dict means it is savable.
    """
    raw = {
        "expense_date": (form.get("expense_date") or "").strip(),
        "category": (form.get("category") or "").strip(),
        "description": (form.get("description") or "").strip(),
        "amount": (form.get("amount") or "").strip(),
        "notes": (form.get("notes") or "").strip(),
    }
    errors = {}

    expense_date = _parse_date(raw["expense_date"])
    if expense_date is None:
        errors["expense_date"] = "Choose the date the expense was incurred."
    elif expense_date > date.today():
        errors["expense_date"] = "An expense cannot be dated in the future."

    if raw["category"] not in EXPENSE_CATEGORIES:
        errors["category"] = "Choose a category from the list."

    if not raw["description"]:
        errors["description"] = "Describe what the money was spent on."
    elif len(raw["description"]) > 255:
        errors["description"] = "Keep the description under 255 characters."

    amount = _parse_amount(raw["amount"])
    if amount is None:
        errors["amount"] = "Enter an amount greater than zero."

    return (
        {
            "expense_date": expense_date,
            "category": raw["category"],
            "description": raw["description"],
            "amount": amount,
            "notes": raw["notes"],
            "raw": raw,
        },
        errors,
    )


def _render_form(company, expense, values, errors):
    return render_template(
        "client/expense_form.html",
        company=company,
        expense=expense,
        values=values,
        errors=errors,
        categories=EXPENSE_CATEGORIES,
        today=date.today().isoformat(),
    )


def _form_values(expense=None):
    """Initial form values: an existing expense's fields, or today's blank form."""
    if expense is None:
        return {
            "raw": {
                "expense_date": date.today().isoformat(),
                "category": "",
                "description": "",
                "amount": "",
                "notes": "",
            }
        }
    return {
        "raw": {
            "expense_date": expense.expense_date.isoformat() if expense.expense_date else "",
            "category": expense.category or "",
            "description": expense.description or "",
            "amount": f"{expense.amount or 0:.2f}",
            "notes": expense.notes or "",
        }
    }


@client_bp.route("/expenses")
@tenant_required
def expenses():
    """The company's expense ledger with its own totals and category breakdown.

    The same figures feed the dashboard (via :func:`app.analytics.expense_summary`
    and the dashboard's expense total), so the two surfaces can never disagree.
    """
    rows = (
        tenant_query(Expense)
        .order_by(Expense.expense_date.desc(), Expense.id.desc())
        .all()
    )
    return render_template(
        "client/expenses.html",
        company=_company(),
        expenses=rows,
        summary=expense_summary(rows),
    )


@client_bp.route("/expenses/add", methods=["GET", "POST"])
@tenant_role_required(*EXPENSE_ROLES)
def expense_add():
    company = _company()
    if request.method == "POST":
        values, errors = validate_expense_form(request.form)
        if errors:
            return _render_form(company, None, values, errors)
        expense = Expense(
            # Forced to the active tenant — never read from the form.
            client_company_id=company.id,
            status=EXPENSE_STATUS,
            recorded_by=current_user.id,
        )
        db.session.add(expense)
        _apply(expense, values)
        db.session.flush()
        record_audit(
            "Expense recorded",
            expense,
            f"{expense.category}: {expense.description} — {expense.amount:.2f} "
            f"on {expense.expense_date}.",
        )
        db.session.commit()
        flash(f"Expense recorded: {expense.description}.", "success")
        return redirect(url_for("client.expenses"))
    return _render_form(company, None, _form_values(), {})


@client_bp.route("/expenses/<int:expense_id>/edit", methods=["GET", "POST"])
@tenant_role_required(*EXPENSE_ROLES)
def expense_edit(expense_id):
    expense = tenant_get_or_404(Expense, expense_id)  # 404 if another tenant's
    company = _company()
    if request.method == "POST":
        values, errors = validate_expense_form(request.form)
        if errors:
            return _render_form(company, expense, values, errors)
        _apply(expense, values)
        # Keep the tenant binding invariant even on edit.
        expense.client_company_id = company.id
        record_audit(
            "Expense updated",
            expense,
            f"{expense.category}: {expense.description} — {expense.amount:.2f} "
            f"on {expense.expense_date}.",
        )
        db.session.commit()
        flash("Expense updated.", "success")
        return redirect(url_for("client.expenses"))
    return _render_form(company, expense, _form_values(expense), {})


@client_bp.route("/expenses/<int:expense_id>/delete", methods=["POST"])
@tenant_role_required(*EXPENSE_ROLES)
def expense_delete(expense_id):
    expense = tenant_get_or_404(Expense, expense_id)  # 404 if another tenant's
    description = expense.description
    record_audit(
        "Expense deleted",
        expense,
        f"{expense.category}: {description} — {expense.amount or 0:.2f} deleted by client.",
    )
    db.session.delete(expense)
    db.session.commit()
    flash(f"Expense deleted: {description}.", "success")
    return redirect(url_for("client.expenses"))


def _apply(expense, values):
    expense.expense_date = values["expense_date"]
    expense.category = values["category"]
    expense.description = values["description"]
    expense.amount = values["amount"]
    expense.notes = values["notes"] or None
    # An expense the client typed carries its description as the title, so the
    # operator's Expenses & Audit view (which prefers title) reads identically.
    expense.title = values["description"][:180]
