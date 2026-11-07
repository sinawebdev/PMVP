"""Access requests: companies applying for Payrolla from the landing page.

Public side: ``POST /request-access`` validates the form, stores an
``AccessRequest`` and notifies the platform admins. It is the only public write
in the app, so it carries the defences a login form does: CSRF (global), a
honeypot field, and a per-IP submission cap kept in process memory (the same
single-process caveat ``app.login_throttle`` documents applies here).

Operator side: requests are listed on the Clients page. Approving one opens the
add-company form prefilled from it (``main.add_client?request=<id>``); saving
that form calls :func:`mark_approved`. Declining records a reason.
"""

import re
import threading
import time as _time
from datetime import date, datetime, timezone

from flask import Blueprint, current_app, flash, redirect, render_template, request, url_for
from flask_login import current_user

from app import db
from app.audit import record_audit
from app.client_ip import client_ip, rate_limit_key
from app.auth import role_required
from app.distribution.channels import is_valid_email
from app.models import (
    ACCESS_APPROVED,
    ACCESS_CONTACTED,
    ACCESS_DECLINED,
    ACCESS_NEW,
    ACCESS_OPEN_STATUSES,
    AccessRequest,
)
from app.permissions import CLIENT_MANAGEMENT_ROLES

access_bp = Blueprint("access", __name__)

EMPLOYEE_BANDS = ("1-10", "11-50", "51-200", "201-500", "500+")
INTERESTS = {
    "web": "Web workspace",
    "desktop": "Desktop app for Windows",
    "both": "Both",
}
HONEYPOT_FIELD = "website"

# --- Per-IP cap ---------------------------------------------------------------
_lock = threading.Lock()
_hits = {}  # ip -> [monotonic timestamps]
_MAX_TRACKED = 5000


def _limit():
    return int(current_app.config.get("ACCESS_REQUEST_MAX_PER_HOUR", 5))


def _allow(ip, now=None):
    """Record a submission from ``ip``; False once it has used its hourly cap."""
    now = _time.monotonic() if now is None else now
    with _lock:
        recent = [t for t in _hits.get(ip, []) if now - t < 3600]
        if len(recent) >= _limit():
            _hits[ip] = recent
            return False
        if len(_hits) >= _MAX_TRACKED and ip not in _hits:
            # Drop only addresses with nothing inside the window. Clearing the
            # whole dict here would let a flood of new addresses reset everyone.
            for key in [k for k, ts in _hits.items() if not any(now - t < 3600 for t in ts)]:
                del _hits[key]
            if len(_hits) >= _MAX_TRACKED:
                return False
        recent.append(now)
        _hits[ip] = recent
        return True


def reset():
    """Test seam: forget every recorded submission."""
    with _lock:
        _hits.clear()


# --- Validation ---------------------------------------------------------------
_PHONE_RE = re.compile(r"^[0-9+()\-\s]{7,20}$")
# Control characters (CR/LF included) have no business in a name and would
# travel on into email subjects and exports once the company is created.
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


def blank_form(interest="web"):
    return {
        "company_name": "", "contact_name": "", "email": "", "phone": "",
        "location": "", "employee_band": "",
        "interest": interest if interest in INTERESTS else "web", "message": "",
    }


def validate(form):
    """(values, errors) for a submitted request. Lengths match the columns."""
    values = {key: (form.get(key) or "").strip() for key in blank_form()}
    values["email"] = values["email"].lower()
    errors = {}
    for key in ("company_name", "contact_name", "email", "phone", "location"):
        if _CONTROL_RE.search(values[key]):
            errors[key] = "Remove line breaks and special characters."
    if errors:
        return values, errors
    if not values["company_name"]:
        errors["company_name"] = "Enter your company's name."
    elif len(values["company_name"]) > 160:
        errors["company_name"] = "Keep the company name under 160 characters."
    if not values["contact_name"]:
        errors["contact_name"] = "Enter your name."
    elif len(values["contact_name"]) > 120:
        errors["contact_name"] = "Keep your name under 120 characters."
    if not is_valid_email(values["email"]) or len(values["email"]) > 160:
        errors["email"] = "Enter an email address we can reply to."
    if values["phone"] and not _PHONE_RE.match(values["phone"]):
        errors["phone"] = "Enter a phone number, e.g. 024 123 4567."
    if len(values["location"]) > 120:
        errors["location"] = "Keep the location under 120 characters."
    if values["employee_band"] and values["employee_band"] not in EMPLOYEE_BANDS:
        errors["employee_band"] = "Choose a team size from the list."
    if values["interest"] not in INTERESTS:
        errors["interest"] = "Choose web, desktop or both."
    if len(values["message"]) > 2000:
        errors["message"] = "Keep the message under 2,000 characters."
    return values, errors


def render_landing(form=None, errors=None, status=200, sent=False):
    return render_template(
        "landing.html",
        desktop_download_url=current_app.config.get("DESKTOP_DOWNLOAD_URL"),
        request_form=form or blank_form(request.args.get("interest", "web")),
        request_errors=errors or {},
        request_sent=sent,
        employee_bands=EMPLOYEE_BANDS,
        interests=INTERESTS,
        trial_offer_ends=_trial_offer_label(),
    ), status


def _trial_offer_label():
    """"31 December 2026" while the free-trial offer runs; None afterwards."""
    from app.desktop_licences import long_date, trial_offer_ends

    ends = trial_offer_ends()
    return long_date(ends) if ends and ends >= date.today() else None


@access_bp.route("/request-access", methods=["POST"])
def submit():
    # A filled honeypot is a bot. Answer exactly as for a real submission so the
    # bot learns nothing, and store nothing.
    if request.form.get(HONEYPOT_FIELD):
        return redirect(url_for("main.index", sent=1, _anchor="request-access"))

    values, errors = validate(request.form)
    if errors:
        return render_landing(values, errors, status=400)

    if not _allow(rate_limit_key(client_ip())):
        return render_landing(values, {"form": "We've had several requests from this connection in the "
                                       "last hour. Please try again later, or email us."}, status=429)

    entry = AccessRequest(
        company_name=values["company_name"],
        contact_name=values["contact_name"],
        email=values["email"],
        phone=values["phone"] or None,
        location=values["location"] or None,
        employee_band=values["employee_band"] or None,
        interest=values["interest"],
        message=values["message"] or None,
        status=ACCESS_NEW,
    )
    db.session.add(entry)
    db.session.flush()
    from app.events import platform_admins, record_event

    record_event(
        "access.requested",
        summary=f"{entry.company_name} asked for access ({INTERESTS[entry.interest].lower()}).",
        subject=entry,
        recipients=platform_admins(),
    )
    record_audit("Access requested", entry, f"{entry.contact_name} <{entry.email}> for {entry.company_name}.")
    db.session.commit()
    return redirect(url_for("main.index", sent=1, _anchor="request-access"))


# --- Operator side ------------------------------------------------------------

def open_requests():
    return (
        AccessRequest.query.filter(AccessRequest.status.in_(ACCESS_OPEN_STATUSES))
        .order_by(AccessRequest.created_at.asc())
        .all()
    )


def open_request_count():
    return AccessRequest.query.filter(AccessRequest.status.in_(ACCESS_OPEN_STATUSES)).count()


def _review(entry, status):
    entry.status = status
    entry.reviewed_by_id = current_user.id
    entry.reviewed_at = datetime.now(timezone.utc)


def mark_approved(entry, company):
    """Link ``entry`` to the company created from it. Caller commits."""
    _review(entry, ACCESS_APPROVED)
    entry.client_company_id = company.id


@access_bp.route("/clients/requests/<int:request_id>")
@role_required(*CLIENT_MANAGEMENT_ROLES)
def detail(request_id):
    entry = db.get_or_404(AccessRequest, request_id)
    from app.desktop_licences import licences_enabled

    return render_template("access_request.html", entry=entry, interests=INTERESTS,
                           licence=entry.licences.first(), licences_enabled=licences_enabled())


@access_bp.route("/clients/requests/<int:request_id>/contacted", methods=["POST"])
@role_required(*CLIENT_MANAGEMENT_ROLES)
def contacted(request_id):
    entry = db.get_or_404(AccessRequest, request_id)
    if entry.is_open:
        _review(entry, ACCESS_CONTACTED)
        record_audit("Access request contacted", entry, f"{entry.company_name} marked as contacted.")
        db.session.commit()
        flash(f"{entry.company_name} marked as contacted.", "success")
    return redirect(url_for("access.detail", request_id=entry.id))


@access_bp.route("/clients/requests/<int:request_id>/decline", methods=["POST"])
@role_required(*CLIENT_MANAGEMENT_ROLES)
def decline(request_id):
    entry = db.get_or_404(AccessRequest, request_id)
    if not entry.is_open:
        flash("That request has already been decided.", "info")
        return redirect(url_for("access.detail", request_id=entry.id))
    reason = (request.form.get("reason") or "").strip()[:255]
    _review(entry, ACCESS_DECLINED)
    entry.decline_reason = reason or None
    record_audit("Access request declined", entry,
                 f"{entry.company_name} declined." + (f" Reason: {reason}" if reason else ""))
    db.session.commit()
    flash(f"Request from {entry.company_name} declined.", "success")
    return redirect(url_for("main.clients"))
