"""Setting up payroll bureaus: Payrolla's side of the bureau package.

Two ways a firm becomes a bureau, both Payrolla staff only:

* **From an access request** that answered "yes, we run payroll for other
  companies": the bureau is created and its contact is invited as the bureau
  admin. They then add their own clients from the operator interface.
* **From an existing company** (Chrisnat was onboarded as a single company before
  bureaus existed): the company becomes the bureau's first client, its own
  in-house payroll, and its logins become the bureau's staff (company admins
  become bureau admins, preparers become bureau officers). Their passwords are
  untouched; on their next page they see the bureau interface.

Neither is reachable by a bureau (app.bureau_scope keeps them off everything not
in BUREAU_ENDPOINTS) nor by anyone but Payrolla staff (payrolla_staff_required).
"""
import re

from flask import Blueprint, flash, redirect, url_for

from app import db
from app.audit import record_audit
from app.auth import role_required
from app.bureau_scope import payrolla_staff_required
from app.models import AccessRequest, Bureau, ClientCompany, User
from app.permissions import CLIENT_MANAGEMENT_ROLES
from app.roles import BUREAU_ADMIN, BUREAU_OFFICER, CLIENT_ADMIN, CLIENT_PREPARER, normalise_role

bureaus_bp = Blueprint("bureaus", __name__)

# A company login becomes the bureau role that does the same job.
ROLE_ON_CONVERSION = {CLIENT_ADMIN: BUREAU_ADMIN, CLIENT_PREPARER: BUREAU_OFFICER}


def unique_bureau_code(name, preferred=None):
    """A short unused code, e.g. "CHRISNAT" from "Chrisnat Limited"."""
    base = (preferred or "").strip().upper()
    if not base:
        words = [w for w in re.split(r"[^A-Za-z0-9]+", name or "") if w]
        base = (words[0] if words else "BUREAU").upper()[:16]
    base = re.sub(r"[^A-Z0-9-]", "", base)[:16] or "BUREAU"
    code, n = base, 2
    while Bureau.query.filter_by(code=code).first() is not None:
        code, n = f"{base[:16]}-{n}", n + 1
    return code


@bureaus_bp.route("/clients/requests/<int:request_id>/approve-bureau", methods=["POST"])
@role_required(*CLIENT_MANAGEMENT_ROLES)
@payrolla_staff_required
def approve_request(request_id):
    from app.access_requests import _review
    from app.invites import _flash_send_result, send_invite, validate_invitee
    from app.models import ACCESS_APPROVED

    entry = db.get_or_404(AccessRequest, request_id)
    back = redirect(url_for("access.detail", request_id=entry.id))
    if not entry.is_open:
        flash("That request has already been decided.", "info")
        return back
    if Bureau.query.filter(db.func.lower(Bureau.name) == entry.company_name.lower()).first():
        flash(f"A payroll bureau called {entry.company_name} already exists.", "warning")
        return back
    errors = validate_invitee(entry.contact_name, entry.email)
    if errors:
        flash(" ".join(errors.values()) + " Nothing was created.", "warning")
        return back

    bureau = Bureau(name=entry.company_name, code=unique_bureau_code(entry.company_name),
                    contact_email=entry.email, phone=entry.phone)
    db.session.add(bureau)
    db.session.flush()
    admin = User(name=entry.contact_name, email=entry.email, role=BUREAU_ADMIN,
                 bureau_id=bureau.id, password_hash="!")
    db.session.add(admin)
    db.session.flush()
    result = send_invite(admin, bureau)
    _review(entry, ACCESS_APPROVED)
    record_audit("Payroll bureau created", bureau,
                 f"{bureau.name} ({bureau.code}) from access request #{entry.id}; "
                 f"{admin.email} invited as bureau admin.")
    db.session.commit()
    _flash_send_result(result, admin.email)
    flash(f"{bureau.name} is set up as a payroll bureau. They add their own clients once "
          "they've signed in.", "success")
    return back


@bureaus_bp.route("/clients/<int:client_id>/make-bureau", methods=["POST"])
@role_required(*CLIENT_MANAGEMENT_ROLES)
@payrolla_staff_required
def convert_company(client_id):
    company = db.get_or_404(ClientCompany, client_id)
    back = redirect(url_for("main.client_detail", client_id=company.id))
    if company.bureau_id is not None:
        flash(f"{company.name} is already run by {company.bureau.name}.", "info")
        return back
    if Bureau.query.filter(db.func.lower(Bureau.name) == company.name.lower()).first():
        flash(f"A payroll bureau called {company.name} already exists.", "warning")
        return back

    bureau = Bureau(name=company.name, code=unique_bureau_code(company.name, company.company_code),
                    contact_email=company.email, phone=company.phone)
    db.session.add(bureau)
    db.session.flush()
    company.bureau_id = bureau.id
    company.bureau_home = True
    moved = []
    for user in User.query.filter_by(client_company_id=company.id).all():
        role = ROLE_ON_CONVERSION.get(normalise_role(user.role))
        if role is None:
            continue
        # Both columns in the one UPDATE: a login is never in two scopes at once.
        user.client_company_id = None
        user.bureau_id = bureau.id
        user.role = role
        moved.append(f"{user.email} ({role})")
    record_audit(
        "Company set up as payroll bureau", company,
        f"{company.name} is now the payroll bureau {bureau.name} ({bureau.code}) and its own "
        f"first client. Logins moved to the bureau: {', '.join(moved) or 'none'}.",
    )
    db.session.commit()
    flash(
        f"{company.name} is now a payroll bureau. "
        + (f"{len(moved)} login{'' if len(moved) == 1 else 's'} moved to the bureau; "
           "they'll see the bureau interface on their next page." if moved
           else "It had no logins to move."),
        "success",
    )
    return back
