"""Operator dashboard view model — the platform's decision surface.

Mirrors :mod:`app.dashboard`'s role for the book-wide (cross-tenant) view: a
composition layer only, no new query and no new number. Every figure below is
already computed in ``app/routes.py::dashboard()`` (platform_dashboard_analytics,
risk_summary, the period-scoped counts) — this module only reshapes those
already-fetched values into the ``{kpi}`` / ``{signal}`` contracts
``macros/dashboard.html`` renders, so the operator workspace can reuse the exact
same components as the company portal instead of a second, divergent set.

**Tier 1 (the KPI band).** Same rule :func:`app.dashboard.executive_kpis`
states for the company portal — one tile per genuinely different question, and
a repeat of an existing question does not get a second tile. Book-wide, that is
five questions, not four: what is payroll costing across the book, how big is
the workforce, how many companies are on it, what else are they spending, and
is Payrolla actually delivering payslips (the product's stated differentiator
over "payslip sits in a portal" — see the comment on ``delivery_rate`` in
``routes.py``). Everything that used to be a sixth-through-sixteenth tile
(pending approvals, held payrolls, validation warnings, PAYE/SSNIT) is a
*signal* below instead, not a number tile — the same reasoning that already
keeps run status out of the tenant KPI band: a count that implies an action
belongs next to that action, not floating in a grid of unrelated numbers.

**Tier 2 (the operator's attention column).** :func:`platform_risk_signals` and
:func:`statutory_summary` shape the right rail: what needs a decision, and what
the period owes the state.

**Tier 3 (the workspace).** :func:`company_rows` consolidates the two
overlapping per-company tables this page used to render into the single table an
operator scans to find the company that needs them.
"""

from app.analytics import TOP_CLIENT_LIMIT


def period_scoped_trend(portfolio_trend, period_label):
    """Blank the trend's ``change`` unless the trend actually ENDS at the
    period the page is reporting on.

    ``portfolio_trend`` is built from :func:`app.analytics.monthly_totals`,
    which returns only the periods that HAVE runs — so selecting a month with
    no payroll (a future month, or a quiet one) leaves the series ending at the
    last month that did have some. Its ``change`` then describes that older
    pair of months, while every figure beside it describes the selected one.

    On the operator dashboard that rendered as ``GH₵ 0.00`` under a green
    ``▲ +3.3% vs previous month``: a headline of zero and a rise, side by side,
    both true of different questions. A delta that does not describe the number
    it sits under is worse than no delta, so it becomes None and the macros
    draw the dash they already draw when there is no baseline.

    Returns a new dict; the caller's original is not mutated, and ``points`` is
    untouched — the sparkline's history is still real history.
    """
    points = portfolio_trend.get("points") if portfolio_trend else None
    ends_here = bool(points) and points[-1].get("full_label") == period_label
    return {**portfolio_trend, "change": portfolio_trend["change"] if ends_here else None}


def platform_kpis(
    analytics,
    current_month_total,
    portfolio_trend,
    total_clients,
    active_employees,
    total_employees,
    total_expenses,
    delivery_rate,
    payslips_delivered,
    payslips_total,
    period_label,
):
    """The five Tier-1 tiles: ``[{key, label, value, formatter, delta, caption,
    endpoint, trend}]``, same contract :func:`app.dashboard.executive_kpis`
    returns so ``macros/dashboard.html::kpi_card`` renders either unchanged.

    ``portfolio_trend`` is the dict :func:`app.analytics.totals_trend` returns
    (``{points, change, window_change, periods_covered}``) — unpacked here into
    the plain ``trend`` list + signed ``delta`` ``kpi_card`` expects, so this is
    the one place that shape gets translated.
    """
    stats = analytics["stats"]
    trend_points = portfolio_trend["points"] if portfolio_trend else []
    trend_delta = portfolio_trend["change"] if portfolio_trend else None

    return [
        {
            "key": "payroll_cost",
            "label": "Payroll cost",
            "value": current_month_total,
            "formatter": "cedis",
            "delta": trend_delta,
            "caption": f"All clients · {period_label}",
            "compare": "previous month",
            "trend": trend_points,
            "endpoint": "payroll.runs",
        },
        {
            "key": "workforce",
            "label": "Workforce",
            "value": active_employees,
            "formatter": "count",
            "delta": None,
            "caption": f"Active · {total_employees} total on the book",
            "compare": None,
            "trend": [],
            "endpoint": "main.clients",
        },
        {
            # Active, not total — same pattern as the Workforce tile above
            # (active headline, total in the caption), so a company that has
            # gone quiet does not keep inflating the number an MD glances at.
            "key": "clients",
            "label": "Client companies",
            "value": stats["active_companies"],
            "formatter": "count",
            "delta": None,
            "caption": f"Active · {total_clients} total on the book",
            "compare": None,
            "trend": [],
            "endpoint": "main.clients",
        },
        {
            "key": "expenses",
            "label": "Client expenses",
            "value": total_expenses,
            "formatter": "cedis",
            "delta": None,
            "caption": "All-time · across the book",
            "compare": None,
            "trend": [],
            "endpoint": "main.clients",
        },
        {
            # `delivery_rate` is None when no run in the period has reached a
            # status that may be distributed — see the comment on its
            # computation in routes.py. The tile then reads as a dash with the
            # reason in its caption rather than as 0%, because "we have not
            # been allowed to send anything yet" and "we tried and failed to
            # send everything" are opposite facts and must not share a face.
            "key": "delivery",
            "label": "Payslip delivery",
            "value": delivery_rate,
            "formatter": "percent",
            "delta": None,
            "caption": (
                f"{payslips_delivered} of {payslips_total} sent · {period_label}"
                if delivery_rate is not None
                else f"No approved payroll to send yet · {period_label}"
            ),
            "compare": None,
            "trend": [],
            "endpoint": "distribution.analytics",
        },
    ]


# --- Risk & action -------------------------------------------------------

# Held payrolls reuse the tenant vocabulary's tone for the same status
# (macros/dashboard.html::_STATUS_TONE has Held -> warn), so a colour never
# means something different on this dashboard than it does on the run it links
# to. The three tones below are used strictly by what the item demands of the
# reader, never for variety: `warn` is work that needs attention, `info` is
# ordinary pending work moving through the pipeline on its own, and `ok` is a
# real all-clear. Nothing here is `danger` — a danger tone is reserved for a
# condition that blocks payroll outright, and none of these three do.

# Ceiling on rendered signals. Three kinds exist today, so this never truncates
# in practice; it is the guarantee that a future fourth and fifth signal cannot
# turn the operator's attention column into a list nobody reads. The panel links
# to the full risk queue either way.
MAX_SIGNALS = 4


def platform_risk_signals(held_count, pending_approvals, warning_count,
                          awaiting_credentials=0, limit=MAX_SIGNALS):
    """Book-wide action items, worst first: ``[{tone, title, detail, action,
    endpoint}]``, the contract ``macros/dashboard.html::signal`` renders.

    These three counts are already computed once in ``routes.py`` (from
    :func:`app.risk.risk_summary` and the period-scoped queries) — this only
    reshapes them, so the signal panel, the KPI band and the risk queue itself
    can never disagree. An all-clear returns one ``ok`` signal rather than an
    empty panel, same principle as the tenant risk panel: silence is not the
    same as reassurance.

    Every item carries what happened (``title``), why it matters (``detail``)
    and what to do about it (``action``) — a count on its own tells an operator
    nothing they can act on.
    """
    signals = []

    if held_count:
        signals.append({
            "tone": "warn",
            "title": f"{held_count} payroll{'' if held_count == 1 else 's'} held for risk review",
            "detail": "The risk gate stopped these before approval. They stay unpaid until someone clears them.",
            "action": "Review queue",
            "endpoint": "oversight.risk_queue",
        })

    if warning_count:
        signals.append({
            "tone": "warn",
            "title": f"{warning_count} validation warning{'' if warning_count == 1 else 's'}",
            "detail": "Flagged payroll lines across current runs need a second look before sign-off.",
            "action": "Open runs",
            "endpoint": "payroll.runs",
        })

    if pending_approvals:
        signals.append({
            "tone": "info",
            "title": f"{pending_approvals} payroll{'' if pending_approvals == 1 else 's'} awaiting approval",
            "detail": "Submitted and past the risk gate. Ordinary queue work, waiting on Payrolla sign-off.",
            "action": "Approve",
            "endpoint": "payroll.runs",
        })

    # Onboarded but unable to sign in. Not a payroll risk — a delivery one, and
    # invisible until someone at the company complains, because the onboarding
    # flow completes successfully without ever minting a credential.
    if awaiting_credentials:
        signals.append({
            "tone": "info",
            "title": f"{awaiting_credentials} compan{'y' if awaiting_credentials == 1 else 'ies'} awaiting credentials",
            "detail": "Onboarded, with no login identity provisioned yet — nobody there can sign in.",
            "action": "Open companies",
            "endpoint": "main.clients",
        })

    if not signals:
        signals.append({
            "tone": "ok",
            "title": "No urgent payroll risks",
            "detail": "All current runs are within configured review thresholds, with nothing awaiting approval.",
            "action": "",
            "endpoint": "",
        })

    return signals[:limit]


def statutory_summary(paye_total, ssnit_total, period_label):
    """PAYE and SSNIT for the period as a compact two-figure strip.

    Deliberately NOT KPI tiles. These are obligations an operator reconciles at
    filing time, not a question they scan the dashboard to answer, so they ride
    along the foot of the risk panel where the period's other liabilities are —
    close enough to be found, quiet enough not to compete with the five tiles.

    ``ssnit_total`` is the combined employee (5.5%) + employer (13%) SSF the
    caller already sums, i.e. the amount actually remitted, which is why it is
    labelled *payable* rather than *deducted*.
    """
    # `figures`, not `items`: Jinja resolves a dict's attribute access to the
    # method of that name first, so `statutory.items` would hand the template
    # dict.items and fail to iterate.
    return {
        "period": period_label,
        "figures": [
            {"label": "PAYE", "value": paye_total},
            {"label": "SSNIT Payable", "value": ssnit_total},
        ],
    }


# --- Company payroll overview ----------------------------------------------

# How many companies the consolidated table lists before deferring to the full
# client list. Bound to the ranking chart's limit rather than repeating the
# number, so the table and the chart above it stay the same LENGTH and the page
# never reads as a top-6 beside a top-8. They are not necessarily the same six
# companies: the chart ranks by cost over the charted window, the table by cost
# in the selected month.
COMPANY_ROW_LIMIT = TOP_CLIENT_LIMIT


def company_rows(client_costs, limit=COMPANY_ROW_LIMIT):
    """The 'Company payroll overview' rows, biggest spender first.

    Replaces the two overlapping tables this dashboard used to carry — Top
    Clients and Payroll Cost Per Client — which ranked the same companies by two
    different measures of the same money and made an operator read both to learn
    one thing. One row now answers the five questions actually asked of a
    company: how many workers, what this period cost, which way that is moving,
    where its latest run stands, and whether anything is waiting on me.

    ``client_costs`` is the list :func:`app.routes.dashboard` already builds from
    the eager-loaded companies — no query and no new figure here. Returns
    ``(rows, total)`` so the caller can say "showing 6 of 12" and link out for
    the rest; per-company history belongs on the company page, not in a
    dashboard row.
    """
    rows = []
    for item in client_costs:
        runs = item["runs"]
        # Ordered by id, not created_at: SQLite hands back naive datetimes for a
        # tz-aware default (see app.events.as_utc), and a row created earlier in
        # the same request is still aware — max() over the mix raises. Ids are
        # monotonic and answer the same question.
        latest = max(runs, key=lambda run: run.id) if runs else None
        trend = item["trend"]
        rows.append(
            {
                "company": item["client"],
                "workers": item["workers"],
                "cost": item["payroll_cost"],
                "trend": trend,
                # Movement is stated as a signed percentage in every case; the
                # sparkline beside it is additive and only drawn where there is
                # more than one period to draw.
                "change": trend["change"] if trend else None,
                "has_trend": bool(trend and trend["periods_covered"] > 1),
                "status": latest.status if latest else "No run",
                "pending": item["pending"],
            }
        )
    rows.sort(key=lambda row: row["cost"], reverse=True)
    return rows[:limit], len(rows)
