from datetime import date, datetime, time

from flask import Blueprint, flash, has_request_context, redirect, render_template, request, url_for
from flask_login import current_user

from app import db
from app.auth import role_required
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


@audit_bp.route("")
@role_required("admin", "md")
def audit_trail():
    entries = AuditTrail.query.order_by(AuditTrail.created_at.desc()).limit(250).all()
    expenses = Expense.query.order_by(Expense.created_at.desc()).limit(250).all()
    clients = ClientCompany.query.order_by(ClientCompany.name).all()
    payroll_runs = PayrollRun.query.order_by(PayrollRun.created_at.desc()).limit(100).all()
    rows = build_audit_rows(entries, expenses)
    return render_template(
        "audit.html",
        rows=rows,
        clients=clients,
        payroll_runs=payroll_runs,
        today=date.today().isoformat(),
    )


@audit_bp.route("/expenses", methods=["POST"])
@role_required("admin", "md")
def add_expense():
    expense_date = parse_date(request.form.get("expense_date")) or date.today()
    title = (request.form.get("title") or "").strip()
    description = (request.form.get("description") or "").strip()
    client_id = request.form.get("client_company_id") or None
    payroll_run_id = request.form.get("payroll_run_id") or None
    expense = Expense(
        title=title or description[:180],
        expense_date=expense_date,
        category=request.form.get("category") or "General",
        description=description or title or "Expense",
        amount=float(request.form.get("amount") or 0),
        client_company_id=int(client_id) if client_id else None,
        payroll_run_id=int(payroll_run_id) if payroll_run_id else None,
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
                "time": created_at.strftime("%H:%M"),
                "day": created_at.strftime("%A"),
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
                "time": created_at.strftime("%H:%M"),
                "day": display_date.strftime("%A"),
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
