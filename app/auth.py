from functools import wraps

from flask import (
    Blueprint,
    current_app,
    flash,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from flask_login import current_user, login_required, login_user, logout_user
from werkzeug.security import check_password_hash, generate_password_hash

from app import db, login_throttle
from app.models import User
from app.roles import normalise_role

auth_bp = Blueprint("auth", __name__)

# A real hash of an unguessable value, computed once at import under the same
# algorithm and cost as every stored password (Werkzeug's current default, via
# the same generate_password_hash the User model uses).
#
# It exists to be verified against and fail. When the submitted email matches no
# user there is nothing to check, so the naive route returns immediately — and
# that shortcut is a user-enumeration oracle: a wrong password on a real account
# costs a full scrypt verification (~300 ms here), a wrong password on an unknown
# address costs ~4 ms. Anyone can read the difference over the network and
# enumerate the entire user list without a single successful login. Burning the
# same work on the miss path removes the signal.
_DUMMY_PASSWORD_HASH = generate_password_hash("payrolla-nonexistent-account-placeholder")


def _verify_credentials(user, password):
    """True when ``password`` authenticates ``user``.

    Always performs one password verification, whether or not ``user`` exists, so
    the two paths cost the same. Never short-circuits on ``user is None``.
    """
    if user is None:
        check_password_hash(_DUMMY_PASSWORD_HASH, password)
        return False
    return user.check_password(password)


def _record_audit(*args, **kwargs):
    """Local indirection for :func:`app.audit.record_audit`.

    Imported inside the call rather than at module scope because ``app.audit``
    imports ``role_required`` from this module — a module-level import here would
    close that cycle. Same lazy pattern ``login()`` uses for ``landing_endpoint``.
    """
    from app.audit import record_audit

    return record_audit(*args, **kwargs)


def _request_fingerprint():
    """(ip, user_agent) for the current request, for the audit trail.

    Prefers the left-most X-Forwarded-For hop because the app runs behind
    Render's proxy, where ``remote_addr`` is the proxy rather than the client.
    Both values are attacker-controlled headers and are recorded as claims, not
    facts — they are evidence for an investigator, never an access decision.
    """
    forwarded = (request.headers.get("X-Forwarded-For") or "").split(",")[0].strip()
    ip = forwarded or request.remote_addr or "unknown"
    agent = (request.headers.get("User-Agent") or "")[:200]
    return ip, agent

# Superuser access is NOT a special case here (Phase 1, Task 1.1). ``md`` and
# ``payrolla_admin`` used to short-circuit every check below, which meant a route
# could authorize them for an action whose button the template — reading
# app/permissions.py — never showed. Both are now members of every capability
# group in app.permissions instead, so one predicate answers for the route and
# the affordance alike. Effective access did not change; see
# tests/test_permission_parity.py. Gate new routes on a group from
# app.permissions, never a literal role tuple.


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
            # Compare canonical spellings on both sides: a user row (or a
            # decorator argument) still using a pre-rename alias resolves to the
            # same role, so the rename cannot cost anyone access.
            role = normalise_role(current_user.role)
            has_direct_role = role in {normalise_role(r) for r in roles}
            if not has_direct_role:
                flash("You do not have permission to access that page.", "warning")
                return redirect(url_for("main.dashboard"))
            return view(*args, **kwargs)

        # Publish the canonical role set this route accepts. The parity test
        # (tests/test_permission_parity.py) walks every view function and asserts
        # each guard is a named group from app.permissions that both superusers
        # belong to — the drift that Task 1.1 removed cannot come back silently.
        wrapped._required_roles = frozenset(normalise_role(r) for r in roles)
        return wrapped

    return decorator


def demo_login_hints():
    """Sign-in hints for the login page, grouped by plane.

    ``[{"group": ..., "rows": [{"email": ..., "label": ...}]}]``, derived from
    the seed roster itself (:mod:`app.seed`) so the hints can never drift from
    the accounts that actually exist. Empty — and the panel disappears entirely —
    unless ``SHOW_DEMO_LOGINS`` is on, which is impossible in production.
    """
    if not current_app.config.get("SHOW_DEMO_LOGINS"):
        return []
    from app.seed import (
        DEMO_COMPANIES,
        PLATFORM_USERS,
        TENANT_USER_TEMPLATE,
        company_domain,
    )

    groups = [
        {
            "group": "Payrolla platform",
            "rows": [
                {"email": email, "label": name} for name, email, _role in PLATFORM_USERS
            ],
        }
    ]
    for spec in DEMO_COMPANIES:
        domain = company_domain(spec)
        groups.append(
            {
                "group": f"{spec['name']} ({spec['company_code']})",
                "rows": [
                    {"email": f"{local}@{domain}", "label": title}
                    for local, title, _role in TENANT_USER_TEMPLATE
                ],
            }
        )
    return groups


def demo_login_password():
    from app.seed import DEMO_PASSWORD

    return DEMO_PASSWORD if current_app.config.get("SHOW_DEMO_LOGINS") else ""


@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    from app.tenancy import landing_endpoint

    if current_user.is_authenticated:
        return redirect(url_for(landing_endpoint()))

    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        ip, agent = _request_fingerprint()
        keys = [login_throttle.ip_key(ip), login_throttle.account_key(email)]

        def _render(error):
            # Re-render with the submitted email preserved (never the password) and
            # an inline error, instead of wiping the form. Not a flash — the message
            # is bound to the fields it concerns.
            return render_template(
                "login.html",
                email=email,
                login_error=error,
                demo_logins=demo_login_hints(),
                demo_password=demo_login_password(),
            )

        # Checked before the user lookup, and phrased identically either way, so a
        # locked response reveals nothing about whether the address is real.
        locked_for = login_throttle.retry_after(keys)
        if locked_for > 0:
            minutes = max(1, int(locked_for // 60) + (1 if locked_for % 60 else 0))
            _record_audit(
                "login.blocked",
                notes=f"email={email or '(blank)'} ip={ip} agent={agent} "
                      f"reason=rate-limited retry_after_s={int(locked_for)}",
            )
            db.session.commit()
            return _render(
                f"Too many sign-in attempts. Try again in about {minutes} minute(s)."
            ), 429

        user = User.query.filter_by(email=email).first()
        if _verify_credentials(user, password):
            session.clear()
            login_user(user)
            login_throttle.clear(keys)
            # After login_user so record_audit attributes the row to the new user.
            _record_audit("login.success", related_record=user,
                         notes=f"email={email} ip={ip} agent={agent}")
            db.session.commit()
            # Resolve the landing plane AFTER login so current_user is the new user:
            # tenant users -> Company Dashboard, platform users -> oversight console.
            return redirect(url_for(landing_endpoint()))

        now_locked = login_throttle.register_failure(keys)
        # `known` records whether the address exists — useful to an investigator
        # reading the trail, and safe here because the audit row is never shown to
        # the person signing in. The HTTP response stays identical either way.
        _record_audit(
            "login.failure",
            related_record=user,
            notes=f"email={email or '(blank)'} ip={ip} agent={agent} "
                  f"known={'yes' if user else 'no'}"
                  f"{' locked=yes' if now_locked else ''}",
        )
        db.session.commit()
        return _render("Invalid email or password.")

    return render_template(
        "login.html",
        demo_logins=demo_login_hints(),
        demo_password=demo_login_password(),
    )


@auth_bp.route("/logout")
@login_required
def logout():
    # Recorded BEFORE logout_user(): record_audit reads current_user for the
    # actor, and after logout that is the anonymous user, so the row would lose
    # the very attribution it exists to provide.
    ip, agent = _request_fingerprint()
    _record_audit("logout", related_record=current_user,
                 notes=f"email={current_user.email} ip={ip} agent={agent}")
    db.session.commit()
    logout_user()
    flash("You have been logged out.", "info")
    return redirect(url_for("auth.login"))


@auth_bp.route("/forgot-password")
def forgot_password():
    return render_template("forgot_password.html")
