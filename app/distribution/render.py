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


def _brand():
    """(brand name, accent colour) from config, so email branding follows the
    product identity seam without hardcoding. Safe outside an app context."""
    try:
        from flask import current_app

        return (
            current_app.config.get("APP_BRAND_NAME", "Chrisnat"),
            current_app.config.get("EMAIL_BRAND_COLOR", "#0F766E"),
        )
    except Exception:  # noqa: BLE001 - no app context (e.g. pure-unit render tests)
        return "Chrisnat", "#0F766E"


def render_payslip_email(item, run, client, link=None):
    """Return (subject, body_text, body_html) for an email payslip.

    The HTML is a self-contained, table-based, inline-styled document (the layout
    email clients render reliably), branded with the product/company identity and
    with a plain-text ``body_text`` fallback for text-only clients."""
    org = client.name if client else "Chrisnat"
    brand, accent = _brand()
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

    button = (
        f'<p style="margin:20px 0;"><a href="{escape(link)}" '
        f'style="display:inline-block;background:{accent};color:#fff;text-decoration:none;'
        f'padding:10px 18px;border-radius:6px;font-weight:bold;">View payslip &amp; download PDF</a></p>'
        if link
        else ""
    )

    body_html = (
        '<!DOCTYPE html><html><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1"></head>'
        '<body style="margin:0;padding:0;background:#f4f5f7;">'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        'style="background:#f4f5f7;padding:24px 0;"><tr><td align="center">'
        '<table role="presentation" width="560" cellpadding="0" cellspacing="0" '
        'style="max-width:560px;width:100%;background:#ffffff;border-radius:8px;overflow:hidden;'
        'font-family:Arial,Helvetica,sans-serif;color:#1a1a1a;">'
        # Branded header band
        f'<tr><td style="background:{accent};padding:16px 24px;color:#ffffff;font-size:18px;'
        f'font-weight:bold;">{escape(brand)}</td></tr>'
        '<tr><td style="padding:24px;">'
        f"<p style=\"margin:0 0 8px;\">Hello {escape(item.full_name or 'Employee')},</p>"
        f'<p style="margin:0 0 4px;">Here is your <strong>{escape(period)}</strong> payslip '
        f"breakdown from {escape(org)}.</p>"
        '<table cellpadding="0" cellspacing="0" style="border-collapse:collapse;margin:16px 0;width:100%;">'
        f"{rows(earnings)}"
        '<tr><td style="padding:6px 12px 6px 0;border-top:1px solid #ddd;"><strong>Gross</strong></td>'
        f'<td align="right" style="padding:6px 0;border-top:1px solid #ddd;"><strong>{escape(cedis(item.gross_pay))}</strong></td></tr>'
        f"{rows(deductions, negative=True)}"
        '<tr><td style="padding:6px 12px 6px 0;border-top:2px solid #333;"><strong>Net pay</strong></td>'
        f'<td align="right" style="padding:6px 0;border-top:2px solid #333;"><strong>{escape(cedis(item.net_pay))}</strong></td></tr>'
        "</table>"
        f"{button}"
        '<p style="color:#777;font-size:12px;margin:16px 0 0;">This is your salary breakdown, not a payment.</p>'
        "</td></tr>"
        # Footer
        f'<tr><td style="padding:16px 24px;background:#fafafa;color:#999;font-size:11px;">'
        f"Sent by {escape(brand)} on behalf of {escape(org)}. If you did not expect this, please ignore it."
        "</td></tr>"
        "</table></td></tr></table></body></html>"
    )
    return subject, body_text, body_html
