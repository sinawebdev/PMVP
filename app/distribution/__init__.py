"""Payslip distribution blueprint.

Delivers a payroll run's payslip breakdowns to workers over SMS / WhatsApp / email, with
per-worker delivery tracking, resend-failed, and idempotent sends. Lives inside Chrisnat and
reuses its models, auth, audit, and Jinja/Bootstrap UI.
"""
import os
import uuid
from datetime import datetime, timezone

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
from .queue import (
    cancel_distribution,
    cancel_flash_message,
    enqueue_distribution,
    reschedule_distribution,
)
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


# ---------------------------------------------------------------------------
# Searchable delivery history (Phase 3, Slice 6) + per-batch detail pages.
# ---------------------------------------------------------------------------
@distribution_bp.route("/history")
@role_required(*PAYROLL_ROLES)
def history():
    from .history import filter_options, search_deliveries

    try:
        page = max(1, int(request.args.get("page", 1)))
    except (TypeError, ValueError):
        page = 1
    pagination = search_deliveries(request.args, page=page)
    base_args = {k: v for k, v in request.args.items() if k != "page"}
    return render_template(
        "distribution/history.html",
        pagination=pagination,
        deliveries=pagination.items,
        options=filter_options(),
        filters=request.args,
        base_args=base_args,
    )


@distribution_bp.route("/analytics")
@role_required(*PAYROLL_ROLES)
def analytics():
    from .analytics import delivery_analytics
    from .history import filter_options

    return render_template(
        "distribution/analytics.html",
        stats=delivery_analytics(request.args),
        options=filter_options(),
        filters=request.args,
    )


@distribution_bp.route("/history/export.csv")
@role_required(*PAYROLL_ROLES)
def history_export_csv():
    from .analytics import export_deliveries_csv

    data, filename = export_deliveries_csv(request.args)
    return current_app.response_class(
        data,
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@distribution_bp.route("/history/export.xlsx")
@role_required(*PAYROLL_ROLES)
def history_export_xlsx():
    from .analytics import export_deliveries_xlsx

    data, filename = export_deliveries_xlsx(request.args)
    return current_app.response_class(
        data,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@distribution_bp.route("/batch/<int:batch_id>")
@role_required(*PAYROLL_ROLES)
def batch_detail(batch_id):
    from app.events import run_activity
    from app.models import DistributionBatch

    batch = db.get_or_404(DistributionBatch, batch_id)
    run = db.session.get(PayrollRun, batch.payroll_run_id)
    deliveries = (
        PayslipDelivery.query.filter_by(distribution_batch_id=batch.id)
        .order_by(PayslipDelivery.updated_at.desc())
        .all()
    )
    return render_template(
        "distribution/batch_detail.html",
        batch=batch,
        run=run,
        deliveries=deliveries,
        activity=run_activity(run) if run else [],
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
    scheduled = batch is not None and batch.status == "scheduled"
    seconds_until = None
    if scheduled and batch.scheduled_for is not None:
        target = batch.scheduled_for
        if target.tzinfo is None:
            target = target.replace(tzinfo=timezone.utc)
        seconds_until = int((target - datetime.now(timezone.utc)).total_seconds())
    return {
        "run": run,
        "rows": rows,
        "channels": DELIVERY_CHANNELS,
        "sendable": run.status in SENDABLE_STATUSES,
        "sent_count": sent,
        "failed_count": failed,
        "batch": batch,
        "in_flight": batch_active or pending_retry or scheduled,
        # Drives live polling — a far-future scheduled batch changes nothing
        # second-to-second, so it does not poll (only active/retrying does).
        "live": batch_active or pending_retry,
        "scheduled": scheduled,
        "seconds_until_scheduled": seconds_until,
        # Cancellable == there is not-yet-sent work to stop and no send is
        # actively running (a running batch is never cancelled mid-flight).
        "cancellable": scheduled
        or (batch is not None and batch.status == "queued")
        or pending_retry,
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


def _hx_redirect(url):
    """Reload the page after a mutating action. For an htmx request this returns
    an HX-Redirect (htmx does a full navigation, so blocks outside the swapped
    fragment — e.g. the send controls in the header — refresh too); otherwise a
    normal redirect."""
    if request.headers.get("HX-Request"):
        resp = current_app.make_response("")
        resp.headers["HX-Redirect"] = url
        return resp
    return redirect(url)


@distribution_bp.route("/run/<int:run_id>/cancel", methods=["POST"])
@role_required(*PAYROLL_ROLES)
def cancel(run_id):
    run = db.get_or_404(PayrollRun, run_id)
    result = cancel_distribution(run, current_user)
    flash(*cancel_flash_message(result))
    return _hx_redirect(url_for("distribution.run_status", run_id=run.id))


def _parse_schedule(value):
    """A datetime-local form value -> aware UTC datetime. Ghana runs on GMT
    year-round (no DST), so the operator's wall clock IS UTC — we interpret the
    naive input as UTC, which is unambiguous and DST-safe. None on blank/bad."""
    value = (value or "").strip()
    for fmt in ("%Y-%m-%dT%H:%M", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(value, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


@distribution_bp.route("/run/<int:run_id>/schedule", methods=["POST"])
@role_required(*PAYROLL_ROLES)
def schedule(run_id):
    run = db.get_or_404(PayrollRun, run_id)
    if run.status not in SENDABLE_STATUSES:
        flash("Payslips can only be distributed after the payroll run is approved.", "warning")
        return redirect(url_for("distribution.run_status", run_id=run.id))
    channel = request.form.get("channel", CHANNEL_AUTO)
    if channel not in VALID_SEND_CHANNELS:
        flash(f"Unknown channel: {channel}", "danger")
        return redirect(url_for("distribution.run_status", run_id=run.id))
    when = _parse_schedule(request.form.get("scheduled_for"))
    if when is None or when <= datetime.now(timezone.utc):
        flash("Pick a valid future date and time to schedule the distribution.", "warning")
        return redirect(url_for("distribution.run_status", run_id=run.id))
    summary = enqueue_distribution(run, channel, False, current_user, scheduled_for=when)
    if summary.get("already_in_progress"):
        flash("A distribution is already scheduled or in progress for this run.", "warning")
    else:
        flash(
            f"Distribution scheduled for {when.strftime('%Y-%m-%d %H:%M')} GMT "
            f"({summary['total']} payslip(s)).",
            "success",
        )
    return redirect(url_for("distribution.run_status", run_id=run.id))


@distribution_bp.route("/run/<int:run_id>/reschedule", methods=["POST"])
@role_required(*PAYROLL_ROLES)
def reschedule(run_id):
    run = db.get_or_404(PayrollRun, run_id)
    when = _parse_schedule(request.form.get("scheduled_for"))
    result = reschedule_distribution(run, when, current_user)
    if result["ok"]:
        flash(f"Distribution rescheduled to {when.strftime('%Y-%m-%d %H:%M')} GMT.", "success")
    elif result["reason"] == "not_future":
        flash("Pick a valid future date and time.", "warning")
    else:
        flash("There is no scheduled distribution to change for this run.", "warning")
    return redirect(url_for("distribution.run_status", run_id=run.id))


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
