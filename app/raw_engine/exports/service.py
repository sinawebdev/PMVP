"""Regenerate the full export family for a computed raw run.

Reuses the standard writers where they already fit (wage sheet, GRA PAYE return,
payslip PDFs — all run off ``PayrollItem`` rows) and adds the raw-specific
outputs (AKOTO bank grouping, PV cash list, ICU distribution). Returns a
manifest of the files written plus the bank/PV routing summary.
"""
from app.excel_utils import export_gra_paye_schedule, export_wages_sheet
from app.raw_engine.exports.bank_routing import route_payments
from app.raw_engine.exports.writers import (
    export_bank_grouping,
    export_icu_distribution,
    export_pv,
)


def generate_run_exports(
    payroll_run,
    export_folder,
    *,
    employer_tin="",
    tax_office="",
    whitelist=None,
    include_payslips=False,
):
    """Write every export for ``payroll_run`` into ``export_folder``.

    Returns ``{"files": {...}, "routing": {...}}``. ``routing.complete`` is True
    only when every worker landed in exactly one of {bank, PV}.
    """
    items = list(payroll_run.items)
    routing = route_payments(items, whitelist=whitelist)

    files = {
        "wages_sheet": export_wages_sheet(payroll_run, export_folder),
        "gra_paye": export_gra_paye_schedule(
            payroll_run, export_folder, employer_tin=employer_tin, tax_office=tax_office
        ),
        "bank_grouping": export_bank_grouping(payroll_run, export_folder, routing=routing),
        "pv": export_pv(payroll_run, export_folder, routing=routing),
        "icu_distribution": export_icu_distribution(payroll_run, export_folder),
    }

    if include_payslips:
        from app.pdf_service import generate_payslip_pdf

        files["payslips"] = [
            generate_payslip_pdf(item, export_folder) for item in items
        ]

    return {
        "files": files,
        "routing": {
            "banked_workers": len(routing.banked),
            "pv_workers": len(routing.pv),
            "banked_total": routing.banked_total,
            "pv_total": routing.pv_total,
            "routed_total": routing.routed_total,
            "complete": routing.is_complete(items),
        },
    }
