"""The staff-only page that sets up, refreshes and removes the demo workspace.

The data side is app.demo_workspace; this module is the boundary: it validates
what staff type, sends the login invites, and reports what happened. Payrolla
staff only (payrolla_staff_required), and not in BUREAU_ENDPOINTS, so a bureau
login gets a 404 here like on every other Payrolla-only page.
"""

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from flask_login import current_user
from sqlalchemy.exc import IntegrityError

from app import db
from app.audit import record_audit
from app.auth import role_required
from app.bureau_scope import payrolla_staff_required
from app.demo_workspace import (
    LOGIN_ROLES,
    DemoGuardError,
    DemoStateError,
    demo_bureau,
    login_for_resend,
    refresh,
    remove,
    set_up,
    status,
)
from app.permissions import CLIENT_MANAGEMENT_ROLES
from app.roles import CLIENT_PREPARER
from app.tenant_purge import delete_stored_files

demo_bp = Blueprint("demo", __name__, url_prefix="/clients/demo")


def _send(user, org):
    """Invite one demo login. A preparer's email says what they join as, rather
    than telling them they are the company's administrator."""
    from app.invites import send_invite

    if user.role == CLIENT_PREPARER:
        return send_invite(user, org, invited_by=current_user.name, role_label="a payroll preparer")
    return send_invite(user, org)


def _render(form=None, errors=None, code=200):
    return render_template(
        "demo_workspace.html",
        state=status(),
        login_roles=LOGIN_ROLES,
        role_labels={role: label for _key, role, label in LOGIN_ROLES},
        form=form or {},
        errors=errors or {},
    ), code


def _read_logins(form):
    """{key: (name, email)} and {field: error} from the Set up form."""
    from app.invites import validate_invitee

    logins, errors, seen = {}, {}, {}
    for key, _role, _label in LOGIN_ROLES:
        name = (form.get(f"{key}_name") or "").strip()
        email = (form.get(f"{key}_email") or "").strip().lower()
        problems = validate_invitee(name, email)
        if "invite_name" in problems:
            errors[f"{key}_name"] = problems["invite_name"]
        if "invite_email" in problems:
            errors[f"{key}_email"] = problems["invite_email"]
        elif email in seen:
            errors[f"{key}_email"] = "Use a different address for each login."
        seen[email] = key
        logins[key] = (name, email)
    return logins, errors


@demo_bp.route("")
@role_required(*CLIENT_MANAGEMENT_ROLES)
@payrolla_staff_required
def index():
    return _render()


@demo_bp.route("/setup", methods=["POST"])
@role_required(*CLIENT_MANAGEMENT_ROLES)
@payrolla_staff_required
def setup():
    from app.invites import _flash_send_result

    form = {k: (request.form.get(k) or "").strip() for k in request.form if k != "csrf_token"}
    logins, errors = _read_logins(request.form)
    if errors:
        return _render(form, errors, 400)
    try:
        bureau, users = set_up(logins)
    except DemoStateError as exc:
        db.session.rollback()
        flash(str(exc), "warning")
        return redirect(url_for("demo.index"))
    except IntegrityError:
        # A double click: the other request created the same unique names first.
        db.session.rollback()
        flash("The demo workspace is already set up.", "info")
        return redirect(url_for("demo.index"))

    results = []
    for key, _role, _label in LOGIN_ROLES:
        user = users[key]
        org = bureau if user.bureau_id is not None else user.client_company
        results.append((_send(user, org), user.email))
    record_audit(
        "Demo workspace set up", bureau,
        "Demo bureau, 3 companies and sample payroll created; logins invited: "
        + ", ".join(f"{u.email} ({u.role})" for u in users.values()) + ".",
    )
    db.session.commit()
    for result, email in results:
        _flash_send_result(result, email)
    flash("The demo workspace is ready. Accept each invite in a private window, "
          "because accepting signs out whoever is signed in.", "success")
    return redirect(url_for("demo.index"))


@demo_bp.route("/refresh", methods=["POST"])
@role_required(*CLIENT_MANAGEMENT_ROLES)
@payrolla_staff_required
def refresh_workspace():
    try:
        stored_keys, _summary = refresh(current_user)
    except (DemoStateError, DemoGuardError) as exc:
        db.session.rollback()
        flash(str(exc), "warning")
        return redirect(url_for("demo.index"))
    delete_stored_files(stored_keys)
    bureau = demo_bureau()
    record_audit("Demo workspace refreshed", bureau,
                 "Demo data emptied and rebuilt; demo logins kept.")
    db.session.commit()
    flash("The demo data is rebuilt. The demo logins and their passwords are unchanged.", "success")
    return redirect(url_for("demo.index"))


@demo_bp.route("/remove", methods=["POST"])
@role_required(*CLIENT_MANAGEMENT_ROLES)
@payrolla_staff_required
def remove_workspace():
    if request.form.get("confirm") != "remove":
        flash("Tick the box to confirm. Nothing was removed.", "warning")
        return redirect(url_for("demo.index"))
    try:
        bureau = demo_bureau()
        bureau_ref = (bureau.name, bureau.id) if bureau is not None else None
        stored_keys = remove(current_user)
    except (DemoStateError, DemoGuardError) as exc:
        db.session.rollback()
        flash(str(exc), "warning")
        return redirect(url_for("demo.index"))
    delete_stored_files(stored_keys)
    record_audit("Demo workspace removed", None,
                 f"{bureau_ref[0]} (bureau #{bureau_ref[1]}), its companies, data and logins deleted.")
    db.session.commit()
    flash("The demo workspace and its logins are gone.", "success")
    return redirect(url_for("demo.index"))


@demo_bp.route("/logins/<int:login_id>/resend", methods=["POST"])
@role_required(*CLIENT_MANAGEMENT_ROLES)
@payrolla_staff_required
def resend(login_id):
    from app.invites import _flash_send_result

    try:
        user, org = login_for_resend(login_id)
    except DemoGuardError as exc:
        flash(str(exc), "warning")
        return redirect(url_for("demo.index"))
    if user is None:
        abort(404)
    if not user.invite_pending:
        flash(f"{user.email} has already chosen a password, so there is no invite to resend.", "info")
        return redirect(url_for("demo.index"))
    result = _send(user, org)
    record_audit("Demo login invite resent", org, f"Invite resent to {user.email}.")
    db.session.commit()
    _flash_send_result(result, user.email)
    return redirect(url_for("demo.index"))
