"""Operator sidebar navigation — declared once, in route metadata.

Two problems this replaces (Phase 3, Task 3.2):

1. **Active state was string matching.** Each link carried its own
   ``path.startswith('/payroll')`` test in base.html, so adding a route meant
   editing the template to make the highlight work, and a URL rename silently
   broke it. Ownership is declared here by *blueprint*, which is metadata the
   route already has — a new route inside an existing blueprint lights the right
   item with no template change at all.

2. **The sidebar grew with the data.** It listed every client company, so it got
   longer as the business got bigger — a navigation surface whose length is a
   function of the customer count is not navigation. The client list belongs on
   the Client Companies page, which is built to page and filter it.

Seven top-level items, all domain nouns. Everything that used to be an eighth or
ninth item is still one click away from the item that owns it — Risk Queue and
the Approval Queue from Payroll Runs, Distribution Monitor and History from
Payslips. That is deliberate: a sidebar is for the handful of places you go
without being sent, not a table of contents for the app.
"""
from collections import namedtuple

from app.permissions import (
    can_manage_statutory,
    can_operate_payroll,
    can_view_audit,
)

# `blueprints` is the set of blueprint names this item owns for active-state
# purposes. `visible` is a predicate over the operator's role, or None for
# "everyone on the platform plane".
NavItem = namedtuple("NavItem", "key label icon endpoint blueprints visible")

NAV = (
    NavItem("dashboard", "Dashboard", "bi-speedometer2", "main.dashboard",
            frozenset({"main"}), None),
    NavItem("clients", "Client Companies", "bi-buildings", "main.clients",
            frozenset({"employees"}), None),
    NavItem("payroll", "Payroll Runs", "bi-cash-stack", "payroll.runs",
            frozenset({"payroll", "oversight"}), can_operate_payroll),
    NavItem("payslips", "Payslips", "bi-file-earmark-person", "payslip.index",
            frozenset({"payslip", "distribution"}), can_operate_payroll),
    NavItem("audit", "Expenses & Audit", "bi-activity", "audit.audit_trail",
            frozenset({"audit"}), can_view_audit),
    NavItem("statutory", "Statutory Rates", "bi-bank", "statutory.index",
            frozenset({"statutory"}), can_manage_statutory),
    NavItem("notifications", "Notifications", "bi-bell", "notifications.inbox",
            frozenset({"notifications"}), None),
)

# `main` owns both the dashboard and the client pages, so blueprint alone cannot
# separate them. These endpoints are re-pointed at the item that really owns them.
_ENDPOINT_OVERRIDES = {
    "main.clients": "clients",
    "main.client_detail": "clients",
    "main.add_client": "clients",
    "main.edit_client": "clients",
    "main.client_onboarding": "clients",
    "main.search": "clients",
}


def visible_nav(role):
    """The items this role may see, in order."""
    return [item for item in NAV if item.visible is None or item.visible(role)]


def active_nav_key(endpoint, blueprint):
    """Which nav item the current request belongs to, or None.

    Derived from the request's own routing metadata, so it keeps working when a
    URL changes and needs no maintenance when a route is added.
    """
    if endpoint and endpoint in _ENDPOINT_OVERRIDES:
        return _ENDPOINT_OVERRIDES[endpoint]
    if not blueprint:
        return None
    for item in NAV:
        if blueprint in item.blueprints:
            return item.key
    return None
