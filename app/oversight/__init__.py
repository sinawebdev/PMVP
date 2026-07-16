"""Chrisnat oversight — the risk-gate control plane (PMVP v1 Phase 5).

Platform-only (``@platform_required``) routes for the operator who watches over
every tenant's runs:

  * ``/oversight/risk``                  — every run currently HELD, across tenants
  * ``/oversight/runs/<id>/risk-check``  — (re)score a run through the risk gate
  * ``/oversight/runs/<id>/release``     — release a HELD run into approval

Scoring lives in :mod:`app.risk` (pure/deterministic). These routes own the
PayrollRun.status lifecycle transition and the audit trail, and commit once.
This is oversight *above* tenants, so it intentionally spans all client
companies — hence platform-only, never tenant-scoped.
"""

from datetime import datetime, timezone

from flask import Blueprint, flash, redirect, render_template, request, url_for

from app import db
from app.audit import record_audit
from app.events import record_event, tenant_users
from app.models import PayrollRun
from app.payroll_status import AUTO_ACCEPTED, HELD, PENDING_APPROVAL, RISK_GATED_STATUSES
from app.risk import apply_risk_gate
from app.tenancy import platform_required

oversight_bp = Blueprint("oversight", __name__, url_prefix="/oversight")


@oversight_bp.route("/risk")
@platform_required
def risk_queue():
    """Every held run, newest first — the Chrisnat review queue."""
    held_runs = (
        PayrollRun.query.filter(PayrollRun.status == HELD)
        .order_by(PayrollRun.risk_checked_at.desc(), PayrollRun.id.desc())
        .all()
    )
    return render_template("oversight/risk_queue.html", held_runs=held_runs)


@oversight_bp.route("/runs/<int:run_id>/risk-check", methods=["POST"])
@platform_required
def risk_check(run_id):
    """Score (or re-score) a run through the risk gate and park it accordingly."""
    run = db.get_or_404(PayrollRun, run_id)
    if run.status not in RISK_GATED_STATUSES:
        flash(
            f"Run is {run.status}; the risk gate only applies before approval.",
            "warning",
        )
        return redirect(url_for("payroll.detail", run_id=run.id))
    verdict = apply_risk_gate(run, when=datetime.now(timezone.utc))
    run.status = HELD if verdict.held else AUTO_ACCEPTED
    reasons = verdict.reasons_text() or "No rule tripped."
    record_audit("Risk gate evaluated", run, f"Verdict: {verdict.status}. {reasons}")
    # Append a domain event and notify the client's users so a hold is visible
    # to the tenant, not just to Chrisnat.
    record_event(
        "run.risk_held" if verdict.held else "run.risk_accepted",
        summary=f"{run.month} {run.year}: {reasons}",
        subject=run,
        level="warning" if verdict.held else "success",
        payload={"status": verdict.status, "reasons": verdict.reasons},
        recipients=tenant_users(run.client_company_id) if verdict.held else None,
    )
    db.session.commit()
    if verdict.held:
        flash("Run held for review — a risk rule was tripped.", "warning")
    else:
        flash("Run auto-accepted — no risk rule tripped.", "success")
    return redirect(url_for("payroll.detail", run_id=run.id))


@oversight_bp.route("/runs/<int:run_id>/release", methods=["POST"])
@platform_required
def release_hold(run_id):
    """Release a HELD run into the operator approval queue (Pending Approval)."""
    run = db.get_or_404(PayrollRun, run_id)
    if run.status != HELD:
        flash("Only a held run can be released.", "warning")
        return redirect(url_for("payroll.detail", run_id=run.id))
    run.status = PENDING_APPROVAL
    note = (request.form.get("notes") or "").strip()
    record_audit(
        "Risk hold released",
        run,
        f"Released to Pending Approval by oversight. {note}".strip(),
    )
    record_event(
        "run.hold_released",
        summary=f"{run.month} {run.year} payroll released for approval. {note}".strip(),
        subject=run,
        level="info",
        recipients=tenant_users(run.client_company_id),
    )
    db.session.commit()
    flash("Hold released — run moved to Pending Approval.", "success")
    return redirect(url_for("payroll.detail", run_id=run.id))
