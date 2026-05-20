from calendar import month_name
from datetime import datetime
import os

from flask import Blueprint, current_app, render_template, request, send_file
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.platypus import SimpleDocTemplate, Spacer, Table, TableStyle

from app.auth import role_required
from app.excel_utils import export_monthly_payroll_summary
from app.models import ClientCompany, Expense, PaymentVoucher, PayrollItem, PayrollRun, Remittance

reports_bp = Blueprint("reports", __name__, url_prefix="/reports")


def report_filters():
    now = datetime.now()
    month = request.args.get("month") or now.strftime("%B")
    year = int(request.args.get("year") or now.year)
    client_id = request.args.get("client_id") or ""
    return month, year, client_id


@reports_bp.route("")
@role_required("admin", "md", "accounts_officer", "viewer")
def reports_home():
    month, year, client_id = report_filters()
    clients = ClientCompany.query.order_by(ClientCompany.name).all()
    query = PayrollRun.query.filter_by(month=month, year=year)
    if client_id:
        query = query.filter_by(client_company_id=int(client_id))
    runs = query.order_by(PayrollRun.created_at.desc()).all()
    return render_template(
        "reports.html",
        clients=clients,
        month_options=[month_name[index] for index in range(1, 13)],
        selected_month=month,
        selected_year=year,
        selected_client_id=client_id,
        payroll_runs=runs,
        total_workers=sum(run.total_workers for run in runs),
        total_gross=sum(run.total_gross_pay for run in runs),
        total_deductions=sum(run.total_deductions for run in runs),
        total_net=sum(run.total_net_pay for run in runs),
        total_paye=sum(run.total_paye for run in runs),
        total_ssnit=sum(run.total_ssnit for run in runs),
        expense_total=sum(expense.amount for expense in Expense.query.all()),
        voucher_total=sum(voucher.total_amount for voucher in PaymentVoucher.query.all()),
        remittance_total=sum(remittance.amount_due for remittance in Remittance.query.all()),
        warning_count=PayrollItem.query.filter_by(validation_status="Warning").count(),
    )


@reports_bp.route("/monthly-payroll.xlsx")
@role_required("admin", "md", "accounts_officer", "viewer")
def monthly_payroll_excel():
    month, year, client_id = report_filters()
    query = PayrollRun.query.filter_by(month=month, year=year)
    if client_id:
        query = query.filter_by(client_company_id=int(client_id))
    file_path = export_monthly_payroll_summary(
        query.order_by(PayrollRun.created_at.desc()).all(),
        current_app.config["EXPORT_FOLDER"],
        month,
        year,
    )
    return send_file(file_path, as_attachment=True)


@reports_bp.route("/monthly-payroll.pdf")
@role_required("admin", "md", "accounts_officer", "viewer")
def monthly_payroll_pdf():
    month, year, client_id = report_filters()
    query = PayrollRun.query.filter_by(month=month, year=year)
    if client_id:
        query = query.filter_by(client_company_id=int(client_id))
    runs = query.order_by(PayrollRun.created_at.desc()).all()
    file_path = os.path.join(
        current_app.config["EXPORT_FOLDER"],
        f"Chrisnat_Monthly_Payroll_{month}_{year}.pdf",
    )
    rows = [["Client", "Month", "Workers", "Gross", "Deductions", "PAYE", "SSNIT", "Net", "Status"]]
    rows.extend(
        [
            [
                run.client_company.name if run.client_company else "",
                f"{run.month} {run.year}",
                run.total_workers,
                f"{run.total_gross_pay:,.2f}",
                f"{run.total_deductions:,.2f}",
                f"{run.total_paye:,.2f}",
                f"{run.total_ssnit:,.2f}",
                f"{run.total_net_pay:,.2f}",
                run.status,
            ]
            for run in runs
        ]
    )
    table = Table(rows, repeatRows=1)
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#0f766e")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#dce8e4")),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("FONTSIZE", (0, 0), (-1, -1), 8),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f2f7f5")]),
            ]
        )
    )
    document = SimpleDocTemplate(file_path, pagesize=landscape(A4))
    document.build([table, Spacer(1, 12)])
    return send_file(file_path, as_attachment=True)
