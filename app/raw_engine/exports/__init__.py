"""Raw Hours Engine exports (Phase 5).

Reproduces the operator's operational outputs by **reusing the existing writers**
(`app.excel_utils`, `app.pdf_service`) wherever they already fit — wage sheet,
GRA PAYE return, and payslip PDFs all run off the run's ``PayrollItem`` rows,
which the raw engine already populates. This package adds only the pieces the
raw path needs on top:

  * `bank_routing` — split every net-pay worker into exactly one of {bank, PV}
    using the **config** bank whitelist (never a hardcoded formula list).
  * `writers` — the AKOTO bank-grouped schedule, the PV (cash) voucher list, and
    the ICU union-distribution export.
  * `service` — one call that regenerates the full export family for a run.
"""
from app.raw_engine.exports.bank_routing import PaymentRouting, route_payments
from app.raw_engine.exports.service import generate_run_exports

__all__ = ["PaymentRouting", "route_payments", "generate_run_exports"]
