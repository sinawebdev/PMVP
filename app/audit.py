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
# The page merges two tables in Python, so each side is fetched to this depth
# before paging. Filters narrow it in SQL first.
AUDIT_FETCH_LIMIT = 1000


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
    from app.paging import paginate_list

    f = _audit_filters(request.args)
    entries, expenses = [], []
    if f["kind"] in ("all", "actions"):
        query = AuditTrail.query
        if f["q"]:
            like = f"%{f['q']}%"
            query = query.filter(db.or_(AuditTrail.action.ilike(like), AuditTrail.notes.ilike(like)))
        if f["date_from"]:
            query = query.filter(AuditTrail.created_at >= datetime.combine(f["date_from"], time.min))
        if f["date_to"]:
            query = query.filter(AuditTrail.created_at <= datetime.combine(f["date_to"], time.max))
        entries = query.order_by(AuditTrail.created_at.desc()).limit(AUDIT_FETCH_LIMIT).all()
    if f["kind"] in ("all", "expenses"):
        query = Expense.query
        if f["q"]:
            like = f"%{f['q']}%"
            query = query.filter(db.or_(Expense.title.ilike(like), Expense.description.ilike(like),
                                        Expense.category.ilike(like)))
        if f["date_from"]:
            query = query.filter(Expense.expense_date >= f["date_from"])
        if f["date_to"]:
            query = query.filter(Expense.expense_date <= f["date_to"])
        expenses = query.order_by(Expense.created_at.desc()).limit(AUDIT_FETCH_LIMIT).all()
    rows = build_audit_rows(entries, expenses)
    if f["module"]:
        rows = [row for row in rows if row["module"] == f["module"]]
    page = paginate_list(rows, per_page=50)
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
    expense_date = parse_date(request.form.get("expense_date")) or date.today()
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
    expense = Expense(
        title=title or description[:180],
        expense_date=expense_date,
        category=(request.form.get("category") or "General").strip()[:80],
        description=description or title,
        amount=amount,
        client_company_id=int(client_id) if client_id and client_id.isdigit() else None,
        payroll_run_id=int(payroll_run_id) if payroll_run_id and payroll_run_id.isdigit() else None,
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
