"""Payslip distribution blueprint.

Delivers a payroll run's payslip breakdowns to workers over SMS / WhatsApp / email, with
per-worker delivery tracking, resend-failed, and idempotent sends. Lives inside Chrisnat and
reuses its models, auth, audit, and Jinja/Bootstrap UI.
"""
import os
import uuid

from flask import (
    Blueprint,
    current_app,
    flash,
    redirect,
    render_template,
    request,
    send_file,
    url_for,
)

from flask_login import current_user

from app import db
from app.auth import role_required
from app.models import (
    CHANNEL_AUTO,
    DELIVERY_CHANNELS,
    DistributionBatch,
    Employee,
    PayrollItem,
    PayrollRun,
    PayslipDelivery,
)
from app.payroll_status import SENDABLE_STATUSES
from app.pdf_service import generate_payslip_pdf

from .idempotency import replay_or_run
from .queue import enqueue_distribution
from .service import resolve_channel
from .tokens import verify_payslip_token

distribution_bp = Blueprint("distribution", __name__, url_prefix="/distribution")

from app.permissions import PAYROLL_ROLES  # canonical operator capability group
VALID_SEND_CHANNELS = set(DELIVERY_CHANNELS) | {CHANNEL_AUTO}


# ---------------------------------------------------------------------------
# Monitoring dashboard (Phase 3, Slice 4) — the primary operational view of the
# distribution subsystem: batch/delivery stats, retries, backlog, throughput and
# worker health across every tenant. Operator-plane (role_required blocks tenant
# users), read-only, live-refreshed via htmx.
# ---------------------------------------------------------------------------
@distribution_bp.route("/dashboard")
@role_required(*PAYROLL_ROLES)
def dashboard():
    from .dashboard import collect_dashboard_stats

    return render_template("distribution/dashboard.html", stats=collect_dashboard_stats())


@distribution_bp.route("/dashboard/fragment")
@role_required(*PAYROLL_ROLES)
def dashboard_fragment():
    from .dashboard import collect_dashboard_stats

    return render_template(
        "distribution/_dashboard_fragment.html", stats=collect_dashboard_stats()
    )


def _latest_delivery(item_id):
    return (
        PayslipDelivery.query.filter_by(payroll_item_id=item_id)
        .order_by(PayslipDelivery.created_at.desc())
        .first()
    )


def _latest_batch(run_id):
    return (
        DistributionBatch.query.filter_by(payroll_run_id=run_id)
        .order_by(DistributionBatch.created_at.desc())
        .first()
    )


def _run_status_context(run):
    """Shared by the full page and its auto-refreshing fragment, so both ever
    agree on what "delivery status" means for a run."""
    rows = []
    for item in run.items:
        rows.append(
            {
                "item": item,
                "delivery": _latest_delivery(item.id),
                "suggested": resolve_channel(item),
            }
        )
    sent = sum(1 for r in rows if r["delivery"] and r["delivery"].status == "sent")
    failed = sum(1 for r in rows if r["delivery"] and r["delivery"].status == "failed")
    batch = _latest_batch(run.id)
    # A pending automatic retry (a failed delivery still scheduled) keeps the page
    # live even after the batch itself reached a terminal state, so the operator
    # watches recovery happen.
    pending_retry = any(
        r["delivery"] and r["delivery"].status == "failed" and r["delivery"].next_retry_at
        for r in rows
    )
    batch_active = batch is not None and batch.status in ("queued", "running")
    return {
        "run": run,
        "rows": rows,
        "channels": DELIVERY_CHANNELS,
        "sendable": run.status in SENDABLE_STATUSES,
        "sent_count": sent,
        "failed_count": failed,
        "batch": batch,
        "in_flight": batch_active or pending_retry,
    }


@distribution_bp.route("/run/<int:run_id>")
@role_required(*PAYROLL_ROLES)
def run_status(run_id):
    run = db.get_or_404(PayrollRun, run_id)
    return render_template(
        "distribution/run_status.html", nonce=uuid.uuid4().hex, **_run_status_context(run)
    )


@distribution_bp.route("/run/<int:run_id>/status-fragment")
@role_required(*PAYROLL_ROLES)
def run_status_fragment(run_id):
    run = db.get_or_404(PayrollRun, run_id)
    return render_template(
        "distribution/_status_fragment.html", **_run_status_context(run)
    )


def _do_send(run_id, only_failed):
    run = db.get_or_404(PayrollRun, run_id)
    if run.status not in SENDABLE_STATUSES:
        flash("Payslips can only be distributed after the payroll run is approved.", "warning")
        return redirect(url_for("distribution.run_status", run_id=run.id))

    channel = request.form.get("channel", CHANNEL_AUTO)
    if channel not in VALID_SEND_CHANNELS:
        flash(f"Unknown channel: {channel}", "danger")
        return redirect(url_for("distribution.run_status", run_id=run.id))

    nonce = request.form.get("nonce")
    action = "resend-failed" if only_failed else "send"
    key = f"distribute:{run.id}:{action}:{channel}:{nonce}" if nonce else None

    summary, replayed = replay_or_run(
        key, lambda: enqueue_distribution(run, channel, only_failed, current_user)
    )
    if summary.get("already_in_progress"):
        flash("A distribution is already in progress for this run.", "warning")
    else:
        note = " (already queued)" if replayed else ""
        flash(
            f"Distribution queued{note}: {summary['total']} payslip(s) will be sent shortly.",
            "success",
        )
    return redirect(url_for("distribution.run_status", run_id=run.id))


@distribution_bp.route("/run/<int:run_id>/send", methods=["POST"])
@role_required(*PAYROLL_ROLES)
def send(run_id):
    return _do_send(run_id, only_failed=False)


@distribution_bp.route("/run/<int:run_id>/resend-failed", methods=["POST"])
@role_required(*PAYROLL_ROLES)
def resend_failed(run_id):
    return _do_send(run_id, only_failed=True)


@distribution_bp.route("/item/<int:item_id>/preferred-channel", methods=["POST"])
@role_required(*PAYROLL_ROLES)
def set_preferred_channel(item_id):
    item = db.get_or_404(PayrollItem, item_id)
    channel = request.form.get("preferred_channel") or None
    if channel and channel not in DELIVERY_CHANNELS:
        flash(f"Unknown channel: {channel}", "danger")
    elif item.employee_id:
        employee = db.session.get(Employee, item.employee_id)
        employee.preferred_channel = channel
        db.session.commit()
        flash(f"Preferred channel updated for {item.full_name}.", "success")
    else:
        flash("This payroll row is not linked to an employee record yet.", "warning")
    return redirect(url_for("distribution.run_status", run_id=item.payroll_run_id))


# ---------------------------------------------------------------------------
# Public, no-login payslip link (the worker-facing surface).
# Reached only via a signed, expiring token carried in the SMS/WhatsApp/email.
# These routes are deliberately NOT behind @login_required — the token IS the
# credential, scoped to one payslip and time-limited.
# ---------------------------------------------------------------------------

payslip_link_bp = Blueprint("payslip_link", __name__)


def _item_from_token(token):
    item_id = verify_payslip_token(token)
    if not item_id:
        return None
    return db.session.get(PayrollItem, item_id)


@payslip_link_bp.route("/p/<token>")
def public_payslip(token):
    item = _item_from_token(token)
    if item is None:
        return render_template("distribution/link_expired.html"), 404
    return render_template(
        "distribution/public_payslip.html",
        item=item,
        run=item.payroll_run,
        client=item.payroll_run.client_company if item.payroll_run else None,
        token=token,
    )


@payslip_link_bp.route("/p/<token>/pdf")
def public_payslip_pdf(token):
    item = _item_from_token(token)
    if item is None:
        return render_template("distribution/link_expired.html"), 404
    file_path = generate_payslip_pdf(item, current_app.config["EXPORT_FOLDER"])
    return send_file(
        file_path,
        mimetype="application/pdf",
        as_attachment=True,
        download_name=os.path.basename(file_path),
    )
