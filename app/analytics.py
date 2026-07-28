"""Dashboard analytics — read-only aggregation for the executive summary charts.

Deliberately NOT a BI layer. Every figure here is a plain aggregate of columns
PayrollRun/Expense/Employee already carry, computed per request from the same
tenant-scoped queries the rest of the portal uses. No new tables, no cached
rollups (so nothing can go stale), no business rules — presentation maths only.

Two consumers:

  * :func:`client_dashboard_analytics` — the tenant company portal's overview
    (monthly trend, cost by month, cost mix, quick stats)
  * :func:`client_cost_trend` — the operator dashboard's per-client sparkline

Series are returned chart-ready: each point carries its raw ``value`` plus
``pct``, its height as a percentage of the series maximum, so the Jinja chart
macros (templates/macros/charts.html) stay pure geometry and do no arithmetic.
"""

from calendar import month_name

# Month name -> 1..12, for ordering runs stored as (month name, year).
MONTH_INDEX = {name: index for index, name in enumerate(month_name) if name}

# How many periods the dashboard charts look back over. Six keeps the trend
# readable at card size and the query trivial.
TREND_PERIODS = 6


def period_key(run):
    """Sortable chronological key for a run's period, e.g. (2026, 7)."""
    return (run.year or 0, MONTH_INDEX.get(run.month, 0))


def _short_month(month):
    """'September' -> 'Sep' — the axis label at card width."""
    return str(month or "")[:3]


def _as_points(rows, value_key):
    """Turn period rows into chart points scaled against the series maximum.

    Each point is ``{label, full_label, value, pct}``. ``pct`` is 0-100 of the
    largest value in the series, which is what the chart macros plot. An
    all-zero series yields pct 0 throughout rather than dividing by zero."""
    peak = max((row[value_key] for row in rows), default=0)
    return [
        {
            "label": row["label"],
            "full_label": row["full_label"],
            "value": row[value_key],
            "pct": round(row[value_key] / peak * 100, 2) if peak else 0,
        }
        for row in rows
    ]


def monthly_totals(runs, periods=TREND_PERIODS):
    """Aggregate ``runs`` into up to ``periods`` chronological monthly rows.

    Runs sharing a month/year are summed (a client can have more than one run in
    a period). Only periods that actually have runs are returned — a two-month-old
    tenant gets two points, not four blanks padding the axis.

    Each row: ``{label, full_label, year, month, net, cost, workers, runs}``
    where ``cost`` is the employer's total outlay (gross + employer SSF) and
    ``net`` is what workers receive.
    """
    buckets = {}
    for run in runs:
        key = period_key(run)
        bucket = buckets.setdefault(
            key,
            {
                "label": _short_month(run.month),
                "full_label": f"{run.month} {run.year}",
                "year": run.year,
                "month": run.month,
                "net": 0.0,
                "cost": 0.0,
                "workers": 0,
                "runs": 0,
            },
        )
        bucket["net"] += run.total_net_pay or 0
        bucket["cost"] += (run.total_gross_pay or 0) + (run.total_ssnit_employer or 0)
        bucket["workers"] += run.total_workers or 0
        bucket["runs"] += 1
    ordered = [buckets[key] for key in sorted(buckets)]
    return ordered[-periods:] if periods else ordered


def client_dashboard_analytics(runs, employee_count, expense_total, periods=TREND_PERIODS):
    """The tenant dashboard's analytics bundle.

    ``runs`` is the company's runs (any order — sorted here), ``employee_count``
    its roster size, ``expense_total`` its recorded expenses. All three are
    passed in already tenant-scoped so this function never queries and cannot
    leak across tenants.

    Returns ``{trend, cost_by_month, cost_mix, stats, has_data}``:

      trend         — net pay per month, for the line chart
      cost_by_month — employer cost per month, for the bar chart
      cost_mix      — payroll vs expenses, for the donut
      stats         — the quick-stat card figures
      has_data      — False when the company has no runs yet, so the template
                      shows an empty state instead of blank axes
    """
    rows = monthly_totals(runs, periods=periods)
    latest = rows[-1] if rows else None
    previous = rows[-2] if len(rows) > 1 else None

    current_payroll = latest["net"] if latest else 0
    current_workers = latest["workers"] if latest else 0
    # Average take-home for the latest period — per worker paid in that period,
    # not per person on the roster, so a partly-paid month isn't understated.
    average_net = (current_payroll / current_workers) if current_workers else 0
    payroll_total = sum(row["net"] for row in rows)

    return {
        "has_data": bool(rows),
        "trend": _as_points(rows, "net"),
        "cost_by_month": _as_points(rows, "cost"),
        "cost_mix": cost_mix(payroll_total, expense_total),
        "stats": {
            "employees": employee_count,
            "current_payroll": current_payroll,
            "current_period": latest["full_label"] if latest else None,
            "expenses": expense_total,
            "average_net": average_net,
            "payroll_change": pct_change(
                current_payroll, previous["net"] if previous else None
            ),
            "previous_period": previous["full_label"] if previous else None,
        },
    }


def cost_mix(payroll_total, expense_total):
    """Payroll vs expenses as donut slices over the periods charted.

    Returns ``{slices: [{label, value, pct, tone}], total}``. ``pct`` is each
    slice's share of the total; ``tone`` is a semantic name the macro maps to a
    brand colour. Both zero -> an empty slice list, so the macro renders the
    track ring only."""
    total = (payroll_total or 0) + (expense_total or 0)
    if total <= 0:
        return {"slices": [], "total": 0}
    parts = (
        ("Payroll", payroll_total or 0, "brand"),
        ("Expenses", expense_total or 0, "accent"),
    )
    return {
        "slices": [
            {
                "label": label,
                "value": value,
                "pct": round(value / total * 100, 2),
                "tone": tone,
            }
            for label, value, tone in parts
            if value > 0
        ],
        "total": total,
    }


# Slice tones the chart macros know how to colour, cycled for a breakdown with
# more categories than named tones. Presentation only.
BREAKDOWN_TONES = ("brand", "accent", "deep", "soft", "muted")


def category_breakdown(expenses, tones=BREAKDOWN_TONES):
    """Expenses grouped by category, largest first, in donut-slice shape.

    ``{slices: [{label, value, pct, tone}], total}`` — the same contract as
    :func:`cost_mix`, so the donut macro renders it with no changes. Categories
    summing to zero yield an empty slice list rather than a divide-by-zero.
    """
    buckets = {}
    for expense in expenses:
        label = (getattr(expense, "category", None) or "Uncategorised").strip() or "Uncategorised"
        buckets[label] = buckets.get(label, 0.0) + (getattr(expense, "amount", 0) or 0)
    total = sum(buckets.values())
    if total <= 0:
        return {"slices": [], "total": 0}
    ordered = sorted(buckets.items(), key=lambda pair: pair[1], reverse=True)
    return {
        "slices": [
            {
                "label": label,
                "value": value,
                "pct": round(value / total * 100, 2),
                "tone": tones[index % len(tones)],
            }
            for index, (label, value) in enumerate(ordered)
            if value > 0
        ],
        "total": total,
    }


def expense_summary(expenses, today=None):
    """Headline figures for a company's expense ledger.

    ``{total, monthly_total, monthly_label, count, breakdown, by_month}``:

      total         — every recorded expense
      monthly_total — the CURRENT calendar month only (the figure a manager
                      checks against this month's budget)
      breakdown     — :func:`category_breakdown`, donut-ready
      by_month      — the last :data:`TREND_PERIODS` months as bar-chart points

    ``expenses`` is passed in already tenant-scoped, so this never queries and
    cannot leak across tenants — the same contract as
    :func:`client_dashboard_analytics`.
    """
    from datetime import date as _date

    today = today or _date.today()
    rows = list(expenses)
    total = sum((expense.amount or 0) for expense in rows)

    monthly_total = sum(
        (expense.amount or 0)
        for expense in rows
        if expense.expense_date
        and expense.expense_date.year == today.year
        and expense.expense_date.month == today.month
    )

    # Month buckets keyed chronologically, so a December/January boundary orders
    # correctly rather than alphabetically by month name.
    buckets = {}
    for expense in rows:
        when = expense.expense_date
        if not when:
            continue
        key = (when.year, when.month)
        bucket = buckets.setdefault(
            key,
            {
                "label": month_name[when.month][:3],
                "full_label": f"{month_name[when.month]} {when.year}",
                "amount": 0.0,
            },
        )
        bucket["amount"] += expense.amount or 0
    ordered = [buckets[key] for key in sorted(buckets)][-TREND_PERIODS:]

    return {
        "total": total,
        "monthly_total": monthly_total,
        "monthly_label": f"{month_name[today.month]} {today.year}",
        "count": len(rows),
        "breakdown": category_breakdown(rows),
        "by_month": _as_points(ordered, "amount"),
    }


def pct_change(current, previous):
    """Signed fractional change vs ``previous``, or None when there is no
    baseline to compare against (no previous period, or a zero baseline — a
    percentage change from zero is undefined, not infinite)."""
    if previous in (None, 0):
        return None
    return ((current or 0) - previous) / abs(previous)


def client_cost_trend(runs, periods=TREND_PERIODS):
    """Per-client cost history for the operator dashboard's sparkline.

    ``{points, change, window_change, periods_covered}``:

      points        — net-pay points ready for the sparkline macro
      change        — signed fractional change vs the immediately previous
                      period (the "is this month unusual" question)
      window_change — signed change across the whole charted window (the
                      "where is this client heading" question)

    Both are None with fewer than two periods, where a trend is not yet
    meaningful — the caller renders a dash rather than a fake 0%."""
    rows = monthly_totals(runs, periods=periods)
    points = _as_points(rows, "net")
    multi = len(rows) > 1
    return {
        "points": points,
        "change": pct_change(rows[-1]["net"], rows[-2]["net"]) if multi else None,
        "window_change": pct_change(rows[-1]["net"], rows[0]["net"]) if multi else None,
        "periods_covered": len(rows),
    }


def totals_trend(runs, periods=TREND_PERIODS):
    """Platform-wide cost trend across every client, for the operator dashboard's
    header sparkline. Same shape as :func:`client_cost_trend`; runs from
    different clients in the same month are summed into one point."""
    return client_cost_trend(runs, periods=periods)


# --- Platform (operator) executive overview ---------------------------------
# The operator dashboard's counterpart to client_dashboard_analytics: the same
# lightweight, per-request, no-cache approach, aggregated ACROSS tenants rather
# than within one. Deliberately not a BI layer — every figure is a plain
# aggregate of columns the models already carry.

# Run status -> a donut tone name defined in tokens.css. Presentation only; the
# status vocabulary itself lives in app/payroll_status.py.
STATUS_TONES = {
    "Approved": "green",
    "Processed": "brand",
    "Held": "warn",
    "Pending Approval": "accent",
    "Submitted": "deep",
    "Auto-Accepted": "deep",
    "Draft": "muted",
    "Rejected": "danger",
}

# How many companies the "by payroll cost" ranking shows. Enough to see the
# shape of the book at card height without turning the card into a second
# client list.
TOP_CLIENT_LIMIT = 6


def status_distribution(runs, tones=STATUS_TONES):
    """Payroll runs grouped by status, in donut-slice shape.

    Values are run COUNTS, not money — render with ``formatter="count"``.
    Ordered largest-first so the dominant state reads first."""
    buckets = {}
    for run in runs:
        label = run.status or "Unknown"
        buckets[label] = buckets.get(label, 0) + 1
    total = sum(buckets.values())
    if total <= 0:
        return {"slices": [], "total": 0}
    ordered = sorted(buckets.items(), key=lambda pair: pair[1], reverse=True)
    return {
        "slices": [
            {
                "label": label,
                "value": count,
                "pct": round(count / total * 100, 2),
                "tone": tones.get(label, "soft"),
            }
            for label, count in ordered
        ],
        "total": total,
    }


def client_growth(clients, cutoff=None, periods=TREND_PERIODS):
    """Cumulative companies onboarded per month, as line-chart points.

    Cumulative rather than per-month because "client growth" is the size of the
    book over time, not the intake of any one month — a month with no signings
    should hold the line flat, not drop it to zero. Companies with no
    ``created_at`` are counted in the opening balance so the final point always
    equals the real company count.

    ``cutoff`` is a ``(year, month)`` tuple; months after it are dropped, so a
    period-scoped dashboard never charts months that had not happened yet in the
    view the operator selected."""
    dated = [c for c in clients if getattr(c, "created_at", None)]
    undated = len(clients) - len(dated)

    buckets = {}
    for company in dated:
        created = company.created_at
        key = (created.year, created.month)
        if cutoff and key > cutoff:
            continue
        bucket = buckets.setdefault(
            key,
            {
                "label": month_name[created.month][:3],
                "full_label": f"{month_name[created.month]} {created.year}",
                "new": 0,
            },
        )
        bucket["new"] += 1

    running = undated
    rows = []
    for key in sorted(buckets):
        running += buckets[key]["new"]
        rows.append({**buckets[key], "total": running})
    return _as_points(rows[-periods:] if periods else rows, "total")


def platform_dashboard_analytics(
    clients, expense_total, active_employees, cutoff=None, periods=TREND_PERIODS
):
    """The operator dashboard's executive bundle.

    ``clients`` is every :class:`~app.models.ClientCompany` with its
    ``employees`` and ``payroll_runs`` relationships already loaded — the caller
    eager-loads them once, so nothing here issues a query and the whole overview
    costs no extra round trips. ``expense_total`` and ``active_employees`` are
    passed in for the same reason: the dashboard route already counts them.

    ``cutoff`` is a ``(year, month)`` tuple (the selected period) that truncates
    every time series, matching the rest of the period-scoped dashboard.

    Returns ``{has_data, revenue_trend, companies_by_cost, status_mix,
    growth_trend, stats, top_clients}``.
    """
    runs = [run for client in clients for run in client.payroll_runs]
    if cutoff:
        runs = [run for run in runs if period_key(run) <= cutoff]

    rows = monthly_totals(runs, periods=periods)
    charted_periods = {(row["year"], MONTH_INDEX.get(row["month"], 0)) for row in rows}

    # Per-company payroll expenditure over exactly the charted window, so the
    # ranking and the trend above it describe the same months.
    ranked = []
    for client in clients:
        window_runs = [
            run
            for run in client.payroll_runs
            if period_key(run) in charted_periods
            and (cutoff is None or period_key(run) <= cutoff)
        ]
        ranked.append(
            {
                "label": client.company_code or client.name,
                "full_label": client.name,
                "cost": sum(
                    (run.total_gross_pay or 0) + (run.total_ssnit_employer or 0)
                    for run in window_runs
                ),
            }
        )
    ranked.sort(key=lambda row: row["cost"], reverse=True)
    companies_by_cost = _as_points(ranked[:TOP_CLIENT_LIMIT], "cost")

    payroll_total = sum((run.total_net_pay or 0) for run in runs)
    active_companies = sum(1 for client in clients if client.status == "Active")

    return {
        "has_data": bool(runs),
        "revenue_trend": _as_points(rows, "net"),
        "companies_by_cost": companies_by_cost,
        "status_mix": status_distribution(runs),
        "growth_trend": client_growth(clients, cutoff=cutoff, periods=periods),
        "stats": {
            "active_companies": active_companies,
            "active_employees": active_employees,
            "payroll_runs": len(runs),
            "payroll_total": payroll_total,
            "expense_total": expense_total,
            # Per ACTIVE company: an inactive tenant with no runs would otherwise
            # drag the average down and read as a decline in the book.
            "average_per_company": (payroll_total / active_companies)
            if active_companies
            else 0,
        },
        "top_clients": top_clients(clients, cutoff=cutoff),
    }


def top_clients(clients, cutoff=None, limit=TOP_CLIENT_LIMIT):
    """The Top Clients table: ``{company, code, employees, payroll, last_run,
    last_run_status, status}`` per row, ranked by payroll value.

    ``payroll`` is the company's lifetime net payroll (up to ``cutoff``), and
    ``last_run`` its most recent period — the two questions an executive asks of
    a client row. Computed from the already-loaded relationships."""
    rows = []
    for client in clients:
        runs = list(client.payroll_runs)
        if cutoff:
            runs = [run for run in runs if period_key(run) <= cutoff]
        latest = max(runs, key=period_key) if runs else None
        rows.append(
            {
                "company": client.name,
                "code": client.company_code,
                "employees": sum(
                    1 for employee in client.employees if employee.status == "Active"
                ),
                "payroll": sum((run.total_net_pay or 0) for run in runs),
                "last_run": f"{latest.month} {latest.year}" if latest else None,
                "last_run_status": latest.status if latest else None,
                "status": client.status,
            }
        )
    rows.sort(key=lambda row: row["payroll"], reverse=True)
    return rows[:limit]


def up_to_period(runs, year, month):
    """``runs`` filtered to periods at or before (``year``, ``month``).

    The operator dashboard is period-scoped: when an operator looks back at
    March, the trend must end at March rather than running on through today, or
    the sparkline would show months that hadn't happened yet in the view they
    selected."""
    cutoff = (year or 0, MONTH_INDEX.get(month, 0))
    return [run for run in runs if period_key(run) <= cutoff]
