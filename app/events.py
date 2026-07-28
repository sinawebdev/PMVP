"""Domain events + in-app notification fan-out (Payrolla Phase 6).

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
    """Platform oversight users who should hear about tenant-side activity."""
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
    "distribution.cancelled": "Distribution cancelled",
    "distribution.completed": "Distribution completed",
    "distribution.partial": "Distribution partially completed",
    "distribution.failed": "Distribution failed",
    "distribution.batch_failed": "Distribution batch failed",
    "distribution.retry_exhausted": "Payslip retries exhausted",
    "distribution.scheduled_started": "Scheduled distribution started",
    "distribution.worker_stopped": "Distribution worker stopped",
    "distribution.sla_breach": "Distribution SLA breach",
}


def event_type_label(event_type):
    return _EVENT_LABELS.get(event_type, event_type.replace(".", " ").replace("_", " ").title())


# --- Platform activity timeline ---------------------------------------------
# The operator dashboard's "what has been happening across the book" feed. The
# two existing logs already hold everything it needs, so this introduces no
# model and no writer: DomainEvents supply the distribution/risk milestones,
# AuditTrail the lifecycle ones. AuditTrail is filtered to a curated set of
# MILESTONE actions — an unfiltered feed drowns in per-field payroll edits and
# stops being an executive summary.
PLATFORM_TIMELINE_ACTIONS = (
    "Client company onboarded",
    "Client run imported",
    "Payroll import confirmed",
    "Payroll approval",
    "Payroll rejection",
    "Payroll processed",
    "Payslips distributed",
    "Risk hold released",
    "Expense recorded",
    "Statutory rate version added",
)

# title -> (Bootstrap icon class, semantic tone). Same layering as
# app.payroll_status.status_badge_class: presentation mapping in Python so every
# surface renders the same event identically.
_TIMELINE_LOOKS = {
    "Client company onboarded": ("bi-building-add", "success"),
    "Client run imported": ("bi-file-earmark-arrow-up", "info"),
    "Payroll import confirmed": ("bi-file-earmark-arrow-up", "info"),
    "Payroll approval": ("bi-check2-circle", "success"),
    "Payroll rejection": ("bi-x-circle", "danger"),
    "Payroll processed": ("bi-flag", "primary"),
    "Payslips distributed": ("bi-send-check", "primary"),
    "Distribution completed": ("bi-send-check", "primary"),
    "Distribution failed": ("bi-exclamation-octagon", "danger"),
    "Distribution cancelled": ("bi-slash-circle", "warning"),
    "Payroll run held for review": ("bi-shield-exclamation", "warning"),
    "Payroll run released": ("bi-shield-check", "success"),
    "Risk hold released": ("bi-shield-check", "success"),
    "Payroll run auto-accepted": ("bi-shield-check", "info"),
    "Expense recorded": ("bi-receipt", "secondary"),
    "Statutory rate version added": ("bi-bank", "secondary"),
}
_DEFAULT_LOOK = ("bi-dot", "secondary")


def timeline_look(title):
    """(icon class, tone) for a timeline entry; a sane default for anything new."""
    return _TIMELINE_LOOKS.get(title, _DEFAULT_LOOK)


def platform_activity(limit=10):
    """The most recent cross-tenant milestones, newest first.

    Two bounded queries (each capped at ``limit``), merged and re-sorted — never
    a full-table scan, and no per-row lookups. Each item is
    ``{at, actor, title, detail, company, icon, tone}``; ``company`` is the
    tenant the event belongs to when it is knowable, else None (a platform-plane
    event).
    """
    items = []

    events = (
        DomainEvent.query.order_by(DomainEvent.created_at.desc()).limit(limit).all()
    )
    for event in events:
        title = event_type_label(event.event_type)
        icon, tone = timeline_look(title)
        items.append(
            {
                "at": event.created_at,
                "actor": event.actor.name if event.actor else "System",
                "title": title,
                "detail": event.summary or "",
                "company": event.client_company.name if event.client_company else None,
                "icon": icon,
                "tone": tone,
            }
        )

    audits = (
        AuditTrail.query.filter(AuditTrail.action.in_(PLATFORM_TIMELINE_ACTIONS))
        .order_by(AuditTrail.created_at.desc())
        .limit(limit)
        .all()
    )
    for entry in audits:
        icon, tone = timeline_look(entry.action)
        items.append(
            {
                "at": entry.created_at,
                "actor": entry.user.name if entry.user else "System",
                "title": entry.action,
                "detail": entry.notes or "",
                "company": None,
                "icon": icon,
                "tone": tone,
            }
        )

    items.sort(key=lambda item: item["at"].timestamp() if item["at"] else 0.0, reverse=True)
    return items[:limit]


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
