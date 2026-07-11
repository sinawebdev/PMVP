"""Validation layer — enforce correctness before a raw run is savable.

Rules (spec §6):
  * **Hours reconciliation — hard block.** Σ raw hours must equal Σ consolidated
    matrix hours (catches case-sensitivity drops and dropped rows).
  * **ICU tie-out — hard block.** Σ ICU dues must equal what the union-distribution
    export pays out. A mismatched remittance is a compliance error.
  * **Unknown Staff ID — hard block.** A thin row with no seeded employee.
  * **Non-member with an ICU amount — flag.** Someone paying dues who is not
    seeded as a member.
  * **Recompute drift — warning.** Recompute-vs-source cross-check (data quality).

A run is savable only when there are no blocks; flags and warnings surface for
operator judgement but do not stop the run.
"""
from dataclasses import dataclass, field

from app.money import money
from app.raw_engine.cleaning import coerce_hours
from app.raw_engine.consolidation import total_hours
from app.raw_engine.icu_distribution import distribute_union_dues

BLOCK = "block"
FLAG = "flag"
WARNING = "warning"


@dataclass
class ValidationIssue:
    code: str
    severity: str
    message: str
    detail: dict = field(default_factory=dict)


@dataclass
class ValidationReport:
    issues: list = field(default_factory=list)

    def add(self, issue):
        if issue:
            self.issues.append(issue)

    def extend(self, issues):
        self.issues.extend(i for i in issues if i)

    @property
    def blocks(self):
        return [i for i in self.issues if i.severity == BLOCK]

    @property
    def flags(self):
        return [i for i in self.issues if i.severity == FLAG]

    @property
    def warnings(self):
        return [i for i in self.issues if i.severity == WARNING]

    @property
    def is_savable(self):
        """A run may be saved only if nothing hard-blocks it."""
        return not self.blocks


# --- individual rules (pure, testable) -------------------------------------

def reconcile_hours(raw_rows, consolidated, tol=0.01):
    """Σ raw hours vs Σ consolidated matrix hours. ``raw_rows``: iterable of
    ``(staff, pay_code, hours)`` — the pre-consolidation input. A mismatch means
    a row was dropped (blank/unlisted key, case-sensitivity) — a hard block."""
    raw = sum(coerce_hours(h) for _s, _c, h in raw_rows)
    cons = total_hours(consolidated)
    if abs(raw - cons) > tol:
        return ValidationIssue(
            "hours_reconciliation",
            BLOCK,
            f"Hours reconciliation failed: raw total {raw:.2f} != consolidated "
            f"total {cons:.2f} (lost {raw - cons:+.2f}). A row was dropped — "
            f"check for blank/unlisted staff IDs or element labels.",
            {"raw_total": round(raw, 2), "consolidated_total": round(cons, 2)},
        )
    return None


def check_icu_tie_out(icu_total, distribution=None, tol=0.01):
    """Σ ICU dues must equal the union-distribution payout. Built correctly the
    cascade always ties; this blocks if the export would remit a different total
    than was deducted."""
    icu_total = money(icu_total)
    dist = distribution if distribution is not None else distribute_union_dues(icu_total)
    payout = dist.total_payout
    if abs(payout - icu_total) > tol:
        return ValidationIssue(
            "icu_tie_out",
            BLOCK,
            f"ICU tie-out failed: dues deducted {icu_total:.2f} != union "
            f"remittance {payout:.2f}. Remittance must equal deductions.",
            {"icu_total": icu_total, "payout": payout},
        )
    return None


def flag_nonmember_icu(records):
    """Flag any non-member carrying ICU dues. ``records``: iterable of
    ``(staff_id, icu_dues, is_member)``. Warning, not a block."""
    issues = []
    for staff_id, icu_dues, is_member in records:
        if (icu_dues or 0) > 0 and not is_member:
            issues.append(ValidationIssue(
                "nonmember_icu",
                FLAG,
                f"{staff_id}: ICU dues {float(icu_dues):.2f} on a worker not "
                f"seeded as a union member. Seed as a member via a rich upload, "
                f"or remove the dues.",
                {"staff_id": staff_id, "icu_dues": float(icu_dues)},
            ))
    return issues


def block_unknown_staff(blocked):
    """Turn a thin run's blocked list into hard-block issues. ``blocked``:
    iterable of dicts with ``staff_id`` / ``reason``."""
    issues = []
    for entry in blocked or []:
        issues.append(ValidationIssue(
            "unknown_staff_id",
            BLOCK,
            f"{entry.get('staff_id')}: {entry.get('reason')}",
            {"staff_id": entry.get("staff_id")},
        ))
    return issues


def recompute_drift(records, tol=0.02):
    """Recompute-vs-source cross-check. ``records``: iterable of
    ``(staff_id, field, computed, source)``. Warning per drifting field."""
    issues = []
    for staff_id, field_name, computed, source in records:
        diff = (computed or 0) - (source or 0)
        if abs(diff) > tol:
            issues.append(ValidationIssue(
                "recompute_drift",
                WARNING,
                f"{staff_id}: {field_name} recomputed {float(computed):.2f} vs "
                f"source {float(source):.2f} (drift {diff:+.2f}).",
                {"staff_id": staff_id, "field": field_name,
                 "computed": float(computed), "source": float(source)},
            ))
    return issues


# --- orchestrator ----------------------------------------------------------

def validate_run(
    items,
    membership,
    *,
    raw_rows=None,
    consolidated=None,
    blocked=None,
    source_totals=None,
):
    """Full validation for a computed run.

    ``items``: PayrollItem-like objects (need ``staff_id``, ``icu_dues``,
    ``gross_pay``, ``net_pay``). ``membership``: ``{staff_id: is_member}``.
    Optional: pre-consolidation ``raw_rows`` + ``consolidated`` map for the
    hours reconciliation, a thin run's ``blocked`` list, and ``source_totals``
    (``{staff_id: {'gross': x, 'net': y}}``) for recompute drift.
    """
    report = ValidationReport()

    if raw_rows is not None and consolidated is not None:
        report.add(reconcile_hours(raw_rows, consolidated))

    icu_total = money(sum((i.icu_dues or 0) for i in items))
    report.add(check_icu_tie_out(icu_total))

    report.extend(flag_nonmember_icu(
        (i.staff_id, (i.icu_dues or 0), bool(membership.get(i.staff_id, False)))
        for i in items
    ))

    report.extend(block_unknown_staff(blocked))

    if source_totals:
        drift_records = []
        for i in items:
            src = source_totals.get(i.staff_id)
            if not src:
                continue
            if "gross" in src:
                drift_records.append((i.staff_id, "gross", i.gross_pay, src["gross"]))
            if "net" in src:
                drift_records.append((i.staff_id, "net", i.net_pay, src["net"]))
        report.extend(recompute_drift(drift_records))

    return report
