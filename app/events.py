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
from datetime import datetime, timezone

from flask import has_request_context
from flask_login import current_user
from sqlalchemy import and_, or_

from app import db
from app.models import AuditTrail, DomainEvent, Notification, PayrollRun, User
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

# title -> (icon name, tone), for BOTH feeds. Same layering as
# app.payroll_status.status_badge_class: the presentation mapping lives in
# Python so every surface renders the same event identically.
#
# One table, not two. Both timelines are drawn by the same component
# (macros/dashboard.html::activity_item), so they need the same two vocabularies
# it understands: an icon name from that file's inline SVG set, and a tone from
# `ok | warn | danger | brand | muted`. The platform feed used to carry
# Bootstrap Icons classes and Bootstrap colour words instead — neither of which
# that component can render — so every operator-dashboard entry fell back to the
# generic glyph with no tone. A second table is how those drifted apart; this is
# one table so they cannot again.
_TIMELINE_LOOKS = {
    # Platform-plane milestones
    "Client company onboarded": ("users", "ok"),
    "Payroll import confirmed": ("upload", "muted"),
    "Statutory rate version added": ("wallet", "muted"),
    "Payslip retries exhausted": ("x-circle", "danger"),
    "Distribution batch failed": ("x-circle", "danger"),
    "Distribution SLA breach": ("alert-triangle", "warn"),
    # Shared by both planes
    "Payroll run held for review": ("shield-alert", "warn"),
    "Payroll run released": ("shield-check", "ok"),
    "Payroll run auto-accepted": ("shield-check", "ok"),
    "Risk hold released": ("shield-check", "ok"),
    "Payroll approval": ("check-circle", "ok"),
    "Payroll rejection": ("x-circle", "danger"),
    "Payroll processed": ("flag", "brand"),
    "Payslips distributed": ("send", "brand"),
    "Distribution completed": ("send", "brand"),
    "Distribution failed": ("x-circle", "danger"),
    "Distribution cancelled": ("alert-triangle", "warn"),
    "Client run imported": ("upload", "muted"),
    "Client import draft": ("upload", "muted"),
    "Client import discarded": ("upload", "muted"),
    "Employee saved": ("users", "muted"),
    "Employee deactivated": ("users", "warn"),
    "Employee reactivated": ("users", "muted"),
    "Expense recorded": ("receipt", "muted"),
    "Client payroll export": ("download", "muted"),
    "Client bank listing export": ("download", "muted"),
    "Branding updated": ("activity", "muted"),
}
# Neutral rather than absent: an event nobody has mapped yet still renders as a
# real entry instead of a hole in the feed.
_DEFAULT_LOOK = ("activity", "muted")


def timeline_look(title):
    """(icon name, tone) for a timeline entry; a sane default for anything new."""
    return _TIMELINE_LOOKS.get(title, _DEFAULT_LOOK)


def platform_activity(limit=10, now=None):
    """The most recent cross-tenant milestones, newest first.

    Two bounded queries (each capped at ``limit``), merged and re-sorted — never
    a full-table scan, and no per-row lookups. Each item is
    ``{at, when, actor, title, detail, company, icon, tone}``; ``company`` is
    the tenant the event belongs to when it is knowable, else None (a
    platform-plane event). ``when`` is the same relative-time string
    :func:`tenant_activity` computes, so the operator dashboard's timeline can
    reuse ``macros/dashboard.html::activity_item`` unchanged.
    """
    now = now or datetime.now(timezone.utc)
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
    items = items[:limit]
    for item in items:
        item["when"] = relative_time(item["at"], now=now)
    return items


# --- Tenant activity timeline -----------------------------------------------
# The company portal's counterpart to platform_activity: the same two sources
# and the same "curated milestones only" discipline, scoped to ONE tenant. It
# lives here rather than in the dashboard module because this file already owns
# the timeline sources, their labels and their looks — a second feed assembled
# somewhere else would eventually describe the same event differently.

TENANT_TIMELINE_ACTIONS = (
    "Client run imported",
    "Client import draft",
    "Client import discarded",
    "Payroll approval",
    "Payroll rejection",
    "Payroll processed",
    "Payslips distributed",
    "Risk hold released",
    "Employee saved",
    "Employee deactivated",
    "Employee reactivated",
    "Expense recorded",
    "Client payroll export",
    "Client bank listing export",
    "Branding updated",
)

def tenant_look(title):
    """(icon name, tone) for a tenant timeline entry.

    The same lookup :func:`timeline_look` performs, against the same table — the
    two feeds render through the same component, so an event that looks one way
    to an operator must look that way to the company it happened to. Kept as its
    own name because the tenant dashboard reads in these terms."""
    return timeline_look(title)


def as_utc(moment):
    """A comparable, timezone-aware UTC datetime.

    SQLite hands back naive values for columns written with a tz-aware default
    (:func:`app.models.utc_now`), so comparing a stored ``created_at`` against
    ``datetime.now(timezone.utc)`` raises. Normalise rather than trust the
    driver — the same row must sort identically on SQLite and PostgreSQL."""
    if moment is None:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def relative_time(moment, now=None):
    """'5 minutes ago' / 'Yesterday' / '12 Mar' for a timeline entry.

    Coarsens with age deliberately: an approval two minutes ago is a live event
    and the minute matters; one from March is a date. The exact timestamp is
    never lost — the template puts it on the element's ``title`` and in a
    machine-readable ``<time datetime>``, which is what an auditor needs."""
    moment = as_utc(moment)
    if moment is None:
        return ""
    now = as_utc(now) or datetime.now(timezone.utc)
    seconds = max((now - moment).total_seconds(), 0)
    if seconds < 60:
        return "Just now"
    if seconds < 3600:
        minutes = int(seconds // 60)
        return f"{minutes} minute{'' if minutes == 1 else 's'} ago"
    if seconds < 86400:
        hours = int(seconds // 3600)
        return f"{hours} hour{'' if hours == 1 else 's'} ago"
    if seconds < 172800:
        return "Yesterday"
    if seconds < 604800:
        return f"{int(seconds // 86400)} days ago"
    return f"{moment.day} {moment:%b %Y}"


def tenant_activity(company_id, limit=8, now=None):
    """One company's recent milestones, newest first.

    Two bounded queries merged and re-sorted, never a full-table scan:
    DomainEvents scoped by ``client_company_id``, and AuditTrail entries recorded
    by users belonging to this company — AuditTrail carries no tenant column,
    which is exactly how ``client.audit`` scopes it, so this feed and the audit
    page can never disagree about who did what.

    Each item is ``{at, when, actor, title, detail, tone, icon}``.
    """
    if not company_id:
        return []
    now = now or datetime.now(timezone.utc)
    items = []

    for event in (
        DomainEvent.query.filter(DomainEvent.client_company_id == company_id)
        .order_by(DomainEvent.created_at.desc())
        .limit(limit)
        .all()
    ):
        title = event_type_label(event.event_type)
        icon, tone = tenant_look(title)
        items.append(
            {
                "at": as_utc(event.created_at),
                "actor": event.actor.name if event.actor else "Payrolla",
                "title": title,
                "detail": event.summary or "",
                "icon": icon,
                "tone": tone,
            }
        )

    # Two ways an AuditTrail row belongs to this company, because AuditTrail
    # carries no tenant column:
    #
    #   1. one of the company's own users recorded it, and
    #   2. it was recorded AGAINST one of the company's payroll runs.
    #
    # (2) is what a Payrolla operator does on the tenant's behalf — approve,
    # reject, mark processed, release a risk hold. Scoping on the actor alone
    # meant the tenant's feed structurally could not contain those, so a company
    # whose payroll had been approved six times saw "No activity yet" under a
    # panel captioned "Every payroll action, on the record" — the dashboard
    # denying an event the operator's own dashboard was listing. Both queries
    # stay bounded by `limit`; neither widens the tenant's horizon, since the
    # run ids are themselves scoped to this company.
    user_ids = [u.id for u in User.query.filter_by(client_company_id=company_id).all()]
    run_ids = [
        row[0]
        for row in db.session.query(PayrollRun.id)
        .filter(PayrollRun.client_company_id == company_id)
        .all()
    ]

    scopes = []
    if user_ids:
        scopes.append(AuditTrail.user_id.in_(user_ids))
    if run_ids:
        scopes.append(
            and_(
                AuditTrail.related_record_type == "PayrollRun",
                AuditTrail.related_record_id.in_(run_ids),
            )
        )

    if scopes:
        seen = set()
        for entry in (
            AuditTrail.query.filter(
                or_(*scopes),
                AuditTrail.action.in_(TENANT_TIMELINE_ACTIONS),
            )
            .order_by(AuditTrail.created_at.desc())
            .limit(limit)
            .all()
        ):
            if entry.id in seen:
                continue
            seen.add(entry.id)
            icon, tone = tenant_look(entry.action)
            items.append(
                {
                    "at": as_utc(entry.created_at),
                    # A platform user acting on this company's payroll is shown
                    # as "Payrolla", not by their personal name: to the tenant
                    # the actor is their provider, and naming an individual
                    # operator leaks staffing detail they did not ask for.
                    "actor": (
                        entry.user.name
                        if entry.user and entry.user.client_company_id == company_id
                        else "Payrolla"
                    ),
                    "title": entry.action,
                    "detail": entry.notes or "",
                    "icon": icon,
                    "tone": tone,
                }
            )

    items.sort(key=lambda item: item["at"].timestamp() if item["at"] else 0.0, reverse=True)
    for item in items:
        item["when"] = relative_time(item["at"], now=now)
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
