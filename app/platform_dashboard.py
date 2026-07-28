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
"""


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
            "key": "delivery",
            "label": "Payslip delivery",
            "value": delivery_rate,
            "formatter": "percent",
            "delta": None,
            "caption": f"{payslips_delivered} of {payslips_total} pushed · {period_label}",
            "compare": None,
            "trend": [],
            "endpoint": "distribution.analytics",
        },
    ]


# --- Risk & action -------------------------------------------------------

# Held payrolls and pending approvals reuse the tenant vocabulary's tone for
# the same statuses (macros/dashboard.html::_STATUS_TONE has Held -> warn), so
# a colour never means something different on this dashboard than it does on
# the run it links to.


def platform_risk_signals(held_count, pending_approvals, warning_count):
    """Book-wide action items, worst first: ``[{tone, title, detail, endpoint}]``,
    the exact contract ``macros/dashboard.html::signal`` renders.

    These three counts are already computed once in ``routes.py`` (from
    :func:`app.risk.risk_summary` and the period-scoped queries) for the KPI
    counters, the old Action Required rows and the Held panel — this only
    reshapes them, so the signal panel, the tiles it replaced and the risk
    queue itself can never disagree. An all-clear returns one ``ok`` signal
    rather than an empty panel, same principle as the tenant risk panel:
    silence is not the same as reassurance.
    """
    signals = []

    if held_count:
        signals.append({
            "tone": "warn",
            "title": f"{held_count} payroll{'' if held_count == 1 else 's'} held for risk review",
            "detail": "Held by the risk gate pending Payrolla review.",
            "endpoint": "oversight.risk_queue",
        })

    if pending_approvals:
        signals.append({
            "tone": "warn",
            "title": f"{pending_approvals} payroll{'' if pending_approvals == 1 else 's'} awaiting approval",
            "detail": "Submitted and cleared the risk gate; needs Payrolla sign-off.",
            "endpoint": "payroll.runs",
        })

    if warning_count:
        signals.append({
            "tone": "warn",
            "title": f"{warning_count} validation warning{'' if warning_count == 1 else 's'}",
            "detail": "Flagged payroll lines across current runs need a second look.",
            "endpoint": "payroll.runs",
        })

    if not signals:
        signals.append({
            "tone": "ok",
            "title": "Nothing waiting on you",
            "detail": "No held payrolls, pending approvals or validation warnings right now.",
            "endpoint": "",
        })

    return signals
