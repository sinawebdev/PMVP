"""Domain events + in-app notification fan-out (PMVP v1 Phase 6).

One entry point, :func:`record_event`, appends a :class:`DomainEvent` (the
append-only business-event log) and fans it out to recipient users as
:class:`Notification` rows. Like :func:`app.audit.record_audit`, it stages rows
but never commits — the calling route owns the transaction, so the state change
and its event are written atomically.

Recipient resolvers (:func:`tenant_users`, :func:`platform_admins`) return the
Users to notify, so a route says *what* happened and *who* should hear about it
without duplicating queries.
"""

import json as _json

from flask import has_request_context
from flask_login import current_user

from app import db
from app.models import AuditTrail, DomainEvent, Notification, User
from app.roles import PLATFORM_ROLES, TENANT_ROLES


def _actor():
    if has_request_context() and getattr(current_user, "is_authenticated", False):
        return current_user.id, current_user.role
    return None, "system"


def _tenant_of(subject):
    """Best-effort tenant id for a subject: its own client_company_id, or its
    payroll_run's. None if it cannot be determined (a platform-plane subject)."""
    if subject is None:
        return None
    direct = getattr(subject, "client_company_id", None)
    if direct is not None:
        return direct
    run = getattr(subject, "payroll_run", None)
    return getattr(run, "client_company_id", None) if run is not None else None


def record_event(
    event_type,
    *,
    summary="",
    subject=None,
    client_company_id=None,
    level="info",
    payload=None,
    recipients=None,
):
    """Append a DomainEvent and notify ``recipients``. Stages rows; caller commits.

    ``client_company_id`` defaults to the subject's tenant. ``recipients`` is an
    iterable of Users (duplicates and Nones are ignored); each gets one
    Notification carrying ``summary`` at ``level``. Returns the DomainEvent.
    """
    actor_id, actor_role = _actor()
    if client_company_id is None:
        client_company_id = _tenant_of(subject)

    event = DomainEvent(
        event_type=event_type,
        actor_user_id=actor_id,
        actor_role=actor_role,
        client_company_id=client_company_id,
        subject_type=subject.__class__.__name__ if subject is not None else None,
        subject_id=getattr(subject, "id", None) if subject is not None else None,
        summary=summary,
        payload=_json.dumps(payload) if payload is not None else None,
    )
    db.session.add(event)
    db.session.flush()  # assign event.id so notifications can reference it

    seen = set()
    for user in recipients or []:
        if user is None or user.id in seen:
            continue
        seen.add(user.id)
        db.session.add(
            Notification(
                user_id=user.id,
                client_company_id=client_company_id,
                event_id=event.id,
                title=event_type_label(event_type),
                body=summary,
                level=level,
            )
        )
    return event


# --- Recipient resolvers ----------------------------------------------------
def tenant_users(client_company_id):
    """All users belonging to a client company (client_admin + client_preparer)."""
    if not client_company_id:
        return []
    return (
        User.query.filter(
            User.client_company_id == client_company_id,
            User.role.in_(tuple(TENANT_ROLES)),
        ).all()
    )


def platform_admins():
    """Chrisnat oversight users who should hear about tenant-side activity."""
    return (
        User.query.filter(
            User.client_company_id.is_(None),
            User.role.in_(tuple(PLATFORM_ROLES)),
        ).all()
    )


# --- Presentation -----------------------------------------------------------
_EVENT_LABELS = {
    "run.risk_held": "Payroll run held for review",
    "run.risk_accepted": "Payroll run auto-accepted",
    "run.hold_released": "Payroll run released",
    "payslips.distributed": "Payslips distributed",
}


def event_type_label(event_type):
    return _EVENT_LABELS.get(event_type, event_type.replace(".", " ").replace("_", " ").title())


def run_activity(run):
    """A run's merged, most-recent-first activity + approval timeline.

    Combines the two existing sources — AuditTrail entries recorded against the
    run (submit / approve / reject / process / edits) and DomainEvents about it
    (risk held/accepted/released, payslips distributed) — into one uniform list
    of ``{at, actor, title, detail, kind}`` dicts. Read-only; no new model."""
    items = []
    audits = (
        AuditTrail.query.filter_by(
            related_record_type="PayrollRun", related_record_id=run.id
        )
        .order_by(AuditTrail.created_at.desc())
        .all()
    )
    for entry in audits:
        items.append(
            {
                "at": entry.created_at,
                "actor": entry.user.name if entry.user else "System",
                "title": entry.action,
                "detail": entry.notes or "",
                "kind": "audit",
            }
        )
    events = (
        DomainEvent.query.filter_by(subject_type="PayrollRun", subject_id=run.id)
        .order_by(DomainEvent.created_at.desc())
        .all()
    )
    for event in events:
        items.append(
            {
                "at": event.created_at,
                "actor": event.actor.name if event.actor else "System",
                "title": event_type_label(event.event_type),
                "detail": event.summary or "",
                "kind": "event",
            }
        )
    items.sort(key=lambda item: item["at"].timestamp() if item["at"] else 0.0, reverse=True)
    return items
