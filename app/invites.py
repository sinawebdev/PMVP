"""Client admin invites: the step that turns an onboarded company into one that
can sign in.

The operator invites the company's contact from the onboarding page. That
creates the ``User`` straight away, with a random unusable password, and emails
a link to choose a real one. Creating the user at invite time is what clears the
derived ``ClientCompany.awaiting_credentials`` flag; ``User.invite_pending``
then says whether the invite has been accepted.

The link is a signed, timestamped token (itsdangerous, already a Flask
dependency) carrying the user id and salted with the user's current password
hash. Choosing a password changes the hash, so a link works exactly once, and
a resend invalidates every earlier link the same way.
"""

import secrets
from datetime import datetime, timezone
from html import escape

from flask import Blueprint, current_app, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_user
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from app import db
from app.audit import record_audit
from app.auth import role_required
from app.distribution.channels import OutboundMessage, get_sender, is_valid_email
from app.models import ClientCompany, User
from app.permissions import CLIENT_MANAGEMENT_ROLES
from app.roles import CLIENT_ADMIN

invites_bp = Blueprint("invites", __name__)

INVITE_MAX_AGE = 72 * 3600
MIN_PASSWORD_LENGTH = 10


def _serializer():
    return URLSafeTimedSerializer(current_app.config["SECRET_KEY"], salt="client-admin-invite")


def make_token(user):
    # The hash fragment ties the token to the password the user has right now.
    return _serializer().dumps({"u": user.id, "h": user.password_hash[-16:]})


def load_user_from_token(token):
    """The invited User for ``token``, or None if it is invalid, used or expired."""
    try:
        data = _serializer().loads(token, max_age=INVITE_MAX_AGE)
    except (SignatureExpired, BadSignature):
        return None
    if not isinstance(data, dict):
        return None
    user = db.session.get(User, data.get("u"))
    if user is None or user.password_hash[-16:] != data.get("h"):
        return None
    return user


def invite_url(user):
    path = url_for("invites.accept", token=make_token(user))
    base = (current_app.config.get("PUBLIC_BASE_URL") or request.url_root).rstrip("/")
    return base + path


def send_invite(user, company):
    """Rotate the user's placeholder password, then email a fresh link.

    Rotating first is what makes a resend cancel the previous link. Returns the
    SendResult so the caller can tell the operator whether the email went out."""
    user.set_password(secrets.token_urlsafe(32))
    user.invited_at = datetime.now(timezone.utc)
    link = invite_url(user)
    app_name = current_app.config.get("APP_BRAND_NAME") or current_app.config.get("APP_NAME", "Payrolla")
    hours = INVITE_MAX_AGE // 3600
    text = (
        f"Hello {user.name},\n\n"
        f"{company.name} now has a {app_name} account, and you are its administrator.\n\n"
        f"Choose your password here (the link works once and expires in {hours} hours):\n{link}\n\n"
        f"After that, sign in with {user.email}.\n"
    )
    html = (
        f"<p>Hello {escape(user.name)},</p>"
        f"<p>{escape(company.name)} now has a {escape(app_name)} account, and you are its administrator.</p>"
        f'<p><a href="{escape(link)}">Choose your password</a></p>'
        f"<p>The link works once and expires in {hours} hours. After that, sign in with {escape(user.email)}.</p>"
    )
    return get_sender("email").send(OutboundMessage(
        channel="email",
        recipient=user.email,
        subject=f"Your {app_name} account for {company.name}",
        body_text=text,
        body_html=html,
    ))


def validate_invitee(name, email, existing=None):
    errors = {}
    if not name:
        errors["invite_name"] = "Enter the person's name."
    elif len(name) > 120:
        errors["invite_name"] = "Keep the name under 120 characters."
    if not is_valid_email(email):
        errors["invite_email"] = "Enter a valid email address."
    else:
        clash = User.query.filter(db.func.lower(User.email) == email).first()
        if clash is not None and clash is not existing:
            errors["invite_email"] = "Someone already signs in with this email."
    return errors


@invites_bp.route("/clients/<int:client_id>/invite", methods=["POST"])
@role_required(*CLIENT_MANAGEMENT_ROLES)
def invite_admin(client_id):
    company = db.get_or_404(ClientCompany, client_id)
    name = (request.form.get("invite_name") or "").strip()
    email = (request.form.get("invite_email") or "").strip().lower()
    errors = validate_invitee(name, email)
    if errors:
        from app.routes import render_onboarding

        return render_onboarding(company, invite_errors=errors,
                                 invite_form={"name": name, "email": email}), 400

    user = User(name=name, email=email, role=CLIENT_ADMIN, client_company_id=company.id,
                password_hash="!")
    db.session.add(user)
    db.session.flush()
    result = send_invite(user, company)
    record_audit("Client admin invited", company, f"{name} <{email}> invited as client admin.")
    db.session.commit()
    _flash_send_result(result, email)
    return redirect(url_for("main.client_onboarding", client_id=company.id))


@invites_bp.route("/clients/<int:client_id>/invite/<int:user_id>/resend", methods=["POST"])
@role_required(*CLIENT_MANAGEMENT_ROLES)
def resend_invite(client_id, user_id):
    company = db.get_or_404(ClientCompany, client_id)
    user = db.get_or_404(User, user_id)
    if user.client_company_id != company.id or not user.invite_pending:
        flash("That invite has already been accepted.", "info")
        return redirect(url_for("main.client_onboarding", client_id=company.id))
    result = send_invite(user, company)
    record_audit("Client admin invite resent", company, f"Invite resent to {user.email}.")
    db.session.commit()
    _flash_send_result(result, user.email)
    return redirect(url_for("main.client_onboarding", client_id=company.id))


def _flash_send_result(result, email):
    if result.ok:
        flash(f"Invite sent to {email}.", "success")
    else:
        flash(f"The login was created, but the invite email to {email} did not send "
              f"({result.error}). Use Resend once email is working.", "warning")


@invites_bp.route("/invite/<token>", methods=["GET", "POST"])
def accept(token):
    user = load_user_from_token(token)
    if user is None:
        return render_template("invite_accept.html", invalid=True), 410
    error = None
    if request.method == "POST":
        password = request.form.get("password", "")
        confirm = request.form.get("confirm", "")
        if len(password) < MIN_PASSWORD_LENGTH:
            error = f"Use at least {MIN_PASSWORD_LENGTH} characters."
        elif password != confirm:
            error = "The two passwords don't match."
        else:
            user.set_password(password)
            user.activated_at = datetime.now(timezone.utc)
            record_audit("Client admin invite accepted", user.client_company, f"{user.email} set a password.")
            db.session.commit()
            if current_user.is_authenticated:
                from flask_login import logout_user
                logout_user()
            login_user(user)
            flash("Your password is set. Welcome.", "success")
            from app.tenancy import landing_endpoint
            return redirect(url_for(landing_endpoint()))
    return render_template("invite_accept.html", invalid=False, invitee=user,
                           error=error, min_length=MIN_PASSWORD_LENGTH), (400 if error else 200)
