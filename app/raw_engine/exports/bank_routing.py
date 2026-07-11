"""Route every net-pay worker to exactly one of {bank, PV}.

A worker is paid by bank transfer only if their bank is on the recognised
whitelist **and** they have an account number to transfer to; everyone else is
paid by cash voucher (PV). The whitelist is config (``RAW_BANK_WHITELIST``),
never a hardcoded formula list — the DZ workbook's nested-IF inlined 10 banks,
so a bank outside the list silently fell through to 0 and was never paid. This
routing makes that impossible: ``banked`` and ``pv`` together cover every item
exactly once (validated by ``PaymentRouting.is_complete``).
"""
import re
from dataclasses import dataclass, field

from app.money import money


def normalise_bank(name):
    """Upper-case, single-spaced bank name for whitelist matching."""
    return re.sub(r"\s+", " ", str(name or "").strip()).upper()


def configured_whitelist():
    """The bank whitelist from app config; empty list if outside an app context."""
    try:
        from flask import current_app

        return current_app.config.get("RAW_BANK_WHITELIST") or []
    except Exception:
        return []


@dataclass
class PaymentRouting:
    banked: list = field(default_factory=list)   # items paid by bank transfer
    pv: list = field(default_factory=list)       # items paid by cash voucher
    whitelist: set = field(default_factory=set)

    @property
    def banked_total(self):
        return money(sum((i.net_pay or 0) for i in self.banked))

    @property
    def pv_total(self):
        return money(sum((i.net_pay or 0) for i in self.pv))

    @property
    def routed_total(self):
        """Bank + PV — must equal the run's net total (nobody unrouted)."""
        return money(self.banked_total + self.pv_total)

    def is_complete(self, items):
        """Every item lands in exactly one bucket — no worker unrouted or
        double-counted."""
        banked_ids = {id(i) for i in self.banked}
        pv_ids = {id(i) for i in self.pv}
        all_ids = {id(i) for i in items}
        return (
            banked_ids.isdisjoint(pv_ids)
            and (banked_ids | pv_ids) == all_ids
        )


def route_payments(items, whitelist=None):
    """Split ``items`` into bank-transfer vs cash/PV using the bank whitelist.
    Falls back to the configured whitelist when none is passed."""
    names = whitelist if whitelist is not None else configured_whitelist()
    wl = {normalise_bank(b) for b in names}

    routing = PaymentRouting(whitelist=wl)
    for item in items:
        bank = normalise_bank(item.bank_name)
        has_account = bool(str(item.bank_account_number or "").strip())
        if bank in wl and has_account:
            routing.banked.append(item)
        else:
            routing.pv.append(item)
    return routing
