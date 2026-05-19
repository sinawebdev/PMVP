import os
from datetime import datetime

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from app.excel_utils import slug_filename


def money(value):
    return f"GH₵ {float(value or 0):,.2f}"


def payslip_filename(payroll_item):
    run = payroll_item.payroll_run
    staff_part = payroll_item.staff_id or payroll_item.full_name or "employee"
    return (
        f"{slug_filename(staff_part)}_Payslip_"
        f"{slug_filename(run.month)}_{run.year}.pdf"
    )


def generate_payslip_pdf(payroll_item, export_folder):
    run = payroll_item.payroll_run
    employee = payroll_item.employee
    client_name = run.client_company.name if run.client_company else "Unassigned Client"
    payslip_folder = os.path.join(export_folder, "payslips")
    os.makedirs(payslip_folder, exist_ok=True)
    file_path = os.path.join(payslip_folder, payslip_filename(payroll_item))

    document = SimpleDocTemplate(
        file_path,
        pagesize=A4,
        rightMargin=18 * mm,
        leftMargin=18 * mm,
        topMargin=16 * mm,
        bottomMargin=16 * mm,
        title="Chrisnat Payslip",
    )
    styles = getSampleStyleSheet()
    story = []

    header = Table(
        [
            [
                Paragraph("<b>CN</b>", styles["Title"]),
                Paragraph(
                    "<b>Chrisnat Limited</b><br/>Individual Employee Payslip<br/>"
                    f"{run.month} {run.year}",
                    styles["Normal"],
                ),
            ]
        ],
        colWidths=[32 * mm, 128 * mm],
    )
    header.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (0, 0), colors.HexColor("#FACC15")),
                ("TEXTCOLOR", (0, 0), (0, 0), colors.HexColor("#132034")),
                ("BACKGROUND", (1, 0), (1, 0), colors.HexColor("#ECFDF5")),
                ("BOX", (0, 0), (-1, -1), 0.8, colors.HexColor("#0F766E")),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("LEFTPADDING", (0, 0), (-1, -1), 12),
                ("RIGHTPADDING", (0, 0), (-1, -1), 12),
                ("TOPPADDING", (0, 0), (-1, -1), 12),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 12),
            ]
        )
    )
    story.append(header)
    story.append(Spacer(1, 10 * mm))

    employee_data = [
        ["Employee Name", payroll_item.full_name or ""],
        ["Staff ID", payroll_item.staff_id or ""],
        ["Client Company", client_name],
        ["SSNIT Number", payroll_item.ssnit_number or (employee.ssnit_number if employee else "")],
        ["Bank", employee.bank_name if employee else ""],
        ["Bank Account", employee.bank_account_number if employee else ""],
        ["Payslip Generated", datetime.now().strftime("%Y-%m-%d %H:%M")],
    ]
    story.append(section_table("Employee Details", employee_data))
    story.append(Spacer(1, 7 * mm))

    earnings = [
        ["Basic Salary", money(payroll_item.basic_salary)],
        ["Transport Allowance", money(payroll_item.transport_allowance)],
        ["Housing Allowance", money(payroll_item.housing_allowance)],
        ["Overtime Pay", money(payroll_item.overtime_pay)],
        ["Other Allowances", money(payroll_item.other_allowances)],
        ["Gross Pay", money(payroll_item.gross_pay)],
    ]
    deductions = [
        ["PAYE", money(payroll_item.paye)],
        ["SSNIT", money(payroll_item.ssnit)],
        ["Other Deductions", money(payroll_item.other_deductions)],
        ["Total Deductions", money(payroll_item.total_deductions)],
        ["Net Pay", money(payroll_item.net_pay)],
    ]

    story.append(section_table("Earnings", earnings))
    story.append(Spacer(1, 7 * mm))
    story.append(section_table("Deductions and Net Pay", deductions, highlight_last=True))
    story.append(Spacer(1, 8 * mm))
    story.append(
        Paragraph(
            "This payslip is generated from the approved payroll records in the Chrisnat Payroll MVP. "
            "PAYE and SSNIT values are based on uploaded payroll data for Phase 1.",
            styles["Italic"],
        )
    )

    document.build(story)
    return file_path


def section_table(title, rows, highlight_last=False):
    data = [[title, ""]] + rows
    table = Table(data, colWidths=[72 * mm, 88 * mm])
    style = [
        ("SPAN", (0, 0), (1, 0)),
        ("BACKGROUND", (0, 0), (1, 0), colors.HexColor("#0F766E")),
        ("TEXTCOLOR", (0, 0), (1, 0), colors.white),
        ("FONTNAME", (0, 0), (1, 0), "Helvetica-Bold"),
        ("BACKGROUND", (0, 1), (-1, -1), colors.white),
        ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#DCE8E4")),
        ("FONTNAME", (0, 1), (0, -1), "Helvetica-Bold"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 9),
        ("RIGHTPADDING", (0, 0), (-1, -1), 9),
        ("TOPPADDING", (0, 0), (-1, -1), 8),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
    ]
    if highlight_last:
        style.extend(
            [
                ("BACKGROUND", (0, -1), (1, -1), colors.HexColor("#FEF3C7")),
                ("FONTNAME", (0, -1), (1, -1), "Helvetica-Bold"),
            ]
        )
    table.setStyle(TableStyle(style))
    return table
