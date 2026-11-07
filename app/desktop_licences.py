"""Desktop trial licences, issued from an access request and emailed at once.

An operator approves a request that asked for the desktop app; the server signs
a trial licence (app.licensing) with the dedicated trial key, records it in
``DesktopLicence`` and emails it with the download link. Nothing here is
automatic beyond that click: who gets a licence is still an operator's call.

Off unless ``TRIAL_LICENCE_SIGNING_KEY`` is set, which also keeps it off on a
desktop install (that vendors this code but never holds a key).
"""

import json
from datetime import date, datetime, timezone
from html import escape

from flask import Blueprint, current_app, flash, redirect, render_template, url_for
from flask_login import current_user

from app import db
from app.audit import record_audit
from app.auth import role_required
from app.distribution.channels import Attachment, OutboundMessage, get_sender
from app.licensing import LicenseError, issue_trial
from app.models import ACCESS_APPROVED, ACCESS_DECLINED, AccessRequest, DesktopLicence
from app.paging import paginate
from app.permissions import CLIENT_MANAGEMENT_ROLES

licences_bp = Blueprint("licences", __name__)

DESKTOP_INTERESTS = ("desktop", "both")


def licences_enabled():
    return bool(current_app.config.get("TRIAL_LICENCE_SIGNING_KEY"))


def trial_expiry():
    return date.fromisoformat(current_app.config["TRIAL_LICENCE_EXPIRY"])


def trial_offer_ends():
    """The advertised end of the free-trial offer, or None if unset or malformed."""
    try:
        return date.fromisoformat(current_app.config.get("TRIAL_OFFER_ENDS") or "")
    except ValueError:
        return None


def long_date(value):
    return f"{value.day} {value:%B %Y}"


def wants_desktop(entry):
    return entry.interest in DESKTOP_INTERESTS


# --- the email ------------------------------------------------------------------

def licence_message(record, contact_name):
    cfg = current_app.config
    download = cfg.get("DESKTOP_DOWNLOAD_URL")
    expires = long_date(record.expires_on)
    offer_date = trial_offer_ends()
    offer = long_date(offer_date) if offer_date else "the end of the year"
    step_one = (f"Download Payrolla Desktop for Windows: {download}" if download
                else "Reply to this email and we'll send you the installer.")
    steps = [
        step_one,
        "Install it and open Payrolla.",
        f"On the Activate screen, choose the attached file, {record.filename}.",
        "Create your administrator account, then add your company and staff.",
    ]
    fresh = ("Tried an earlier copy of Payrolla Desktop and want a clean start? Uninstall it, "
             "delete the folder %LOCALAPPDATA%\\Payrolla, then install the new download.")
    text = (
        f"Hello {contact_name},\n\n"
        f"Thank you for your interest in Payrolla. {record.firm_name}'s free trial licence for "
        f"Payrolla Desktop is attached ({record.filename}). It runs until {expires}.\n\n"
        + "".join(f"{n}. {s}\n" for n, s in enumerate(steps, 1))
        + f"\n{fresh}\n\n"
        "Keep the licence file safe: you will need it again if you reinstall.\n\n"
        f"The trial is free until {offer}. The licence runs to {expires} so you can finish "
        "December payroll. Reply to this email with any question.\n\n"
        "The Payrolla team\nSinaforte Technologies\n"
    )
    link = (f'<a href="{escape(download)}">Download Payrolla Desktop for Windows</a>'
            if download else "Reply to this email and we'll send you the installer.")
    html_steps = [link] + [escape(s) for s in steps[1:]]
    html = (
        f"<p>Hello {escape(contact_name)},</p>"
        f"<p>Thank you for your interest in Payrolla. {escape(record.firm_name)}'s free trial "
        f"licence for Payrolla Desktop is attached (<code>{escape(record.filename)}</code>). "
        f"It runs until <strong>{escape(expires)}</strong>.</p>"
        "<ol>" + "".join(f"<li>{s}</li>" for s in html_steps) + "</ol>"
        f"<p>{escape(fresh)}</p>"
        "<p>Keep the licence file safe: you will need it again if you reinstall.</p>"
        f"<p>The trial is free until {escape(offer)}. The licence runs to "
        f"{escape(expires)} so you can finish December payroll. Reply to this email with "
        "any question.</p>"
        "<p>The Payrolla team<br>Sinaforte Technologies</p>"
    )
    return OutboundMessage(
        channel="email",
        recipient=record.recipient,
        subject=f"Your Payrolla Desktop trial licence for {record.firm_name}",
        body_text=text,
        body_html=html,
        attachments=[Attachment(record.filename, record.document.encode("utf8"),
                                "application/json")],
    )


def deliver(record, contact_name):
    """Email ``record`` and note the outcome on it. Caller commits."""
    result = get_sender("email").send(licence_message(record, contact_name))
    if result.ok:
        record.emailed_at = datetime.now(timezone.utc)
        record.email_error = None
    else:
        record.email_error = (result.error or "not sent")[:255]
    return result


def _contact_name(record):
    entry = record.access_request
    return entry.contact_name if entry is not None else record.firm_name


def _flash_result(result, record):
    if result.ok:
        flash(f"Trial licence {record.license_id} emailed to {record.recipient}.", "success")
    else:
        flash(f"Licence {record.license_id} was issued, but the email to {record.recipient} did "
              f"not send ({record.email_error}). Use Resend once email is working.", "warning")


# --- routes -----------------------------------------------------------------------

@licences_bp.route("/clients/requests/<int:request_id>/licence", methods=["POST"])
@role_required(*CLIENT_MANAGEMENT_ROLES)
def issue_for_request(request_id):
    entry = db.get_or_404(AccessRequest, request_id)
    back = redirect(url_for("access.detail", request_id=entry.id))
    if not licences_enabled():
        flash("Trial licences are not set up on this server (TRIAL_LICENCE_SIGNING_KEY).", "warning")
        return back
    if not wants_desktop(entry) or entry.status == ACCESS_DECLINED:
        flash("This request did not ask for the desktop app.", "info")
        return back
    existing = entry.licences.first()
    if existing is not None:
        flash(f"{entry.company_name} already has licence {existing.license_id}. Use Resend.", "info")
        return back

    cfg = current_app.config
    try:
        document = issue_trial(cfg["TRIAL_LICENCE_SIGNING_KEY"], entry.company_name,
                               trial_expiry(), cfg["TRIAL_MAX_EMPLOYEES"])
    except (LicenseError, ValueError) as exc:
        current_app.logger.warning("[licence] could not issue for request %s: %s", entry.id, exc)
        flash(f"The licence could not be issued: {exc}", "danger")
        return back

    record = DesktopLicence(
        license_id=document["license_id"], install_id=document["install_id"],
        firm_name=document["firm_name"], plan=document["plan"],
        expires_on=date.fromisoformat(document["license_expiry"]),
        max_employees=document["max_employees"],
        document=json.dumps(document, indent=2) + "\n",
        recipient=entry.email, access_request_id=entry.id, issued_by_id=current_user.id,
    )
    db.session.add(record)
    # A desktop-only request is fully answered by its licence. "Both" still
    # needs its web company, so it stays open for "Approve and create company".
    if entry.is_open and entry.interest == "desktop":
        entry.status = ACCESS_APPROVED
        entry.reviewed_by_id = current_user.id
        entry.reviewed_at = datetime.now(timezone.utc)
    db.session.flush()
    result = deliver(record, entry.contact_name)
    record_audit("Desktop trial licence issued", entry,
                 f"{record.license_id} for {record.firm_name}, expires {record.expires_on}, "
                 f"emailed to {record.recipient}: {'sent' if result.ok else record.email_error}.")
    db.session.commit()
    _flash_result(result, record)
    return back


@licences_bp.route("/clients/licences/<int:licence_id>/resend", methods=["POST"])
@role_required(*CLIENT_MANAGEMENT_ROLES)
def resend(licence_id):
    record = db.get_or_404(DesktopLicence, licence_id)
    result = deliver(record, _contact_name(record))
    record_audit("Desktop licence resent", record.access_request or record,
                 f"{record.license_id} to {record.recipient}: "
                 f"{'sent' if result.ok else record.email_error}.")
    db.session.commit()
    _flash_result(result, record)
    return redirect(url_for("licences.ledger"))


@licences_bp.route("/clients/licences")
@role_required(*CLIENT_MANAGEMENT_ROLES)
def ledger():
    page = paginate(DesktopLicence.query.order_by(DesktopLicence.issued_at.desc()))
    return render_template("desktop_licences.html", page=page, enabled=licences_enabled(),
                           today=date.today())
