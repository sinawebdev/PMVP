"""Statutory compliance readiness for one client company.

"Are we compliant?" is a question with a real answer in this app's data: PAYE is
computed against a live statutory rate version, employees do or do not carry an
SSNIT number, a TIN, a Ghana Card and a way to be paid, and a run either has
reached a status that unlocks the GRA filing exports or it has not.

This module turns those five recorded facts into a **weighted score plus the
five rows that produced it** — never a bare number. An executive who cannot
reconcile a compliance score line by line cannot defend it, so every check
carries its own count ("7 of 42 are missing an SSNIT number") and a link to the
screen that fixes it.

Deliberately narrow: it asserts nothing the data cannot prove. There is no check
for a filing actually being *submitted* to GRA or SSNIT, because the app does not
record that — inventing it would make the score a comfort blanket rather than a
control.

Read-only and query-free except for the active rate lookup: ``employees`` is
passed in already tenant-scoped by the caller (the same contract as
:mod:`app.analytics`), so this cannot leak across tenants.
"""

from datetime import date as _date

from app.models import StatutoryRate
from app.payroll_status import CLOSED_STATUSES

# (key, label, weight). Weighted because the gaps are not equivalent: a missing
# SSNIT number blocks a statutory filing, while a missing Ghana Card is a
# record-keeping gap. Scoring them equally would make the number meaningless.
# Weights sum to 1.0 — asserted below, so a future edit cannot silently produce
# a score that can never reach 100.
COMPLIANCE_WEIGHTS = (
    ("paye", 0.25),
    ("ssnit", 0.25),
    ("payment", 0.20),
    ("documents", 0.15),
    ("filing", 0.15),
)
assert abs(sum(weight for _key, weight in COMPLIANCE_WEIGHTS) - 1.0) < 1e-9


def _ratio_check(present, total):
    """A coverage ratio and its state.

    An empty roster scores 1.0, not 0.0: a company with no active employees is
    not *non-compliant*, it simply has nothing to be compliant about. Scoring it
    as a failure would tell a newly onboarded company it was in breach on day
    one, which is both wrong and the fastest way to teach someone to ignore this
    panel."""
    if not total:
        return 1.0, "ok"
    ratio = present / total
    return ratio, "ok" if ratio == 1 else ("warn" if ratio >= 0.8 else "danger")


def _missing(count, total, what):
    return f"{count} of {total} active employees {what}."


def compliance_overview(employees, latest_run, today=None):
    """``{score, state, headline, checks, gaps}`` for one company.

    ``score`` is the weighted mean of the five coverage ratios, 0-100. ``checks``
    is the five rows behind it, each ``{key, label, state, detail, endpoint}``
    with ``state`` in ``ok`` | ``warn`` | ``danger``.
    """
    today = today or _date.today()
    active = [e for e in employees if (e.status or "").strip().lower() == "active"]
    total = len(active)

    with_ssnit = sum(1 for e in active if e.ssnit_number)
    with_payment = sum(1 for e in active if e.bank_account_number or e.momo_number)
    with_docs = sum(1 for e in active if e.tin and e.ghana_card_number)

    ssnit_ratio, ssnit_state = _ratio_check(with_ssnit, total)
    pay_ratio, pay_state = _ratio_check(with_payment, total)
    doc_ratio, doc_state = _ratio_check(with_docs, total)

    # PAYE is a two-part fact: a rate version must be in force AND a run must
    # have actually computed tax against it. A live rate with no run yet is a
    # half-mark, not a pass — nothing has been proven.
    rate = StatutoryRate.active_for(today)
    paye_ok = rate is not None and bool(latest_run and (latest_run.total_paye or 0) > 0)
    paye_ratio = 1.0 if paye_ok else (0.5 if rate is not None else 0.0)
    filing_ok = bool(latest_run and latest_run.status in CLOSED_STATUSES)

    checks = [
        {
            "key": "paye", "label": "PAYE",
            "state": "ok" if paye_ok else ("warn" if rate is not None else "danger"),
            "detail": (
                f"Computed on {latest_run.month} {latest_run.year} at the rates "
                f"effective {rate.effective_from:%d %b %Y}." if paye_ok
                else "No PAYE computed yet — run a payroll to generate a GRA schedule."
                if rate is not None
                else "No active statutory rate version. Contact Payrolla."
            ),
            "endpoint": "client.statutory",
        },
        {
            "key": "ssnit", "label": "SSNIT",
            "state": ssnit_state,
            "detail": (
                "No active employees on the roster." if not total
                else f"All {total} active employees have an SSNIT number."
                if ssnit_state == "ok"
                else _missing(total - with_ssnit, total, "are missing an SSNIT number")
            ),
            "endpoint": "client.employees",
        },
        {
            "key": "payment", "label": "Payment details",
            "state": pay_state,
            "detail": (
                "No active employees on the roster." if not total
                else "Every active employee has a bank account or MoMo number."
                if pay_state == "ok"
                else _missing(
                    total - with_payment, total,
                    "cannot be paid — no bank account or MoMo number"
                )
            ),
            "endpoint": "client.employees",
        },
        {
            "key": "documents", "label": "Employee documents",
            "state": doc_state,
            "detail": (
                "No active employees on the roster." if not total
                else "TIN and Ghana Card recorded for every active employee."
                if doc_state == "ok"
                else _missing(total - with_docs, total, "are missing a TIN or Ghana Card number")
            ),
            "endpoint": "client.employees",
        },
        {
            "key": "filing", "label": "Filing readiness",
            "state": "ok" if filing_ok else "warn",
            "detail": (
                f"{latest_run.month} {latest_run.year} is {latest_run.status.lower()} — "
                "GRA PAYE schedule and bank listing are downloadable." if filing_ok
                else "Exports unlock once a payroll run is approved."
            ),
            "endpoint": "client.runs",
        },
    ]

    ratios = {
        "paye": paye_ratio, "ssnit": ssnit_ratio, "payment": pay_ratio,
        "documents": doc_ratio, "filing": 1.0 if filing_ok else 0.0,
    }
    score = round(sum(ratios[key] * weight for key, weight in COMPLIANCE_WEIGHTS) * 100)
    gaps = [check["label"] for check in checks if check["state"] != "ok"]

    return {
        "score": score,
        "state": "ok" if not gaps else ("warn" if score >= 80 else "danger"),
        "headline": (
            "Fully compliant" if not gaps
            else "1 area needs attention" if len(gaps) == 1
            else f"{len(gaps)} areas need attention"
        ),
        "checks": checks,
        "gaps": gaps,
    }
