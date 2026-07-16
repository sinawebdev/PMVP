"""In-app notifications (PMVP v1 Phase 6).

Per-user notification inbox for BOTH planes. A Notification is owned by one
recipient (``user_id``), so every query here filters by ``current_user.id`` —
a user can only ever see, or mark read, their own notices. There is no
cross-tenant surface: the owning user IS the scope.
"""

from datetime import datetime, timezone

from flask import Blueprint, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from app import db
from app.models import ClientCompany, Notification
from app.tenancy import active_tenant_id, is_platform_context

notifications_bp = Blueprint("notifications", __name__, url_prefix="/notifications")


def _my_query():
    return Notification.query.filter(Notification.user_id == current_user.id)


def unread_count():
    """Unread notifications for the current user (0 for anonymous)."""
    if not getattr(current_user, "is_authenticated", False):
        return 0
    return _my_query().filter(Notification.read_at.is_(None)).count()


@notifications_bp.route("")
@login_required
def inbox():
    items = _my_query().order_by(Notification.created_at.desc()).limit(200).all()
    if is_platform_context():
        return render_template("notifications/platform.html", items=items, unread=unread_count())
    company = db.session.get(ClientCompany, active_tenant_id())
    return render_template(
        "notifications/tenant.html", items=items, unread=unread_count(), company=company
    )


@notifications_bp.route("/<int:note_id>/read", methods=["POST"])
@login_required
def mark_read(note_id):
    note = _my_query().filter(Notification.id == note_id).first_or_404()
    if note.read_at is None:
        note.read_at = datetime.now(timezone.utc)
        db.session.commit()
    return redirect(url_for("notifications.inbox"))


@notifications_bp.route("/read-all", methods=["POST"])
@login_required
def mark_all_read():
    now = datetime.now(timezone.utc)
    unread = _my_query().filter(Notification.read_at.is_(None)).all()
    for note in unread:
        note.read_at = now
    db.session.commit()
    flash(f"Marked {len(unread)} notification(s) as read.", "success")
    return redirect(url_for("notifications.inbox"))
