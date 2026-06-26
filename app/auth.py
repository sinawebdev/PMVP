from functools import wraps

from flask import Blueprint, flash, redirect, render_template, request, session, url_for
from flask_login import current_user, login_required, login_user, logout_user

from app.models import User

auth_bp = Blueprint("auth", __name__)


def role_required(*roles):
    def decorator(view):
        @wraps(view)
        @login_required
        def wrapped(*args, **kwargs):
            if current_user.role == "client_user" and "client_user" not in roles:
                flash("Client portal access is archived while the payroll MVP is stabilized.", "warning")
                return redirect(url_for("main.dashboard"))
            if str(current_user.role).lower() == "md":
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
    if current_user.is_authenticated:
        return redirect(url_for("main.dashboard"))

    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        user = User.query.filter_by(email=email).first()
        if user and user.check_password(password):
            session.clear()
            login_user(user)
            return redirect(url_for("main.dashboard"))
        flash("Invalid email or password.", "danger")

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
