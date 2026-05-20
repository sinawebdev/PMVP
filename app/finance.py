from datetime import date, datetime, timedelta, timezone

from flask import Blueprint, current_app, flash, redirect, render_template, request, send_file, url_for
from flask_login import current_user, login_required

from app import db
from app.audit import record_audit
from app.auth import role_required
from app.excel_utils import export_expenses, export_payment_vouchers, export_remittances
from app.models import ClientCompany, Expense, PaymentVoucher, PayrollRun, Remittance

finance_bp = Blueprint("finance", __name__, url_prefix="/accounts")

EXPENSE_CATEGORIES = [
    "Office expenses",
    "Transport",
    "Cleaning supplies",
    "Recruitment cost",
    "Goods supply cost",
    "Administrative cost",
    "Miscellaneous",
]


def create_finance_records_for_payroll(payroll_run, approved_by_user_id):
    if not payroll_run.voucher:
        voucher = PaymentVoucher(
            payroll_run_id=payroll_run.id,
            voucher_number=f"PV-{payroll_run.year}-{payroll_run.id:04d}",
            total_amount=payroll_run.total_net_pay,
            gross_payroll=payroll_run.total_gross_pay,
            total_deductions=payroll_run.total_deductions,
            net_amount_payable=payroll_run.total_net_pay,
            prepared_by=payroll_run.created_by,
            reviewed_by=payroll_run.reviewed_by,
            approved_by=approved_by_user_id,
            status="Pending Payment",
            date_approved=datetime.now(timezone.utc),
        )
        db.session.add(voucher)
        record_audit("Voucher generation", payroll_run, "Payment voucher auto-created after MD approval.")

    existing_types = {item.remittance_type for item in payroll_run.remittances}
    due_date = date.today() + timedelta(days=14)
    if "PAYE" not in existing_types:
        db.session.add(
            Remittance(
                payroll_run_id=payroll_run.id,
                remittance_type="PAYE",
                amount_due=payroll_run.total_paye,
                due_date=due_date,
                status="Pending",
                notes="Auto-created when payroll was approved.",
            )
        )
    if "SSNIT" not in existing_types:
        db.session.add(
            Remittance(
                payroll_run_id=payroll_run.id,
                remittance_type="SSNIT",
                amount_due=payroll_run.total_ssnit,
                due_date=due_date,
                status="Pending",
                notes="Auto-created when payroll was approved.",
            )
        )


@finance_bp.route("/")
@role_required("admin", "md", "accounts_officer")
def accounts_dashboard():
    approved_runs = PayrollRun.query.filter_by(status="Approved").order_by(PayrollRun.created_at.desc()).all()
    vouchers = PaymentVoucher.query.order_by(PaymentVoucher.created_at.desc()).all()
    remittances = Remittance.query.order_by(Remittance.due_date.asc()).all()
    expenses = Expense.query.order_by(Expense.expense_date.desc()).limit(5).all()
    return render_template(
        "accounts_dashboard.html",
        approved_runs=approved_runs,
        vouchers=vouchers,
        remittances=remittances,
        expenses=expenses,
        total_expenses=sum(expense.amount for expense in Expense.query.all()),
        total_vouchers=sum(voucher.total_amount for voucher in vouchers),
    )


@finance_bp.route("/vouchers")
@role_required("admin", "md", "accounts_officer")
def vouchers():
    vouchers = PaymentVoucher.query.order_by(PaymentVoucher.created_at.desc()).all()
    return render_template("vouchers.html", vouchers=vouchers)


@finance_bp.route("/vouchers/export")
@role_required("admin", "md", "accounts_officer")
def export_vouchers():
    file_path = export_payment_vouchers(
        PaymentVoucher.query.all(), current_app.config["EXPORT_FOLDER"]
    )
    return send_file(file_path, as_attachment=True)


@finance_bp.route("/vouchers/<int:voucher_id>/mark-paid", methods=["POST"])
@role_required("admin", "accounts_officer")
def mark_voucher_paid(voucher_id):
    voucher = db.get_or_404(PaymentVoucher, voucher_id)
    voucher.status = "Paid"
    voucher.date_paid = datetime.now(timezone.utc)
    if voucher.payroll_run:
        voucher.payroll_run.status = "Paid"
    record_audit("Payment marked as paid", voucher, "Payment voucher marked as paid.")
    db.session.commit()
    flash("Payment voucher marked as paid.", "success")
    return redirect(url_for("finance.vouchers"))


@finance_bp.route("/remittances")
@role_required("admin", "md", "accounts_officer")
def remittances():
    remittances = Remittance.query.order_by(Remittance.due_date.asc()).all()
    return render_template("remittances.html", remittances=remittances)


@finance_bp.route("/remittances/export")
@role_required("admin", "md", "accounts_officer")
def export_remittance_summary():
    file_path = export_remittances(
        Remittance.query.all(), current_app.config["EXPORT_FOLDER"]
    )
    return send_file(file_path, as_attachment=True)


@finance_bp.route("/remittances/<int:remittance_id>/mark-paid", methods=["POST"])
@role_required("admin", "accounts_officer")
def mark_remittance_paid(remittance_id):
    remittance = db.get_or_404(Remittance, remittance_id)
    remittance.status = "Paid"
    remittance.date_paid = date.today()
    remittance.payment_reference = request.form.get("payment_reference") or remittance.payment_reference
    record_audit("Remittance marked as paid", remittance, remittance.payment_reference or "")
    db.session.commit()
    flash("Remittance marked as paid.", "success")
    return redirect(url_for("finance.remittances"))


@finance_bp.route("/expenses", methods=["GET", "POST"])
@role_required("admin", "md", "accounts_officer")
def expenses():
    if request.method == "POST":
        if current_user.role not in ("admin", "md", "accounts_officer"):
            flash("Only Admin, MD, and Accounts Officer users can record expenses.", "warning")
            return redirect(url_for("finance.expenses"))
        expense = Expense(
            expense_date=date.fromisoformat(request.form["expense_date"]),
            category=request.form["category"],
            description=request.form["description"],
            amount=float(request.form.get("amount") or 0),
            payment_method=request.form.get("payment_method"),
            receipt_reference=request.form.get("receipt_reference"),
            receipt_attachment=request.form.get("receipt_attachment"),
            paid_by=current_user.id,
            approved_by=current_user.id if current_user.role in ("admin", "md") else None,
            client_company_id=request.form.get("client_company_id") or None,
            status=request.form.get("status", "Pending"),
            recorded_by=current_user.id,
        )
        db.session.add(expense)
        db.session.flush()
        record_audit("Expense creation", expense, expense.description)
        db.session.commit()
        flash("Expense recorded.", "success")
        return redirect(url_for("finance.expenses"))

    category = request.args.get("category", "")
    query = Expense.query
    if category:
        query = query.filter_by(category=category)
    expense_rows = query.order_by(Expense.expense_date.desc()).all()
    return render_template(
        "expenses.html",
        expenses=expense_rows,
        categories=EXPENSE_CATEGORIES,
        clients=ClientCompany.query.order_by(ClientCompany.name).all(),
        selected_category=category,
        total_expenses=sum(expense.amount for expense in expense_rows),
    )


@finance_bp.route("/expenses/<int:expense_id>/approve", methods=["POST"])
@role_required("admin", "md")
def approve_expense(expense_id):
    expense = db.get_or_404(Expense, expense_id)
    expense.status = "Approved"
    expense.approved_by = current_user.id
    record_audit("Expense approval", expense, expense.description)
    db.session.commit()
    flash("Expense approved.", "success")
    return redirect(url_for("finance.expenses"))


@finance_bp.route("/expenses/export")
@role_required("admin", "md", "accounts_officer")
def export_expense_list():
    file_path = export_expenses(Expense.query.all(), current_app.config["EXPORT_FOLDER"])
    return send_file(file_path, as_attachment=True)
