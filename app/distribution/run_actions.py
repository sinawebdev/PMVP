"""Operator actions on a run's payslip delivery added with SMS (Phase 3).

Two routes on ``distribution_bp``, kept out of ``__init__.py`` so that file stays
under 500 lines; imported at its bottom, the same pattern ``app/client`` uses:

* the confirm step before an SMS or `auto` send (see confirm.py), on one run or
  on the runs list's Bulk Distribute selection, and
* settling a send that may have gone out (see settle.py).

Also the template filter that masks phone numbers wherever a recipient is shown.
"""
from flask import Response, abort, flash, redirect, render_template, request, url_for
from flask_login import current_user

from app import db
from app.audit import record_audit
from app.auth import role_required
from app.models import CHANNEL_AUTO, CHANNEL_SMS, CHANNEL_WHATSAPP, PayrollRun, PayslipDelivery
from app.payroll_status import SENDABLE_STATUSES
from app.permissions import PAYROLL_ROLES

from . import distribution_bp
from .channels import SMS_BLOCKED_MESSAGE, sms_refused
from .confirm import CONFIRM_ACTIONS, CONFIRMED_CHANNELS, bulk_confirm_context, confirm_context
from .phones import mask_msisdn
from .settle import SETTLE_OUTCOMES, SETTLE_SENT, settle_unknown
from .status import delivery_status_context

_SEND_ENDPOINTS = {
    "send": "distribution.send",
    "resend": "distribution.resend_failed",
    "schedule": "distribution.schedule",
}
SETTLE_CONFLICT = (
    "This payslip is no longer unconfirmed, so nothing was changed. "
    "Go back and reload the page to see where it is now."
)


@distribution_bp.app_template_filter("shown_recipient")
def shown_recipient(delivery):
    """A delivery's recipient as a screen may show it: a phone number masked
    (23324***347), an email address as is. Logs use recipient_fingerprint."""
    if delivery is None or not delivery.recipient:
        return "—"
    if delivery.channel in (CHANNEL_SMS, CHANNEL_WHATSAPP):
        return mask_msisdn(delivery.recipient)
    return delivery.recipient


@distribution_bp.route("/run/<int:run_id>/confirm")
@role_required(*PAYROLL_ROLES)
def confirm_send(run_id):
    run = db.get_or_404(PayrollRun, run_id)
    back = url_for("distribution.run_status", run_id=run.id)
    channel = request.args.get("channel", CHANNEL_AUTO)
    action = request.args.get("action", "send")
    if run.status not in SENDABLE_STATUSES:
        flash("Payslips can only be distributed after the payroll run is approved.", "warning")
        return redirect(back)
    if channel not in CONFIRMED_CHANNELS or action not in CONFIRM_ACTIONS:
        return redirect(back)
    if sms_refused(channel):
        flash(SMS_BLOCKED_MESSAGE, "warning")
        return redirect(back)
    confirm = confirm_context(
        run, channel, action, request.args,
        post_url=url_for(_SEND_ENDPOINTS[action], run_id=run.id), back_url=back,
    )
    return render_template(
        "distribution/run_status.html", nonce=confirm["hidden"]["nonce"], confirm=confirm,
        **delivery_status_context(run),
    )


@distribution_bp.route("/runs/confirm")
@role_required(*PAYROLL_ROLES)
def bulk_confirm():
    """Bulk Distribute's confirm step: who the selected runs reach, before any queues."""
    back = url_for("payroll.runs", status=request.args.get("status") or None,
                   client_id=request.args.get("client_id") or None)
    confirm = bulk_confirm_context(
        request.args, current_user.role,
        post_url=url_for("payroll.bulk_distribute"), back_url=back,
    )
    if confirm is None:
        flash("No runs selected for bulk distribute.", "warning")
        return redirect(back)
    return render_template("distribution/bulk_confirm.html", confirm=confirm)


@distribution_bp.route("/run/<int:run_id>/delivery/<int:delivery_id>/settle", methods=["POST"])
@role_required(*PAYROLL_ROLES)
def settle_delivery(run_id, delivery_id):
    """Mark an `unknown` delivery as sent or not sent, after checking SasuSync."""
    run = db.get_or_404(PayrollRun, run_id)
    delivery = db.session.get(PayslipDelivery, delivery_id)
    if delivery is None or delivery.payroll_run_id != run.id:
        abort(404)
    outcome = request.form.get("outcome")
    if outcome not in SETTLE_OUTCOMES:
        abort(400)
    if not settle_unknown(delivery, outcome):
        db.session.rollback()
        return Response(SETTLE_CONFLICT, status=409, mimetype="text/plain")
    record_audit(
        "Unconfirmed delivery settled",
        run,
        f"delivery={delivery.id} item={delivery.payroll_item_id} outcome={outcome}, "
        "after checking the SasuSync portal.",
    )
    db.session.commit()
    worker = delivery.payroll_item.full_name if delivery.payroll_item else "This payslip"
    if outcome == SETTLE_SENT:
        flash(f"{worker}: marked as sent.", "success")
    else:
        flash(f"{worker}: marked as not sent. Use “Resend failed” to send it again.", "info")
    return redirect(url_for("distribution.run_status", run_id=run.id))
