from flask import Blueprint, has_request_context, render_template
from flask_login import current_user

from app import db
from app.auth import role_required
from app.models import AuditTrail

audit_bp = Blueprint("audit", __name__, url_prefix="/audit")


def record_audit(action, related_record=None, notes=""):
    record_type = related_record.__class__.__name__ if related_record is not None else None
    record_id = getattr(related_record, "id", None) if related_record is not None else None
    if has_request_context() and current_user and current_user.is_authenticated:
        user_id = current_user.id
        user_role = current_user.role
    else:
        user_id = None
        user_role = "system"
    db.session.add(
        AuditTrail(
            user_id=user_id,
            user_role=user_role,
            action=action,
            related_record_type=record_type,
            related_record_id=record_id,
            notes=notes,
        )
    )


@audit_bp.route("")
@role_required("admin", "md")
def audit_trail():
    entries = AuditTrail.query.order_by(AuditTrail.created_at.desc()).limit(250).all()
    return render_template("audit.html", entries=entries)
