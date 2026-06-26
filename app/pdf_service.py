"""Employee payslip PDF generation.

One generator, shared by the MVP's per-employee download button (app/payroll.py)
and the distribution feature's public link (app/distribution). Updated to the new
pay-advice layout: a clean header, an Employee Details block, an EARNINGS table and a
DEDUCTIONS table (each with a bold total row), and a prominent NET PAY band — matching
the distribution payslip design so both surfaces produce the same modern format.
"""
import os
from datetime import datetime
from html import escape

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from app.excel_utils import slug_filename

# Shared palette (matches the distribution payslip in app/channels/pdf.py).
_INK = colors.HexColor("#1f3a5f")
_HAIRLINE = colors.HexColor("#dddddd")
_RULE = colors.HexColor("#888888")
_MUTED = colors.HexColor("#666666")

# Two-column geometry: A4 content width (~174mm) split label / amount.
_LABEL_W = 118 * mm
_AMOUNT_W = 56 * mm


def money(value):
    return f"GH₵ {float(value or 0):,.2f}"


def payslip_filename(payroll_item):
    run = payroll_item.payroll_run
    staff_part = payroll_item.staff_id or payroll_item.full_name or "employee"
    return (
        f"{slug_filename(staff_part)}_Payslip_"
        f"{slug_filename(run.month)}_{run.year}.pdf"
    )


def _info_table(rows):
    """Borderless label/value block for the employee header details."""
    table = Table(rows, colWidths=[40 * mm, 134 * mm])
    table.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (0, -1), "Helvetica-Bold"),
        ("TEXTCOLOR", (0, 0), (0, -1), _MUTED),
        ("FONTSIZE", (0, 0), (-1, -1), 9.5),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 0), (-1, -1), 1.5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 1.5),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
    ]))
    return table


def _amount_section(heading, rows, total_label, total_value):
    """[Item | AMOUNT] table with a coloured header band and a bold total row."""
    data = [[heading, "AMOUNT"]]
    data += [[label, money(value)] for label, value in rows]
    data.append([total_label, money(total_value)])

    table = Table(data, colWidths=[_LABEL_W, _AMOUNT_W])
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), _INK),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 9.5),
        ("ALIGN", (0, 0), (0, -1), "LEFT"),
        ("ALIGN", (1, 0), (1, -1), "RIGHT"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
        ("LINEBELOW", (0, 1), (-1, -2), 0.25, _HAIRLINE),
        ("LINEABOVE", (0, -1), (-1, -1), 0.75, _RULE),
        ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"),
    ]))
    return table


def _net_band(value):
    table = Table([["NET PAY", money(value)]], colWidths=[_LABEL_W, _AMOUNT_W])
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), _INK),
        ("TEXTCOLOR", (0, 0), (-1, -1), colors.white),
        ("FONTNAME", (0, 0), (-1, -1), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 11),
        ("ALIGN", (1, 0), (1, -1), "RIGHT"),
        ("TOPPADDING", (0, 0), (-1, -1), 7),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
    ]))
    return table


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
        topMargin=18 * mm,
        bottomMargin=18 * mm,
        title="Chrisnat Payslip",
        author="Chrisnat Limited",
    )
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        "cn_title", parent=styles["Title"], fontSize=17, alignment=0,
        spaceAfter=2, textColor=_INK,
    )
    sub_style = ParagraphStyle("cn_sub", parent=styles["Normal"], fontSize=10, textColor=_MUTED)
    note_style = ParagraphStyle(
        "cn_note", parent=styles["Normal"], fontSize=8, textColor=_MUTED, spaceBefore=10
    )

    story = [
        Paragraph(escape("Chrisnat Limited"), title_style),
        Paragraph(escape(f"Individual Employee Payslip — {run.month} {run.year}"), sub_style),
        Spacer(1, 10),
    ]

    info_rows = [
        ["Employee Name", payroll_item.full_name or ""],
        ["Staff ID", payroll_item.staff_id or ""],
        ["Client Company", client_name],
        ["SSNIT Number", payroll_item.ssnit_number or (employee.ssnit_number if employee else "")],
        ["Bank", employee.bank_name if employee else ""],
        ["Bank Account", employee.bank_account_number if employee else ""],
        ["Payslip Generated", datetime.now().strftime("%Y-%m-%d %H:%M")],
    ]
    story.append(_info_table(info_rows))
    story.append(Spacer(1, 12))

    earnings = [
        ("Basic Salary", payroll_item.basic_salary),
        ("Transport Allowance", payroll_item.transport_allowance),
        ("Housing Allowance", payroll_item.housing_allowance),
        ("Overtime Pay", payroll_item.overtime_pay),
        ("Other Allowances", payroll_item.other_allowances),
    ]
    deductions = [
        ("PAYE", payroll_item.paye),
        ("SSNIT", payroll_item.ssnit),
        ("Other Deductions", payroll_item.other_deductions),
    ]

    story.append(_amount_section("EARNINGS", earnings, "Total Earnings", payroll_item.gross_pay))
    story.append(Spacer(1, 8))
    story.append(
        _amount_section("DEDUCTIONS", deductions, "Total Deductions", payroll_item.total_deductions)
    )
    story.append(Spacer(1, 4))
    story.append(_net_band(payroll_item.net_pay))

    story.append(
        Paragraph(
            "This payslip is generated from the approved payroll records in the Chrisnat "
            "system. PAYE and SSNIT values are based on the uploaded payroll data.",
            note_style,
        )
    )

    document.build(story)
    return file_path
