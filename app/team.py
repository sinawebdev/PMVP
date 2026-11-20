"""The Team page: an admin decides who else at their firm can sign in.

Before this, every company and every bureau had exactly one login, the person
Payrolla invited. Now that person (any admin, in fact) can:

* invite a colleague, who gets the same set-a-password email as the first
  invite (app.invites), saying who added them and as what;
* resend an invite that hasn't been accepted yet;
* change a colleague between the two roles of their plane;
* remove a colleague, and restore one removed by mistake.

There are two Team pages because there are two planes. A company admin
(client_admin) manages the company's logins inside the client portal; a bureau
admin manages the bureau's staff inside the operator interface. A company that
runs its own payroll is set up as a bureau of the same name (app.bureaus), so
its people use the bureau page. The rules are the same on both and live once,
in the functions below; the routes only pick the plane.

Removing never deletes a row. Payroll runs, expenses and the audit trail point
at the people who did the work, so a removed login keeps its row with
``disabled_at`` set: it can't sign in, an open session ends on its next request
(app.load_user) and an unaccepted invite link stops working (app.invites).

Nobody can change or remove their own login. That rule alone guarantees a team
always keeps an admin: whoever is acting is an active admin, and they can only
act on someone else.
"""
from collections import namedtuple
from datetime import datetime, timezone

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from flask_login import current_user

from app import db
from app.audit import record_audit
from app.auth import role_required
from app.models import User
from app.paging import paginate
from app.permissions import COMPANY_TEAM_ROLES, TEAM_ROLES
from app.roles import (
    BUREAU_ADMIN,
    BUREAU_OFFICER,
    CLIENT_ADMIN,
    CLIENT_PREPARER,
    is_bureau_user,
    normalise_role,
)
from app.tenancy import active_tenant_id, tenant_role_required

team_bp = Blueprint("team", __name__)

RoleChoice = namedtuple("RoleChoice", "value label summary")

# Admin first: it is the role the first login already has, and the one a reader
# needs to understand before the narrower one makes sense.
BUREAU_ROLE_CHOICES = (
    RoleChoice(BUREAU_ADMIN, "Administrator",
               "Everything: uploads, calculates and approves payroll, and manages clients, "
               "rates and this team."),
    RoleChoice(BUREAU_OFFICER, "Officer",
               "Keeps employee lists, corrects run figures and sends payslips. Can't upload, "
               "calculate or approve payroll."),
)
COMPANY_ROLE_CHOICES = (
    RoleChoice(CLIENT_ADMIN, "Administrator",
               "Everything, including sending payslips, email branding and this team."),
    RoleChoice(CLIENT_PREPARER, "Preparer",
               "Keeps the employee list, uploads payroll and records expenses. Can't send "
               "payslips or change settings."),
)


class Team:
    """One team: a company's logins or a bureau's staff."""

    def __init__(self, kind, org):
        self.kind = kind  # "company" or "bureau"
        self.org = org
        self.choices = BUREAU_ROLE_CHOICES if kind == "bureau" else COMPANY_ROLE_CHOICES
        self.admin_role = self.choices[0].value

    @property
    def name(self):
        return self.org.name

    def query(self):
        if self.kind == "bureau":
            return User.query.filter(User.bureau_id == self.org.id)
        return User.query.filter(User.client_company_id == self.org.id)

    def owns(self, user):
        if user is None:
            return False
        if self.kind == "bureau":
            return user.bureau_id == self.org.id
        return user.client_company_id == self.org.id

    def role_label(self, role):
        role = normalise_role(role)
        return next((c.label for c in self.choices if c.value == role), role)

    def valid_role(self, role):
        role = normalise_role(role)
        return role if any(c.value == role for c in self.choices) else None

    def new_user(self, name, email, role):
        user = User(name=name, email=email, role=role, password_hash="!")
        if self.kind == "bureau":
            user.bureau_id = self.org.id
        else:
            user.client_company_id = self.org.id
        return user


def _now():
    return datetime.now(timezone.utc)


def members_page(team):
    """Active people first, then pending invites, then removed logins; by name."""
    order = db.case(
        (User.disabled_at.isnot(None), 2),
        (db.and_(User.activated_at.is_(None), User.invited_at.isnot(None)), 1),
        else_=0,
    )
    return paginate(team.query().order_by(order, db.func.lower(User.name)), per_page=50)


def invite(team, name, email, role):
    """Create the colleague's login and email the invite. Returns (errors, result)."""
    from app.invites import send_invite, validate_invitee

    errors = validate_invitee(name, email)
    if team.valid_role(role) is None:
        errors["invite_role"] = "Choose a role."
    if errors:
        return errors, None
    role = team.valid_role(role)
    user = team.new_user(name, email, role)
    db.session.add(user)
    db.session.flush()
    result = send_invite(user, team.org, invited_by=current_user.name,
                         role_label=_with_article(team.role_label(role)))
    record_audit("Team member invited", team.org,
                 f"{name} <{email}> invited as {team.role_label(role)} by {current_user.email}.")
    db.session.commit()
    return {}, result


def _with_article(label):
    label = label.lower()
    return ("an " if label[:1] in "aeiou" else "a ") + label


def _guard_other(team, member):
    """The reason this member can't be acted on, or None."""
    if not team.owns(member):
        abort(404)
    if member.id == current_user.id:
        return "You can't change or remove your own login. Ask another administrator."
    return None


def resend(team, member):
    from app.invites import send_invite

    problem = _guard_other(team, member)
    if problem:
        return problem, None
    if member.disabled_at is not None:
        return f"{member.name} was removed from the team. Restore them first.", None
    if not member.invite_pending:
        return f"{member.name} has already accepted their invite.", None
    result = send_invite(member, team.org, invited_by=current_user.name,
                         role_label=_with_article(team.role_label(member.role)))
    record_audit("Team invite resent", team.org, f"Invite resent to {member.email} by {current_user.email}.")
    db.session.commit()
    return None, result


def change_role(team, member, role):
    problem = _guard_other(team, member)
    if problem:
        return problem
    new_role = team.valid_role(role)
    if new_role is None:
        return "Choose a role."
    old_role = normalise_role(member.role)
    if new_role == old_role:
        return None
    member.role = new_role
    record_audit("Team member role changed", team.org,
                 f"{member.email}: {team.role_label(old_role)} to {team.role_label(new_role)}, "
                 f"by {current_user.email}.")
    db.session.commit()
    return None


def remove(team, member):
    problem = _guard_other(team, member)
    if problem:
        return problem
    if member.disabled_at is not None:
        return None
    member.disabled_at = _now()
    record_audit("Team member removed", team.org,
                 f"{member.email} ({team.role_label(member.role)}) removed by {current_user.email}.")
    db.session.commit()
    return None


def restore(team, member):
    problem = _guard_other(team, member)
    if problem:
        return problem
    if member.disabled_at is None:
        return None
    member.disabled_at = None
    record_audit("Team member restored", team.org,
                 f"{member.email} ({team.role_label(member.role)}) restored by {current_user.email}.")
    db.session.commit()
    return None


def _flash_invite_result(result, email):
    from app.invites import _flash_send_result

    _flash_send_result(result, email)


# --- Bureau staff (operator plane) -------------------------------------------


def bureau_team_required(view):
    """404 for anyone who isn't on a bureau's staff: the superusers pass the role
    guard (every operator group carries them) but Payrolla has no team here."""
    from functools import wraps

    @wraps(view)
    def wrapped(*args, **kwargs):
        if not is_bureau_user(current_user) or current_user.bureau is None:
            abort(404)
        return view(*args, **kwargs)

    return wrapped


def _bureau_team():
    return Team("bureau", current_user.bureau)


def _member(team, member_id):
    member = db.session.get(User, member_id)
    if not team.owns(member):
        abort(404)
    return member


def _render_bureau(team, errors=None, form=None, status=200):
    return render_template(
        "team.html", team=team, page=members_page(team),
        invite_errors=errors or {}, invite_form=form or {"role": BUREAU_OFFICER},
    ), status


@team_bp.route("/team")
@role_required(*TEAM_ROLES)
@bureau_team_required
def bureau_team():
    return _render_bureau(_bureau_team())


@team_bp.route("/team/invite", methods=["POST"])
@role_required(*TEAM_ROLES)
@bureau_team_required
def bureau_invite():
    team = _bureau_team()
    form = _invite_form()
    errors, result = invite(team, form["name"], form["email"], form["role"])
    if errors:
        return _render_bureau(team, errors, form, 400)
    _flash_invite_result(result, form["email"])
    return redirect(url_for("team.bureau_team"))


@team_bp.route("/team/<int:member_id>/resend", methods=["POST"])
@role_required(*TEAM_ROLES)
@bureau_team_required
def bureau_resend(member_id):
    team = _bureau_team()
    member = _member(team, member_id)
    problem, result = resend(team, member)
    _after(problem, result, member)
    return redirect(url_for("team.bureau_team"))


@team_bp.route("/team/<int:member_id>/role", methods=["POST"])
@role_required(*TEAM_ROLES)
@bureau_team_required
def bureau_change_role(member_id):
    team = _bureau_team()
    member = _member(team, member_id)
    _done(change_role(team, member, request.form.get("role")),
          f"{member.name} is now {_with_article(team.role_label(member.role))}.")
    return redirect(url_for("team.bureau_team"))


@team_bp.route("/team/<int:member_id>/remove", methods=["POST"])
@role_required(*TEAM_ROLES)
@bureau_team_required
def bureau_remove(member_id):
    team = _bureau_team()
    member = _member(team, member_id)
    _done(remove(team, member), f"{member.name} can no longer sign in.")
    return redirect(url_for("team.bureau_team"))


@team_bp.route("/team/<int:member_id>/restore", methods=["POST"])
@role_required(*TEAM_ROLES)
@bureau_team_required
def bureau_restore(member_id):
    team = _bureau_team()
    member = _member(team, member_id)
    _done(restore(team, member), f"{member.name} can sign in again.")
    return redirect(url_for("team.bureau_team"))


# --- Company logins (client portal) ------------------------------------------


def _company_team():
    from app.models import ClientCompany

    return Team("company", db.session.get(ClientCompany, active_tenant_id()))


def _render_company(team, errors=None, form=None, status=200):
    return render_template(
        "client/team.html", company=team.org, team=team, page=members_page(team),
        invite_errors=errors or {}, invite_form=form or {"role": CLIENT_PREPARER},
    ), status


@team_bp.route("/company/team")
@tenant_role_required(*COMPANY_TEAM_ROLES)
def company_team():
    return _render_company(_company_team())


@team_bp.route("/company/team/invite", methods=["POST"])
@tenant_role_required(*COMPANY_TEAM_ROLES)
def company_invite():
    team = _company_team()
    form = _invite_form()
    errors, result = invite(team, form["name"], form["email"], form["role"])
    if errors:
        return _render_company(team, errors, form, 400)
    _flash_invite_result(result, form["email"])
    return redirect(url_for("team.company_team"))


@team_bp.route("/company/team/<int:member_id>/resend", methods=["POST"])
@tenant_role_required(*COMPANY_TEAM_ROLES)
def company_resend(member_id):
    team = _company_team()
    member = _member(team, member_id)
    problem, result = resend(team, member)
    _after(problem, result, member)
    return redirect(url_for("team.company_team"))


@team_bp.route("/company/team/<int:member_id>/role", methods=["POST"])
@tenant_role_required(*COMPANY_TEAM_ROLES)
def company_change_role(member_id):
    team = _company_team()
    member = _member(team, member_id)
    _done(change_role(team, member, request.form.get("role")),
          f"{member.name} is now {_with_article(team.role_label(member.role))}.")
    return redirect(url_for("team.company_team"))


@team_bp.route("/company/team/<int:member_id>/remove", methods=["POST"])
@tenant_role_required(*COMPANY_TEAM_ROLES)
def company_remove(member_id):
    team = _company_team()
    member = _member(team, member_id)
    _done(remove(team, member), f"{member.name} can no longer sign in.")
    return redirect(url_for("team.company_team"))


@team_bp.route("/company/team/<int:member_id>/restore", methods=["POST"])
@tenant_role_required(*COMPANY_TEAM_ROLES)
def company_restore(member_id):
    team = _company_team()
    member = _member(team, member_id)
    _done(restore(team, member), f"{member.name} can sign in again.")
    return redirect(url_for("team.company_team"))


# --- Shared request helpers ---------------------------------------------------


def _invite_form():
    return {
        "name": (request.form.get("invite_name") or "").strip(),
        "email": (request.form.get("invite_email") or "").strip().lower(),
        "role": (request.form.get("invite_role") or "").strip(),
    }


def _done(problem, success_message):
    if problem:
        flash(problem, "warning")
    else:
        flash(success_message, "success")


def _after(problem, result, member):
    if problem:
        flash(problem, "info")
    else:
        _flash_invite_result(result, member.email)
