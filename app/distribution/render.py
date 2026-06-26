"""Render a Chrisnat PayrollItem into a payslip breakdown message.

This is the worker-facing salary *breakdown* (not a payment). We reuse Chrisnat's
`format_ghana_cedis` so amounts read identically to the rest of the app.
"""
from html import escape

from app import format_ghana_cedis as cedis


def _line_items(item):
    """(label, amount) pairs for the non-zero parts of the breakdown."""
    earnings = [
        ("Basic", item.basic_salary),
        ("Transport", item.transport_allowance),
        ("Housing", item.housing_allowance),
        ("Overtime", item.overtime_pay),
        ("Other allowances", item.other_allowances),
    ]
    deductions = [
        ("PAYE", item.paye),
        ("SSNIT", item.ssnit),
        ("Tier 2", item.tier_2_pension),
        ("Loan", item.loan_deduction),
        ("Other deductions", item.other_deductions),
    ]
    keep = lambda pairs: [(label, amount) for label, amount in pairs if amount]
    return keep(earnings), keep(deductions)


def _period(run):
    return f"{run.month} {run.year}".strip() if run else "this period"


def render_payslip_text(item, run, client, link=None) -> str:
    """Compact plain-text payslip for SMS / WhatsApp.

    When ``link`` is given, a tokenized no-login URL is appended so the worker can
    open the full payslip and download a PDF on their phone without signing in.
    """
    org = client.name if client else "Chrisnat"
    earnings, deductions = _line_items(item)
    lines = [f"{org} payslip — {_period(run)}", f"{item.full_name}"]
    for label, amount in earnings:
        lines.append(f"{label}: {cedis(amount)}")
    lines.append(f"Gross: {cedis(item.gross_pay)}")
    for label, amount in deductions:
        lines.append(f"-{label}: {cedis(amount)}")
    lines.append(f"Total deductions: {cedis(item.total_deductions)}")
    lines.append(f"NET PAY: {cedis(item.net_pay)}")
    lines.append("(Salary breakdown, not a payment.)")
    if link:
        lines.append(f"View payslip + PDF: {link}")
    return "\n".join(lines)


def render_payslip_email(item, run, client, link=None):
    """Return (subject, body_text, body_html) for an email payslip."""
    org = client.name if client else "Chrisnat"
    period = _period(run)
    subject = f"Your {period} payslip — {org}"
    body_text = render_payslip_text(item, run, client, link=link)

    earnings, deductions = _line_items(item)

    def rows(pairs, negative=False):
        out = []
        for label, amount in pairs:
            shown = f"-{cedis(amount)}" if negative else cedis(amount)
            out.append(
                f'<tr><td style="padding:4px 12px 4px 0;">{escape(label)}</td>'
                f'<td align="right" style="padding:4px 0;">{escape(shown)}</td></tr>'
            )
        return "".join(out)

    body_html = (
        f'<div style="font-family:Arial,sans-serif;color:#1a1a1a;">'
        f"<p>Hello {escape(item.full_name or 'Employee')},</p>"
        f"<p>Here is your {escape(period)} payslip breakdown from {escape(org)}.</p>"
        f'<table cellpadding="0" cellspacing="0" style="border-collapse:collapse;margin:12px 0;">'
        f"{rows(earnings)}"
        f'<tr><td style="padding:6px 12px 6px 0;border-top:1px solid #ddd;"><strong>Gross</strong></td>'
        f'<td align="right" style="padding:6px 0;border-top:1px solid #ddd;"><strong>{escape(cedis(item.gross_pay))}</strong></td></tr>'
        f"{rows(deductions, negative=True)}"
        f'<tr><td style="padding:6px 12px 6px 0;border-top:2px solid #333;"><strong>Net pay</strong></td>'
        f'<td align="right" style="padding:6px 0;border-top:2px solid #333;"><strong>{escape(cedis(item.net_pay))}</strong></td></tr>'
        f"</table>"
        + (
            f'<p><a href="{escape(link)}" '
            f'style="display:inline-block;background:#0F766E;color:#fff;text-decoration:none;'
            f'padding:10px 18px;border-radius:6px;">View payslip &amp; download PDF</a></p>'
            if link
            else ""
        )
        + f'<p style="color:#777;font-size:12px;">This is your salary breakdown, not a payment.</p>'
        f"</div>"
    )
    return subject, body_text, body_html
