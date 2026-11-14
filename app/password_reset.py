"""Forgot password: an emailed link that works once and expires in an hour.

The forgot-password page used to say "ask your administrator", but nothing in
the app could reset a password, and most companies have exactly one login.

How it holds together:

- The link is the invite link's design (app.invites) under its own salt: a
  signed, timestamped token carrying the user id and the tail of the current
  password hash. Setting a new password changes the hash, so a link works once
  and using any link cancels every other one. Unlike an invite, asking for a
  link does NOT rotate the password: that would let anyone lock a person out
  just by typing their address here.
- The page answers the same way whether or not an account exists, so it can't
  be used to find out who has one. The email goes out on a background thread
  for the same reason: a slow mail relay would otherwise time the answer.
- Requests are capped per address and per IP. The Gmail relay sends at most 100
  emails a day, and nobody should be able to spend that on someone's inbox.
  The counters live in this process, with the caveat app.login_throttle states.
- Staff can send the same link from a company's onboarding page when someone
  phones in. Nobody ever sees or types another person's password.
"""
import threading
import time as _time
from datetime import datetime, timezone
from html import escape

from flask import Blueprint, abort, current_app, flash, redirect, render_template, request, url_for
from flask_login import current_user, logout_user
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from sqlalchemy import update as sa_update
from werkzeug.security import generate_password_hash

from app import db, login_throttle
from app.audit import record_audit
from app.auth import role_required
from app.distribution.channels import OutboundMessage, get_sender, is_valid_email
from app.invites import MIN_PASSWORD_LENGTH
from app.models import ClientCompany, User
from app.permissions import CLIENT_MANAGEMENT_ROLES

password_reset_bp = Blueprint("password_reset", __name__)

RESET_MAX_AGE = 3600


# --- Links ---------------------------------------------------------------------
def _serializer():
    return URLSafeTimedSerializer(current_app.config["SECRET_KEY"], salt="password-reset")


def make_token(user):
    return _serializer().dumps({"u": user.id, "h": user.password_hash[-16:]})


def load_user_from_token(token):
    """The User for ``token``, or None if it is invalid, used or expired."""
    try:
        data = _serializer().loads(token, max_age=RESET_MAX_AGE)
    except (SignatureExpired, BadSignature):
        return None
    if not isinstance(data, dict):
        return None
    user = db.session.get(User, data.get("u"))
    if user is None or user.password_hash[-16:] != data.get("h"):
        return None
    return user


def reset_message(user):
    """The reset email for ``user``, with a fresh link."""
    path = url_for("password_reset.choose", token=make_token(user))
    base = (current_app.config.get("PUBLIC_BASE_URL") or request.url_root).rstrip("/")
    link = base + path
    app_name = current_app.config.get("APP_BRAND_NAME") or current_app.config.get("APP_NAME", "Payrolla")
    text = (
        f"Hello {user.name},\n\n"
        f"Someone asked to reset the password for your {app_name} sign-in, {user.email}.\n\n"
        f"Choose a new password here (the link works once and expires in 1 hour):\n{link}\n\n"
        f"If it wasn't you, ignore this email. Your password stays as it is.\n"
    )
    html = (
        f"<p>Hello {escape(user.name)},</p>"
        f"<p>Someone asked to reset the password for your {escape(app_name)} sign-in, "
        f"{escape(user.email)}.</p>"
        f'<p><a href="{escape(link)}">Choose a new password</a></p>'
        f"<p>The link works once and expires in 1 hour. If it wasn't you, ignore this email. "
        f"Your password stays as it is.</p>"
    )
    return OutboundMessage(channel="email", recipient=user.email,
                           subject=f"Reset your {app_name} password",
                           body_text=text, body_html=html)


def dispatch(message):
    """Send ``message`` on a background thread so the page answers in the same
    time whether or not an email went out. Returns the thread (tests join it)."""
    app = current_app._get_current_object()

    def _send():
        with app.app_context():
            result = get_sender("email").send(message)
            if not result.ok:
                app.logger.warning("[password-reset] email did not send: %s", result.error)

    thread = threading.Thread(target=_send, name="password-reset-email", daemon=True)
    thread.start()
    return thread


# --- Request caps --------------------------------------------------------------
_lock = threading.Lock()
_hits = {}  # key -> [monotonic timestamps]
_MAX_TRACKED = 10000


def _allow(key, limit, now=None):
    """Record a request for ``key``; False once it has used ``limit`` this hour."""
    now = _time.monotonic() if now is None else now
    with _lock:
        recent = [t for t in _hits.get(key, []) if now - t < 3600]
        if len(recent) >= limit:
            _hits[key] = recent
            return False
        if len(_hits) >= _MAX_TRACKED and key not in _hits:
            for stale in [k for k, ts in _hits.items() if not any(now - t < 3600 for t in ts)]:
                del _hits[stale]
            if len(_hits) >= _MAX_TRACKED:
                return False
        recent.append(now)
        _hits[key] = recent
        return True


def reset():
    """Test seam: forget every recorded request."""
    with _lock:
        _hits.clear()


def _within_caps(email):
    from app.auth import _request_fingerprint
    from app.client_ip import rate_limit_key

    ip, _agent = _request_fingerprint()
    per_ip = int(current_app.config.get("PASSWORD_RESET_MAX_PER_IP_PER_HOUR", 10))
    per_address = int(current_app.config.get("PASSWORD_RESET_MAX_PER_HOUR", 3))
    # Both counters move on every request, so neither can be probed separately.
    ip_ok = _allow(f"ip:{rate_limit_key(ip)}", per_ip)
    address_ok = _allow(f"account:{email}", per_address)
    return ip_ok and address_ok, ip


# --- Pages ---------------------------------------------------------------------
@password_reset_bp.route("/forgot-password", methods=["GET", "POST"])
def forgot():
    if request.method == "GET":
        return render_template("forgot_password.html", sent=False, email="", error=None)
    email = (request.form.get("email") or "").strip().lower()
    if not is_valid_email(email):
        return render_template("forgot_password.html", sent=False, email=email,
                               error="Enter the email address you sign in with."), 400
    allowed, ip = _within_caps(email)
    user = User.query.filter(db.func.lower(User.email) == email).first() if allowed else None
    if user is not None:
        message = reset_message(user)
        record_audit("Password reset link sent", user, f"Requested from the sign-in page. ip={ip}")
        db.session.commit()
        dispatch(message)
    # The same answer whatever happened above.
    return render_template("forgot_password.html", sent=True, email=email, error=None)


@password_reset_bp.route("/reset-password/<token>", methods=["GET", "POST"])
def choose(token):
    user = load_user_from_token(token)
    if user is None:
        return render_template("reset_password.html", invalid=True), 410
    error = None
    if request.method == "POST":
        password = request.form.get("password", "")
        confirm = request.form.get("confirm", "")
        if len(password) < MIN_PASSWORD_LENGTH:
            error = f"Use at least {MIN_PASSWORD_LENGTH} characters."
        elif password != confirm:
            error = "The two passwords don't match."
        else:
            # Consume the link atomically, as an invite does: the UPDATE only
            # matches while the hash is still the one the link was issued for.
            # A login that never accepted its invite is accepted by this too.
            claimed = db.session.execute(
                sa_update(User)
                .where(User.id == user.id, User.password_hash == user.password_hash)
                .values(password_hash=generate_password_hash(password),
                        activated_at=db.func.coalesce(User.activated_at, datetime.now(timezone.utc)))
            ).rowcount
            if claimed != 1:
                db.session.rollback()
                return render_template("reset_password.html", invalid=True), 410
            db.session.refresh(user)
            record_audit("Password reset", user, f"{user.email} chose a new password from an emailed link.")
            db.session.commit()
            # A forgotten password usually comes with a locked-out sign-in.
            login_throttle.clear([login_throttle.account_key(user.email)])
            if current_user.is_authenticated:
                logout_user()
            flash("Your password has been changed. Sign in with the new one.", "success")
            return redirect(url_for("auth.login"))
    return render_template("reset_password.html", invalid=False, person=user, error=error,
                           min_length=MIN_PASSWORD_LENGTH), (400 if error else 200)


@password_reset_bp.route("/clients/<int:client_id>/logins/<int:user_id>/send-reset", methods=["POST"])
@role_required(*CLIENT_MANAGEMENT_ROLES)
def admin_send(client_id, user_id):
    """Staff send the reset link for someone who phoned in."""
    from app.bureau_scope import is_payrolla_staff

    company = db.get_or_404(ClientCompany, client_id)
    user = db.get_or_404(User, user_id)
    belongs = user.client_company_id == company.id or (
        # A bureau's home company: its people are the bureau's staff, whom only
        # Payrolla staff can reach (app.bureau_scope keeps bureaus to client logins).
        company.bureau_home and company.bureau_id is not None
        and user.bureau_id == company.bureau_id and is_payrolla_staff()
    )
    if not belongs:
        abort(404)
    back = redirect(url_for("main.client_onboarding", client_id=company.id))
    if user.invite_pending:
        flash(f"{user.email} hasn't accepted the invite yet. Use Resend instead.", "info")
        return back
    result = get_sender("email").send(reset_message(user))
    sender = getattr(current_user, "email", "staff")
    record_audit("Password reset link sent", user, f"Sent by {sender} from the onboarding page.")
    db.session.commit()
    if result.ok:
        flash(f"Password reset link sent to {user.email}. It works for 1 hour.", "success")
    else:
        flash(f"The reset email to {user.email} did not send ({result.error}).", "warning")
    return back
