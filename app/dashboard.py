"""Executive dashboard view model — the company portal's decision surface.

The tenant dashboard used to be a statistics page: four numbers, three charts, a
table. This module turns the SAME signals into an executive workspace by asking,
for each panel, *what decision does this support* — then answering with a value,
its movement, and the one action that follows.

Deliberately a **composition layer, not a new engine**. Every figure is already
produced elsewhere and is merely assembled: :mod:`app.analytics` (period
aggregation, deltas, chart shapes), :mod:`app.risk` (verdict, run comparison,
duplicates), :mod:`app.payroll_status` (lifecycle stepper),
:mod:`app.compliance` (statutory readiness), :func:`app.events.tenant_activity`
(the audit feed), and the models' own columns. No new table, no cached rollup,
no business rule — if a number here disagrees with the page it links to, that is
a bug in this file, not a second opinion.

**Tenancy.** Every function takes data the caller already scoped to one tenant
(the same contract as :mod:`app.analytics`), except :func:`cost_composition`,
which takes a run and filters on its id. Nothing here reads ``current_user`` or
the session, so it cannot widen a caller's data horizon.
"""

from datetime import datetime, timedelta, timezone

from sqlalchemy import func

from app import db
from app.analytics import MONTH_INDEX, expense_summary, monthly_totals, pct_change
from app.compliance import compliance_overview
from app.events import as_utc, tenant_activity
from app.models import PayrollItem
from app.payroll_status import (
    APPROVED,
    AUTO_ACCEPTED,
    CLOSED_STATUSES,
    DRAFT,
    HELD,
    PENDING_APPROVAL,
    PENDING_STATUSES,
    PROCESSED,
    REJECTED,
    SUBMITTED,
    run_progress,
)
from app.risk import compare_to_previous, find_possible_duplicates, risk_badge
from app.roles import CLIENT_ADMIN, CLIENT_PREPARER, normalise_role

# --- Metrics hierarchy -------------------------------------------------------
# Tier 1 (the KPI band) answers four *different* questions, one card each: how
# much is payroll costing, how big is the workforce, what else are we spending,
# and are we safe. A fifth card would repeat one of them. Run STATUS is
# deliberately NOT a KPI card — a state that needs a reason and a next step does
# not fit in a number tile, so it gets the Payroll Health panel instead.

# --- 1. Executive KPIs -------------------------------------------------------


def executive_kpis(analytics, expenses_summary, active_employees, compliance, workforce):
    """The four Tier-1 tiles: ``{key, label, value, formatter, delta, caption,
    href, trend}``.

    ``delta`` is a signed fraction (or None where there is no baseline — never a
    fabricated 0%), rendered by the existing ``delta`` chart macro so movement is
    carried by an arrow AND wording, not by colour alone. ``trend`` is the
    sparkline series where one exists, so each tile shows shape as well as value.
    """
    stats = analytics["stats"]
    cost_points = analytics["cost_by_month"]
    cost_now = cost_points[-1]["value"] if cost_points else 0
    cost_prev = cost_points[-2]["value"] if len(cost_points) > 1 else None
    period = stats.get("current_period")

    return [
        {
            "key": "payroll_cost",
            "label": "Payroll cost",
            "value": cost_now,
            "formatter": "cedis",
            "delta": pct_change(cost_now, cost_prev),
            "caption": (
                f"Gross + employer SSF · {period}" if period else "No payroll run yet"
            ),
            "compare": stats.get("previous_period"),
            "trend": cost_points,
            "endpoint": "client.runs",
        },
        {
            "key": "workforce",
            "label": "Workforce",
            "value": active_employees,
            "formatter": "count",
            "delta": workforce["growth_rate"],
            "caption": f"Active employees · {workforce['paid']} paid last run",
            "compare": workforce["previous_period"],
            "trend": workforce["headcount"],
            "endpoint": "client.employees",
        },
        {
            "key": "expenses",
            "label": "Operating expenses",
            "value": expenses_summary["monthly_total"],
            "formatter": "cedis",
            "delta": expenses_summary["monthly_change"],
            "caption": f"{expenses_summary['monthly_label']} · {expenses_summary['count']} recorded",
            "compare": "last month",
            "trend": expenses_summary["by_month"],
            "endpoint": "client.expenses",
        },
        {
            "key": "compliance",
            "label": "Compliance",
            "value": compliance["score"],
            "formatter": "percent",
            "delta": None,
            "caption": compliance["headline"],
            "compare": None,
            "trend": [],
            "endpoint": "client.statutory",
        },
    ]


# --- 2. Payroll health -------------------------------------------------------

# What the company must do next, per lifecycle state. Keyed on the SAME status
# vocabulary the gate writes, so a status the app can produce always has a next
# step — and the dashboard never says "Held" without saying what to do about it.
_NEXT_STEP = {
    DRAFT: ("Finish this import", "Confirm the upload to create the payroll run."),
    SUBMITTED: ("Awaiting risk check", "The risk gate is scoring this run."),
    AUTO_ACCEPTED: (
        "Awaiting approval",
        "Passed every risk rule; queued for Payrolla approval.",
    ),
    HELD: (
        "Review payroll",
        "Held for Payrolla review. Check the figures below against your records.",
    ),
    PENDING_APPROVAL: ("Awaiting approval", "With Payrolla oversight for sign-off."),
    APPROVED: ("Distribute payslips", "Approved — payslips can now be sent."),
    PROCESSED: ("Closed", "Paid and closed. Reports and exports are available."),
    REJECTED: ("Re-upload payroll", "Rejected — correct the workbook and upload again."),
}


def payroll_health(runs, draft_count=0, distributed=False):
    """The Payroll Health Center: the latest run's state, how far through the
    lifecycle it is, and the single next step.

    ``{run, status, stage, steps, completion, next_step, next_detail, exceptions,
    pending_count, draft_count, has_run}``. ``completion`` is derived from the
    existing lifecycle stepper (steps done / steps that apply to this run), so it
    can never drift from the stepper rendered beside it."""
    latest = max(runs, key=lambda r: (r.year or 0, MONTH_INDEX.get(r.month, 0), r.id), default=None)
    pending = sum(1 for run in runs if run.status in PENDING_STATUSES or run.status == HELD)

    if latest is None:
        return {
            "has_run": False,
            "run": None,
            "steps": [],
            "completion": 0,
            "pending_count": pending,
            "draft_count": draft_count,
            "exceptions": 0,
        }

    steps = run_progress(latest, distributed=distributed)
    applicable = [s for s in steps if s["state"] != "skipped"]
    done = sum(1 for s in applicable if s["state"] == "done")
    label, detail = _NEXT_STEP.get(latest.status, ("Open payroll", "Review this run."))

    return {
        "has_run": True,
        "run": latest,
        "steps": steps,
        "completion": round(done / len(applicable) * 100) if applicable else 0,
        "next_step": label,
        "next_detail": detail,
        "exceptions": latest.warning_count,
        "pending_count": pending,
        "draft_count": draft_count,
        "verdict": risk_badge(latest),
    }


# --- 3. Risk intelligence ----------------------------------------------------


def risk_intelligence(run, limit=4):
    """Plain-English risk signals for the latest run, worst first.

    Every signal comes from an engine that already exists: the PERSISTED risk
    verdict (``run.risk_reasons``, written by the gate at scoring time — not
    re-scored here, so the panel and the run's own badge can never disagree),
    :func:`app.risk.compare_to_previous` for period-over-period movement,
    ``warning_count`` for row-level exceptions, and
    :func:`app.risk.find_possible_duplicates`.

    Each item is ``{tone, title, detail, endpoint}`` where ``tone`` is
    ``warn`` | ``danger`` | ``ok``. An all-clear returns one ``ok`` signal rather
    than an empty panel — silence is not the same as reassurance."""
    if run is None:
        return []

    signals = []

    # Period-over-period movement at the gate's own thresholds — this is what
    # produces "Net pay increased 49%" with a real, signed number. Only the two
    # metrics the gate scores are reported: gross, deductions and taxes move
    # mechanically with headcount and net pay, so surfacing all five turns one
    # event ("we hired twelve people") into five near-identical warnings, and a
    # panel that cries wolf five times is one an executive learns to skip. If
    # neither headline metric tripped, the largest remaining movement is shown,
    # so an isolated anomaly (deductions alone) is never swallowed.
    comparison = compare_to_previous(run)
    flagged = [row for row in comparison["rows"] if row["flag"]]
    headline = [row for row in flagged if row["key"] in ("workers", "net")]
    if not headline and flagged:
        headline = [max(flagged, key=lambda r: r["pct"] if r["pct"] is not None else 0)]

    for row in headline:
        change = row["signed_pct"]
        direction = "increased" if (change or 0) > 0 else "decreased"
        movement = f"{abs(change) * 100:.0f}%" if change is not None else "changed"
        signals.append(
            {
                "tone": "warn",
                "title": f"{row['label']} {direction} {movement}",
                "detail": (
                    f"{row['label']} moved beyond the review threshold against "
                    f"{comparison['previous'].month} {comparison['previous'].year}. "
                    "Confirm the change is expected."
                ),
                "endpoint": "client.run_detail",
            }
        )

    if run.warning_count:
        signals.append(
            {
                "tone": "warn",
                "title": f"{run.warning_count} row{'' if run.warning_count == 1 else 's'} need a second look",
                "detail": "Validation flagged these payroll lines during import.",
                "endpoint": "client.run_detail",
            }
        )

    duplicates = find_possible_duplicates(run)
    if duplicates:
        signals.append(
            {
                "tone": "danger",
                "title": f"{len(duplicates)} possible duplicate run",
                "detail": (
                    "Another run for your company has identical headcount and net pay. "
                    "Verify before payslips go out."
                ),
                "endpoint": "client.runs",
            }
        )

    # Rules the gate itself tripped, as recorded on the run. Shown last because
    # the movement signals above say the same thing in business language.
    if run.status == HELD and run.risk_reasons:
        for reason in [r.strip() for r in run.risk_reasons.split("|") if r.strip()][:2]:
            signals.append(
                {"tone": "warn", "title": "Held by the risk gate", "detail": reason,
                 "endpoint": "client.run_detail"}
            )

    if not signals:
        signals.append(
            {
                "tone": "ok",
                "title": "No risks detected",
                "detail": (
                    f"{run.month} {run.year} passed every variance, headcount and "
                    "duplicate check."
                ),
                "endpoint": "client.run_detail",
            }
        )
    return signals[:limit]


# --- 4. Financial analytics --------------------------------------------------

# (label, columns to sum, donut tone). Employer outlay only — employee
# deductions are a slice OF gross, not an addition to it, so putting them in the
# same donut would double-count the money. Where gross GOES is reported
# separately by `destination` below.
_COST_PARTS = (
    ("Basic salaries", ("basic_salary",), "brand"),
    (
        "Allowances",
        ("transport_allowance", "housing_allowance", "medical_allowance",
         "meal_allowance", "other_allowances"),
        "accent",
    ),
    ("Bonuses", ("productivity_bonus", "end_of_year_bonus"), "deep"),
    ("Overtime", ("overtime_pay",), "soft"),
)


def cost_composition(run):
    """What the latest payroll is made of, donut-ready.

    ``{slices, total, destination}`` — the same ``{label, value, pct, tone}``
    slice contract :func:`app.analytics.cost_mix` uses, so the existing donut
    macro renders it unchanged. ``destination`` reports where gross goes (net to
    workers, PAYE, SSNIT, employer SSF) as a separate, non-overlapping list.

    One aggregate query, no row loading — a 500-employee run costs the same as a
    5-employee one."""
    empty = {"slices": [], "total": 0, "destination": []}
    if run is None:
        return empty

    columns = [c for _label, cols, _tone in _COST_PARTS for c in cols]
    columns += ["paye", "ssnit", "ssf_employer"]
    totals = db.session.query(
        *[func.coalesce(func.sum(getattr(PayrollItem, c)), 0.0) for c in columns]
    ).filter(PayrollItem.payroll_run_id == run.id).first()
    if totals is None:
        return empty
    summed = dict(zip(columns, totals))

    parts = [
        (label, sum(summed.get(c, 0) or 0 for c in cols), tone)
        for label, cols, tone in _COST_PARTS
    ]
    total = sum(value for _label, value, _tone in parts)
    if total <= 0:
        return empty

    net = (run.total_net_pay or 0)
    return {
        "total": total,
        "slices": [
            {"label": label, "value": value, "pct": round(value / total * 100, 2), "tone": tone}
            for label, value, tone in parts
            if value > 0
        ],
        "destination": [
            {"label": "Net to employees", "value": net, "tone": "brand"},
            {"label": "PAYE to GRA", "value": summed.get("paye", 0) or 0, "tone": "accent"},
            {"label": "SSNIT (employee)", "value": summed.get("ssnit", 0) or 0, "tone": "deep"},
            {"label": "SSF (employer)", "value": summed.get("ssf_employer", 0) or 0, "tone": "soft"},
        ],
    }


def workforce_movement(runs, employees, now=None):
    """Headcount over time plus this period's movement.

    ``{headcount, paid, joined, left, on_leave, growth_rate, previous_period}``.
    ``headcount`` is the *paid* headcount per period (``run.total_workers``) —
    the only headcount the company has a financial record of. ``left`` and
    ``on_leave`` come from the run's own ``terminated_workers`` /
    ``on_leave_workers`` columns; ``joined`` counts employee records created in
    the last 30 days. Nothing here is inferred from a status change, which has no
    date on the model and would be a guess."""
    now = as_utc(now) or datetime.now(timezone.utc)
    rows = monthly_totals(runs)
    peak = max((row["workers"] for row in rows), default=0)
    headcount = [
        {
            "label": row["label"],
            "full_label": row["full_label"],
            "value": row["workers"],
            "pct": round(row["workers"] / peak * 100, 2) if peak else 0,
        }
        for row in rows
    ]
    latest = max(runs, key=lambda r: (r.year or 0, MONTH_INDEX.get(r.month, 0), r.id), default=None)
    cutoff = now - timedelta(days=30)
    joined = sum(
        1 for e in employees
        if as_utc(getattr(e, "created_at", None)) and as_utc(e.created_at) >= cutoff
    )
    return {
        "headcount": headcount,
        "paid": latest.total_workers or 0 if latest else 0,
        "joined": joined,
        "left": (latest.terminated_workers or 0) if latest else 0,
        "on_leave": (latest.on_leave_workers or 0) if latest else 0,
        "growth_rate": pct_change(rows[-1]["workers"], rows[-2]["workers"]) if len(rows) > 1 else None,
        "previous_period": rows[-2]["full_label"] if len(rows) > 1 else None,
    }


# Sections 5 and 6 of the dashboard — the Compliance panel and the Recent
# Activity feed — are assembled by app/compliance.py and
# app.events.tenant_activity respectively, and are pulled in by
# company_overview below.

# --- 5. Quick actions --------------------------------------------------------

# (key, label, hint, icon, endpoint, roles, needs_run). `roles` mirrors the
# decorator on the target route, so a button is never offered to someone the
# route itself would bounce — the gate stays on the route; this only stops the
# dashboard from advertising a dead end.
_ACTIONS = (
    ("run_payroll", "Run payroll", "Upload this period's workbook", "upload",
     "client.run_upload", (CLIENT_ADMIN, CLIENT_PREPARER), False),
    ("employees", "Manage employees", "Roster, pay details, documents", "users",
     "client.employees", None, False),
    ("reports", "View reports", "Bank listing, GRA PAYE schedule", "file-text",
     "client.run_reports", None, True),
    ("summary", "Download payroll summary", "Full workbook for the latest run", "download",
     "client.download_payroll", None, True),
    # client_admin only, matching tenant_role_required(CLIENT_ADMIN) on
    # client.distribute_send — a preparer can still open the run's own
    # distribution page, but is not offered a shortcut to an action they cannot
    # take.
    ("distribute", "Send payslips", "Distribute the approved run", "send",
     "client.distribute", (CLIENT_ADMIN,), True),
    ("risks", "Review risks", "Held runs and flagged variances", "shield-alert",
     "client.runs", None, False),
    ("expenses", "View expenses", "Operating spend and receipts", "receipt",
     "client.expenses", None, False),
)


def expenses_panel(expenses, today=None):
    """:func:`app.analytics.expense_summary` plus the one thing a KPI tile needs
    that it does not carry — the change against the PREVIOUS calendar month.

    Computed here rather than in analytics so the headline value
    (``monthly_total``, this calendar month) and its delta describe the same
    window; comparing "this month" against "the last month that had any expense"
    would be two different questions in one tile."""
    from datetime import date as _date

    today = today or _date.today()
    rows = list(expenses)
    summary = expense_summary(rows, today=today)
    year, month = (today.year, today.month - 1) if today.month > 1 else (today.year - 1, 12)
    previous = sum(
        (e.amount or 0) for e in rows
        if e.expense_date and e.expense_date.year == year and e.expense_date.month == month
    )
    summary["monthly_change"] = pct_change(summary["monthly_total"], previous or None)
    return summary


def quick_actions(role, latest_run, completed_run=None):
    """Role-filtered executive shortcuts: ``{key, label, hint, icon, endpoint,
    run_id}``.

    Actions needing a run are pointed at the newest *completed* run where the
    target requires one (exports are gated on Approved/Processed by the route),
    and dropped entirely when the company has none — an executive should never
    click a shortcut into an error page."""
    role = normalise_role(role)
    resolved = []
    for key, label, hint, icon, endpoint, roles, needs_run in _ACTIONS:
        if roles and role not in roles:
            continue
        run = completed_run if key in ("reports", "summary", "distribute") else latest_run
        if needs_run and run is None:
            continue
        resolved.append({
            "key": key, "label": label, "hint": hint, "icon": icon,
            "endpoint": endpoint, "run_id": run.id if needs_run and run else None,
        })
    return resolved


# --- Assembly ----------------------------------------------------------------


def company_overview(company, runs, employees, expenses, role, draft_count=0, today=None):
    """The whole dashboard in one dict, so the route stays a data-fetch and the
    template stays a layout.

    ``runs``, ``employees`` and ``expenses`` arrive already tenant-scoped by the
    caller — the same contract as :mod:`app.analytics` — so nothing here can
    reach another company's rows. Cost is bounded and does not grow with the
    roster: the per-period aggregation is in-Python over runs the route already
    loaded, and the three queries added (cost composition, distribution check,
    activity) are each a single bounded statement.
    """
    from app.analytics import client_dashboard_analytics
    from app.payroll import distributed_run_ids

    latest = max(runs, key=lambda r: (r.year or 0, MONTH_INDEX.get(r.month, 0), r.id), default=None)
    completed = max(
        (r for r in runs if r.status in CLOSED_STATUSES),
        key=lambda r: (r.year or 0, MONTH_INDEX.get(r.month, 0), r.id),
        default=None,
    )
    active_employees = sum(1 for e in employees if (e.status or "").strip().lower() == "active")
    expense_rows = list(expenses)

    analytics = client_dashboard_analytics(
        runs,
        employee_count=len(employees),
        expense_total=sum((e.amount or 0) for e in expense_rows),
    )
    spend = expenses_panel(expense_rows, today=today)
    workforce = workforce_movement(runs, employees)
    compliance = compliance_overview(employees, latest, today=today)
    distributed = bool(latest and latest.id in distributed_run_ids([latest.id]))

    return {
        "analytics": analytics,
        "kpis": executive_kpis(analytics, spend, active_employees, compliance, workforce),
        "health": payroll_health(runs, draft_count=draft_count, distributed=distributed),
        "risks": risk_intelligence(latest),
        "composition": cost_composition(latest),
        "workforce": workforce,
        "compliance": compliance,
        "activity": tenant_activity(company.id, limit=8),
        "actions": quick_actions(role, latest, completed_run=completed),
        "spend": spend,
        "latest_run": latest,
        "completed_run": completed,
    }
