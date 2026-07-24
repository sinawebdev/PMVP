from functools import wraps

from flask import Blueprint, flash, redirect, render_template, request, session, url_for
from flask_login import current_user, login_required, login_user, logout_user

from app.models import User
from app.roles import CHRISNAT_ADMIN

auth_bp = Blueprint("auth", __name__)

# Operator superusers: any role_required check passes for these. ``md`` is the
# legacy bureau superuser; ``chrisnat_admin`` is the SaaS-era platform admin,
# granted full operator access (confirmed with Sina).
OPERATOR_SUPERUSERS = ("md", CHRISNAT_ADMIN)


def role_required(*roles):
    def decorator(view):
        @wraps(view)
        @login_required
        def wrapped(*args, **kwargs):
            # Tenant (client) users never belong on an operator/oversight route —
            # send them to their own scoped Company Dashboard, not the (now
            # platform-only) operator dashboard.
            if getattr(current_user, "client_company_id", None) is not None:
                flash("That area is limited to your company dashboard.", "warning")
                return redirect(url_for("main.company_dashboard"))
            if current_user.role == "client_user" and "client_user" not in roles:
                flash("Client portal access is archived while Payrolla is stabilized.", "warning")
                return redirect(url_for("main.dashboard"))
            if str(current_user.role).lower() in OPERATOR_SUPERUSERS:
                return view(*args, **kwargs)
            has_direct_role = current_user.role in roles
            if not has_direct_role:
                flash("You do not have permission to access that page.", "warning")
                return redirect(url_for("main.dashboard"))
            return view(*args, **kwargs)

        return wrapped

    return decorator


@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    from app.tenancy import landing_endpoint

    if current_user.is_authenticated:
        return redirect(url_for(landing_endpoint()))

    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        user = User.query.filter_by(email=email).first()
        if user and user.check_password(password):
            session.clear()
            login_user(user)
            # Resolve the landing plane AFTER login so current_user is the new user:
            # tenant users -> Company Dashboard, platform users -> oversight console.
            return redirect(url_for(landing_endpoint()))
        # Re-render with the submitted email preserved (never the password) and an
        # inline error, instead of wiping the form. Not a flash — the message is
        # bound to the fields it concerns.
        return render_template(
            "login.html",
            email=email,
            login_error="Invalid email or password.",
        )

    return render_template("login.html")


@auth_bp.route("/logout")
@login_required
def logout():
    logout_user()
    flash("You have been logged out.", "info")
    return redirect(url_for("auth.login"))


@auth_bp.route("/forgot-password")
def forgot_password():
    return render_template("forgot_password.html")
