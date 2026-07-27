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


def up_to_period(runs, year, month):
    """``runs`` filtered to periods at or before (``year``, ``month``).

    The operator dashboard is period-scoped: when an operator looks back at
    March, the trend must end at March rather than running on through today, or
    the sparkline would show months that hadn't happened yet in the view they
    selected."""
    cutoff = (year or 0, MONTH_INDEX.get(month, 0))
    return [run for run in runs if period_key(run) <= cutoff]
