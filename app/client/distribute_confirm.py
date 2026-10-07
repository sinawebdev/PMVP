"""The company admin's confirm step before an SMS or `auto` send (SMS Phase 3).

The same preview the operator sees (app/distribution/confirm.py), on the
tenant's own distribute page. Imported at the bottom of app/client/__init__.py,
like raw, reports and expenses, so its route attaches to client_bp.

Tenant-scoped like every other distribute route: tenant_get_or_404 makes
another company's run a 404, and only a client_admin (who alone may send)
reaches it at all.
"""
from flask import flash, redirect, render_template, request, url_for
from flask_login import current_user

from app.distribution.channels import SMS_BLOCKED_MESSAGE, sms_refused
from app.distribution.confirm import CONFIRMED_CHANNELS, confirm_context
from app.distribution.status import delivery_status_context
from app.models import CHANNEL_AUTO, PayrollRun
from app.payroll_status import SENDABLE_STATUSES
from app.permissions import can_send_payslips
from app.roles import CLIENT_ADMIN
from app.tenancy import active_tenant_id, tenant_get_or_404, tenant_role_required

from . import _company, client_bp

# A company admin sends and resends; scheduling is an operator tool.
_SEND_ENDPOINTS = {"send": "client.distribute_send", "resend": "client.distribute_resend"}


@client_bp.route("/runs/<int:run_id>/distribute/confirm")
@tenant_role_required(CLIENT_ADMIN)
def distribute_confirm(run_id):
    run = tenant_get_or_404(PayrollRun, run_id)  # 404 if another tenant's run
    back = url_for("client.distribute", run_id=run.id)
    channel = request.args.get("channel", CHANNEL_AUTO)
    action = request.args.get("action", "send")
    if run.status not in SENDABLE_STATUSES:
        flash("Payslips can only be sent after the payroll run is approved.", "warning")
        return redirect(back)
    if channel not in CONFIRMED_CHANNELS or action not in _SEND_ENDPOINTS:
        return redirect(back)
    if sms_refused(channel):
        flash(SMS_BLOCKED_MESSAGE, "warning")
        return redirect(back)
    confirm = confirm_context(
        run, channel, action, request.args,
        post_url=url_for(_SEND_ENDPOINTS[action], run_id=run.id), back_url=back,
    )
    return render_template(
        "client/distribute.html",
        company=_company(),
        can_send=active_tenant_id() is not None and can_send_payslips(current_user.role),
        nonce=confirm["hidden"]["nonce"],
        confirm=confirm,
        **delivery_status_context(run),
    )
