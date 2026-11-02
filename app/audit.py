from datetime import date, datetime, time

from flask import Blueprint, flash, has_request_context, redirect, render_template, request, url_for
from flask_login import current_user

from app import db
from app.auth import role_required
from app.permissions import AUDIT_ROLES
from app.models import AuditTrail, ClientCompany, Expense, PayrollRun

audit_bp = Blueprint("audit", __name__, url_prefix="/audit")


def record_audit(action, related_record=None, notes=""):
    record_type = related_record.__class__.__name__ if related_record is not None else None
    record_id = getattr(related_record, "id", None) if related_record is not None else None
    if has_request_context() and current_user and current_user.is_authenticated:
        user_id = current_user.id
        user_role = current_user.role
    else:
        user_id = None
        user_role = "system"
    db.session.add(
        AuditTrail(
            user_id=user_id,
            user_role=user_role,
            action=action,
            related_record_type=record_type,
            related_record_id=record_id,
            notes=notes,
        )
    )


# Filters the audit page understands. "kind" separates recorded actions from
# expenses; module values are what module_label() produces.
AUDIT_KINDS = ("all", "actions", "expenses")
AUDIT_MODULES = ("Payroll Runs", "Clients", "Employees", "Payslip", "Audit", "System")
AUDIT_PER_PAGE = 50
# module_label() precedence, as SQL: a row belongs to the first keyword its
# record type + action contains. Keeping the filter in SQL keeps the counts and
# the paging exact.
_MODULE_KEYWORDS = (
    ("Payroll Runs", "%payroll%"),
    ("Audit", "%expense%"),
    ("Clients", "%client%"),
    ("Employees", "%employee%"),
    ("Payslip", "%payslip%"),
)


def _module_clause(module):
    text = db.func.lower(
        db.func.coalesce(AuditTrail.related_record_type, "") + " " + db.func.coalesce(AuditTrail.action, "")
    )
    earlier = []
    for name, pattern in _MODULE_KEYWORDS:
        if name == module:
            return db.and_(*earlier, text.like(pattern))
        earlier.append(~text.like(pattern))
    return db.and_(*earlier, AuditTrail.related_record_type.is_(None))  # "System"


def _audit_filters(args):
    kind = args.get("kind") if args.get("kind") in AUDIT_KINDS else "all"
    module = args.get("module") if args.get("module") in AUDIT_MODULES else ""
    return {
        "q": (args.get("q") or "").strip()[:100],
        "kind": kind,
        "module": module,
        "date_from": parse_date(args.get("from")),
        "date_to": parse_date(args.get("to")),
    }


@audit_bp.route("")
@role_required(*AUDIT_ROLES)
def audit_trail():
    """Recorded actions and expenses merged newest first, paged exactly.

    The two tables are merged in Python, so page N needs at most N x per_page
    rows from each side; both are fetched to that depth, merged, and sliced.
    Totals come from COUNT queries, so the last page is reachable however long
    the trail grows (the first version capped each side at 1,000 rows)."""
    from app.paging import _ListPage, page_arg

    f = _audit_filters(request.args)
    page_num = page_arg("page")
    depth = page_num * AUDIT_PER_PAGE
    entries, expenses, total = [], [], 0
    if f["kind"] in ("all", "actions"):
        query = AuditTrail.query
        if f["q"]:
            like = f"%{f['q']}%"
            query = query.filter(db.or_(AuditTrail.action.ilike(like), AuditTrail.notes.ilike(like)))
        if f["date_from"]:
            query = query.filter(AuditTrail.created_at >= datetime.combine(f["date_from"], time.min))
        if f["date_to"]:
            query = query.filter(AuditTrail.created_at <= datetime.combine(f["date_to"], time.max))
        if f["module"]:
            query = query.filter(_module_clause(f["module"]))
        total += query.count()
        entries = query.order_by(AuditTrail.created_at.desc()).limit(depth).all()
    # Expenses all carry module "Audit", so another module filter excludes them.
    if f["kind"] in ("all", "expenses") and f["module"] in ("", "Audit"):
        query = Expense.query
        if f["q"]:
            like = f"%{f['q']}%"
            query = query.filter(db.or_(Expense.title.ilike(like), Expense.description.ilike(like),
                                        Expense.category.ilike(like)))
        if f["date_from"]:
            query = query.filter(Expense.expense_date >= f["date_from"])
        if f["date_to"]:
            query = query.filter(Expense.expense_date <= f["date_to"])
        total += query.count()
        expenses = (query.order_by(Expense.expense_date.desc(), Expense.created_at.desc())
                    .limit(depth).all())
    rows = build_audit_rows(entries, expenses)
    start = (page_num - 1) * AUDIT_PER_PAGE
    page = _ListPage(rows[start:start + AUDIT_PER_PAGE], page_num, AUDIT_PER_PAGE, total)
    clients = ClientCompany.query.order_by(ClientCompany.name).all()
    payroll_runs = PayrollRun.query.order_by(PayrollRun.created_at.desc()).limit(100).all()
    params = {k: v for k, v in {
        "q": f["q"], "kind": f["kind"] if f["kind"] != "all" else "", "module": f["module"],
        "from": request.args.get("from") if f["date_from"] else "",
        "to": request.args.get("to") if f["date_to"] else "",
    }.items() if v}
    return render_template(
        "audit.html",
        page=page,
        filters=f,
        params=params,
        kinds=AUDIT_KINDS,
        modules=AUDIT_MODULES,
        clients=clients,
        payroll_runs=payroll_runs,
        today=date.today().isoformat(),
    )


def _parse_amount(raw):
    try:
        amount = float(raw)
    except (TypeError, ValueError):
        return None
    if amount != amount or amount in (float("inf"), float("-inf")) or amount <= 0:
        return None
    return round(amount, 2)


@audit_bp.route("/expenses", methods=["POST"])
@role_required(*AUDIT_ROLES)
def add_expense():
    raw_date = (request.form.get("expense_date") or "").strip()
    expense_date = parse_date(raw_date) if raw_date else date.today()
    if expense_date is None:
        flash("Enter the date as a calendar date. Nothing was saved.", "danger")
        return redirect(url_for("audit.audit_trail", _anchor="add-expense"))
    title = (request.form.get("title") or "").strip()[:180]
    description = (request.form.get("description") or "").strip()[:255]
    client_id = request.form.get("client_company_id") or None
    payroll_run_id = request.form.get("payroll_run_id") or None
    amount = _parse_amount(request.form.get("amount"))
    # Validated here, at the boundary: float("abc") used to raise a 500, and a
    # zero or negative expense is a typo, not spend.
    if amount is None:
        flash("Enter the amount as a number greater than zero. Nothing was saved.", "danger")
        return redirect(url_for("audit.audit_trail", _anchor="add-expense"))
    if not (title or description):
        flash("Give the expense a title or description. Nothing was saved.", "danger")
        return redirect(url_for("audit.audit_trail", _anchor="add-expense"))
    client_id = int(client_id) if client_id and client_id.isdigit() else None
    payroll_run_id = int(payroll_run_id) if payroll_run_id and payroll_run_id.isdigit() else None
    run = db.session.get(PayrollRun, payroll_run_id) if payroll_run_id else None
    if (client_id and db.session.get(ClientCompany, client_id) is None) or (payroll_run_id and run is None):
        flash("That client or payroll run no longer exists. Nothing was saved.", "danger")
        return redirect(url_for("audit.audit_trail", _anchor="add-expense"))
    if run is not None and client_id and run.client_company_id != client_id:
        flash("The payroll run belongs to a different client. Nothing was saved.", "danger")
        return redirect(url_for("audit.audit_trail", _anchor="add-expense"))
    expense = Expense(
        title=title or description[:180],
        expense_date=expense_date,
        category=(request.form.get("category") or "General").strip()[:80],
        description=description or title,
        amount=amount,
        client_company_id=client_id,
        payroll_run_id=payroll_run_id,
        status="Recorded",
        recorded_by=current_user.id,
    )
    db.session.add(expense)
    db.session.flush()
    record_audit(
        "Expense recorded",
        expense,
        f"{expense.title or expense.description} - {expense.amount:.2f}",
    )
    db.session.commit()
    flash("Expense recorded.", "success")
    return redirect(url_for("audit.audit_trail"))


def parse_date(value):
    try:
        return datetime.strptime(value or "", "%Y-%m-%d").date()
    except ValueError:
        return None


def build_audit_rows(entries, expenses):
    rows = []
    for entry in entries:
        created_at = entry.created_at or datetime.combine(date.today(), time.min)
        rows.append(
            {
                "sort_at": created_at,
                "date": created_at.strftime("%Y-%m-%d"),
                "date_label": created_at.strftime("%d %b %Y"),
                "time": created_at.strftime("%H:%M"),
                "day": created_at.strftime("%A"),
                "kind": "action",
                "action": entry.action,
                "description": entry.notes or entry.action,
                "user": entry.user.name if entry.user else "System",
                "module": module_label(entry.related_record_type, entry.action),
                "related": related_label(entry.related_record_type, entry.related_record_id),
                "amount": None,
                "category": "",
            }
        )

    for expense in expenses:
        display_date = expense.expense_date or date.today()
        created_at = expense.created_at or datetime.combine(display_date, time.min)
        related_parts = []
        if expense.client_company:
            related_parts.append(expense.client_company.name)
        if expense.payroll_run:
            related_parts.append(f"{expense.payroll_run.month} {expense.payroll_run.year}")
        rows.append(
            {
                "sort_at": datetime.combine(display_date, created_at.time()),
                "date": display_date.strftime("%Y-%m-%d"),
                "date_label": display_date.strftime("%d %b %Y"),
                "time": created_at.strftime("%H:%M"),
                "day": display_date.strftime("%A"),
                "kind": "expense",
                "action": "Expense recorded",
                "description": expense.title or expense.description,
                "user": expense.recorder.name if expense.recorder else "System",
                "module": "Audit",
                "related": " / ".join(related_parts),
                "amount": expense.amount,
                "category": expense.category,
            }
        )
    return sorted(rows, key=lambda row: row["sort_at"], reverse=True)


def module_label(record_type, action):
    text = f"{record_type or ''} {action or ''}".lower()
    if "payroll" in text:
        return "Payroll Runs"
    if "expense" in text:
        return "Audit"
    if "client" in text:
        return "Clients"
    if "employee" in text:
        return "Employees"
    if "payslip" in text:
        return "Payslip"
    return record_type or "System"


def related_label(record_type, record_id):
    if not record_type or not record_id:
        return ""
    return f"{record_type} #{record_id}"
