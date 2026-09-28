"""Operator navigation — declared once, in route metadata.

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
Payslips. That is deliberate: nav is for the handful of places you go without
being sent, not a table of contents for the app.

**Slots.** The sidebar is now a top bar (base.html), which has two regions
rather than one list: the centred primary nav, and the right utility cluster.
Notifications sits in the cluster because it is *ambient* — a standing count you
glance at, not a destination you navigate to — which is the same call the tenant
portal already made for its own bell. The split is declared here rather than
tested for in the template, for the same reason active state is: a template that
asks `if item.key == "notifications"` is one rename away from being wrong.
"""
from collections import namedtuple

from app.permissions import (
    can_manage_statutory,
    can_operate_payroll,
    can_view_audit,
)

# `blueprints` is the set of blueprint names this item owns for active-state
# purposes. `visible` is a predicate over the operator's role, or None for
# "everyone on the platform plane". `glyph` names an icon in the SHARED inline
# set (templates/macros/ui.html), not a Bootstrap Icons class: the bar draws the
# same glyph family as the tenant bar, and the two must not drift. `slot` is
# "primary" (in the bar) or "utility" (in the right cluster).
NavItem = namedtuple("NavItem", "key label glyph endpoint blueprints visible slot")

# One-word domain nouns, the same register the tenant bar uses. They were
# longer — "Client Companies", "Payroll Runs", "Expenses & Audit", "Statutory
# Rates" — because they were written for a 280px sidebar, which has room for a
# phrase. A bar does not: those six labels needed 848px of row against the
# tenant's ~640, which forced the drawer to appear on any screen under 1400px
# and pushed the centred nav ~118px off the midline at 1440. Shortening them is
# what lets both shells share one 1100px breakpoint.
#
# A LABEL is not a TITLE. The pages keep their full names — /clients is still
# headed "Client Companies", the audit page is still "Expenses & Audit" — and so
# do the breadcrumbs that point at them, which are spelled in the templates and
# have never come from here. A nav label answers "where do I go", a title
# answers "where am I", and only the first one has to fit in a row of six.
NAV = (
    NavItem("dashboard", "Dashboard", "grid", "main.dashboard",
            frozenset({"main"}), None, "primary"),
    NavItem("clients", "Clients", "building", "main.clients",
            frozenset({"employees"}), None, "primary"),
    NavItem("payroll", "Payroll", "wallet", "payroll.runs",
            frozenset({"payroll", "oversight"}), can_operate_payroll, "primary"),
    NavItem("payslips", "Payslips", "file-text", "payslip.index",
            frozenset({"payslip", "distribution"}), can_operate_payroll, "primary"),
    # "Audit", not "Expenses": the page leads with the read-only audit trail and
    # recorded spend sits inside it. The dashboard's "Expenses & audit" quick
    # action is still the one place the operator plane says "Expenses" out loud.
    NavItem("audit", "Audit", "list-check", "audit.audit_trail",
            frozenset({"audit"}), can_view_audit, "primary"),
    NavItem("statutory", "Statutory", "bank", "statutory.index",
            frozenset({"statutory"}), can_manage_statutory, "primary"),
    NavItem("notifications", "Notifications", "bell", "notifications.inbox",
            frozenset({"notifications"}), None, "utility"),
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
    """Every item this role may see, in order, both slots.

    Still the whole nav — the count this returns is what "the navigation is a
    fixed set of domain nouns" is measured against, regardless of which region
    of the bar each one is drawn in.
    """
    return [item for item in NAV if item.visible is None or item.visible(role)]


def primary_nav(role):
    """The items drawn in the centred bar."""
    return [item for item in visible_nav(role) if item.slot == "primary"]


def utility_nav(role):
    """The items drawn in the right utility cluster."""
    return [item for item in visible_nav(role) if item.slot == "utility"]


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
