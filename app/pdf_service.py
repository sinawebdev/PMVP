"""Employee payslip PDF generation.

One generator, shared by the MVP's per-employee download button (app/payroll.py)
and the distribution feature's public link (app/distribution). The layout mirrors
the client's original Excel "VBA PAYSLIP" sheet: a two-column employee header
block, EARNINGS (left) against DEDUCTIONS (right) in one side-by-side table, and
a NET PAY totals band beneath — in Payrolla brand colours (Deep Teal + Bright
Teal). The employer (client company) heads the document; Payrolla is credited
as the generating platform in the footer.
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

# Payrolla brand palette.
_TEAL = colors.HexColor("#0D4D4D")     # Deep Teal — headers / bands
_ACCENT = colors.HexColor("#17C3B2")   # Bright Teal — accent lines / totals
_HAIRLINE = colors.HexColor("#dddddd")
_MUTED = colors.HexColor("#666666")

# Side-by-side geometry: A4 content width (~174mm) split into two
# label/amount column pairs (earnings | deductions).
_ITEM_W = 56 * mm
_AMT_W = 31 * mm


def money(value):
    # Plain ASCII cent sign (U+00A2), matching the source workbooks' "GH¢"
    # convention. The Unicode Ghana Cedi Sign (U+20B5) has no glyph in
    # ReportLab's base Helvetica/WinAnsiEncoding and renders as a tofu box.
    return f"GH¢ {float(value or 0):,.2f}"


def payslip_filename(payroll_item):
    run = payroll_item.payroll_run
    staff_part = payroll_item.staff_id or payroll_item.full_name or "employee"
    return (
        f"{slug_filename(staff_part)}_Payslip_"
        f"{slug_filename(run.month)}_{run.year}.pdf"
    )


def _header_block(rows):
    """Two label/value column pairs, VBA PAYSLIP style (label, value, label, value)."""
    table = Table(rows, colWidths=[30 * mm, 57 * mm, 30 * mm, 57 * mm])
    table.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (0, -1), "Helvetica-Bold"),
        ("FONTNAME", (2, 0), (2, -1), "Helvetica-Bold"),
        ("TEXTCOLOR", (0, 0), (0, -1), _MUTED),
        ("TEXTCOLOR", (2, 0), (2, -1), _MUTED),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 0), (-1, -1), 2),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
        ("LEFTPADDING", (0, 0), (0, -1), 0),
        ("LINEBELOW", (0, -1), (-1, -1), 1, _ACCENT),
    ]))
    return table


def _earnings_rows(item):
    """VBA PAYSLIP earnings order: Basic Salary, Medical Allowance,
    Productivity Bonus, Overtime — each from its dedicated PayrollItem column.
    Transport/housing/other rows are appended when present so the total always
    reconciles with gross pay."""
    rows = [
        ("Basic Salary", item.basic_salary),
        ("Medical Allowance", item.medical_allowance),
        ("Productivity Bonus", item.productivity_bonus),
        ("Overtime", item.overtime_pay),
    ]
    if item.transport_allowance:
        rows.append(("Transport Allowance", item.transport_allowance))
    if item.housing_allowance:
        rows.append(("Housing Allowance", item.housing_allowance))
    if item.meal_allowance:
        rows.append(("Meal Allowance", item.meal_allowance))
    if item.other_allowances:
        rows.append(("Other Allowances", item.other_allowances))
    return rows


def _deduction_rows(item):
    # IOU now has its own PayrollItem column — other_deductions used to
    # double as the I.O.U line back when the ACS IOU column folded into it.
    rows = [
        ("Income Tax", item.paye),
        ("Social Security 5.5%", item.ssnit),
        ("Other Deductions", item.other_deductions),
        ("Loan/Salary Advance", item.loan_deduction),
    ]
    if item.welfare_deduction:
        rows.append(("Welfare", item.welfare_deduction))
    if item.iou_deduction:
        rows.append(("I.O.U", item.iou_deduction))
    if item.pf_fund_employee:
        rows.append(("PF Fund (Employee)", item.pf_fund_employee))
    if item.tier_2_pension:
        rows.append(("Tier 2 Pension", item.tier_2_pension))
    return rows


def _earnings_deductions_table(item):
    """Single side-by-side table: EARNINGS columns on the left, DEDUCTIONS on
    the right, with a teal-ruled totals row, matching the VBA PAYSLIP body."""
    earnings = _earnings_rows(item)
    deductions = _deduction_rows(item)
    depth = max(len(earnings), len(deductions))
    earnings += [("", None)] * (depth - len(earnings))
    deductions += [("", None)] * (depth - len(deductions))

    data = [["EARNINGS", "AMOUNT", "DEDUCTIONS", "AMOUNT"]]
    for (e_label, e_val), (d_label, d_val) in zip(earnings, deductions):
        data.append([
            e_label,
            money(e_val) if e_label else "",
            d_label,
            money(d_val) if d_label else "",
        ])
    data.append([
        "Total Earnings",
        money(item.gross_pay),
        "Total Deductions",
        money(item.total_deductions),
    ])

    table = Table(data, colWidths=[_ITEM_W, _AMT_W, _ITEM_W, _AMT_W])
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), _TEAL),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("ALIGN", (1, 0), (1, -1), "RIGHT"),
        ("ALIGN", (3, 0), (3, -1), "RIGHT"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 4.5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4.5),
        ("LEFTPADDING", (0, 0), (-1, -1), 7),
        ("RIGHTPADDING", (0, 0), (-1, -1), 7),
        ("LINEBELOW", (0, 1), (-1, -2), 0.25, _HAIRLINE),
        ("LINEAFTER", (1, 0), (1, -1), 0.5, _HAIRLINE),
        ("LINEABOVE", (0, -1), (-1, -1), 1, _ACCENT),
        ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"),
        ("TEXTCOLOR", (0, -1), (-1, -1), _TEAL),
    ]))
    return table


def _net_band(value):
    table = Table(
        [["NET PAY", money(value)]],
        colWidths=[_ITEM_W + _AMT_W + _ITEM_W, _AMT_W],
    )
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), _TEAL),
        ("TEXTCOLOR", (0, 0), (-1, -1), colors.white),
        ("FONTNAME", (0, 0), (-1, -1), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 11.5),
        ("ALIGN", (1, 0), (1, -1), "RIGHT"),
        ("TOPPADDING", (0, 0), (-1, -1), 8),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
        ("LEFTPADDING", (0, 0), (-1, -1), 7),
        ("RIGHTPADDING", (0, 0), (-1, -1), 7),
        ("LINEABOVE", (0, 0), (-1, 0), 1.5, _ACCENT),
    ]))
    return table


def generate_payslip_pdf(payroll_item, export_folder):
    run = payroll_item.payroll_run
    employee = payroll_item.employee
    client = run.client_company
    client_name = client.name if client else "Unassigned Client"
    # Product identity from config so the payslip credits the platform (Payrolla)
    # without hardcoding it. Safe if the PDF is ever built outside an app context.
    try:
        from flask import current_app

        brand = current_app.config.get("APP_BRAND_NAME", "Payrolla")
    except Exception:  # noqa: BLE001 - generated outside an app context
        brand = "Payrolla"
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
        title=f"Payslip — {client_name}",
        author=brand,
    )
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        "cn_title", parent=styles["Title"], fontSize=17, alignment=0,
        spaceAfter=2, textColor=_TEAL,
    )
    sub_style = ParagraphStyle("cn_sub", parent=styles["Normal"], fontSize=10, textColor=_MUTED)
    note_style = ParagraphStyle(
        "cn_note", parent=styles["Normal"], fontSize=8, textColor=_MUTED, spaceBefore=10
    )

    story = [
        Paragraph(escape(client_name), title_style),
        Paragraph(escape("Employee Payslip"), sub_style),
        Spacer(1, 10),
    ]

    period = f"{run.month} {run.year}"
    location = (client.location if client else "") or (employee.assigned_client if employee else "") or ""
    header_rows = [
        ["Employee Reg #", payroll_item.staff_id or "", "Period", period],
        ["Employee Name", payroll_item.full_name or "", "Department",
         (employee.department if employee else "") or ""],
        ["Job Title", payroll_item.job_role or "", "Location", location],
        ["SSNIT No", payroll_item.ssnit_number or (employee.ssnit_number if employee else "") or "",
         "GH Card", payroll_item.ghana_card_number or (employee.ghana_card_number if employee else "") or ""],
        ["Account No", payroll_item.bank_account_number or (employee.bank_account_number if employee else "") or "",
         "Bank", payroll_item.bank_name or (employee.bank_name if employee else "") or ""],
    ]
    story.append(_header_block(header_rows))
    story.append(Spacer(1, 12))

    story.append(_earnings_deductions_table(payroll_item))
    story.append(Spacer(1, 4))
    story.append(_net_band(payroll_item.net_pay))

    story.append(
        Paragraph(
            f"Generated by {escape(brand)} from the approved payroll records on "
            f"{datetime.now().strftime('%Y-%m-%d %H:%M')}. PAYE and SSNIT values "
            "are based on the approved payroll data.",
            note_style,
        )
    )

    document.build(story)
    return file_path
