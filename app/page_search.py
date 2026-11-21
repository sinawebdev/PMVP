"""Search that finds pages as well as records.

Typing "paye" into the search box used to find nothing, because search only
looked at company names and payslips. People think in tasks ("upload payroll",
"add a colleague", "tax bands"), so the search now also offers the page that
does the job, as a "Go to" group above the records.

Each page is listed once below with the words people might type for it. Which
pages a person is offered is NOT decided here: ``can_open`` reads the guards
already on the route (its role group, Payrolla-only, bureau-only, and the bureau
gate in app.bureau_scope). A page someone can't open is never suggested, and a
change to a route's guard changes what search offers without touching this
file. ``visible`` adds the nav's own rule for the few pages whose route guard is
wider than the nav (payroll pages are platform-wide, but the nav shows them only
to roles that run payroll).
"""
import re
from collections import namedtuple

from flask import current_app, url_for
from flask_login import current_user

from app.permissions import can_operate_payroll
from app.roles import is_bureau_user, normalise_role

# label: what the result says. endpoint: where it goes (no URL arguments).
# terms: other words for it. hint: one line under the label.
Page = namedtuple("Page", "label endpoint terms hint visible")


def _page(label, endpoint, terms, hint, visible=None):
    return Page(label, endpoint, terms, hint, visible)


OPERATOR_PAGES = (
    _page("Dashboard", "main.dashboard", "home overview start summary", "This month across your clients"),
    _page("Client companies", "main.clients", "clients companies customers employers employees workers staff roster applications access requests sign ups",
          "Every company, its employees and its payroll"),
    _page("Onboard a client company", "main.add_client", "add new client company create onboarding", "Add a company you run payroll for"),
    _page("Payroll runs", "payroll.runs", "runs payroll pay month months approvals approve history", "Every run, newest first", can_operate_payroll),
    _page("Upload a payroll", "payroll.new_run", "new run upload import excel spreadsheet workbook create start payroll", "Start a run from a spreadsheet"),
    _page("Download the payroll template", "payroll.payroll_template", "template spreadsheet excel blank workbook format columns", "A blank workbook in the format uploads expect"),
    _page("Risk queue", "oversight.risk_queue", "risk held flagged variance review checks hold", "Runs held for a second look", can_operate_payroll),
    _page("Payslips", "payslip.index", "payslips payslip pdf download generate print", "Generate and download payslips", can_operate_payroll),
    _page("Distribution monitor", "distribution.dashboard", "sending send delivery monitor sms email whatsapp queue", "Payslip sending across every client"),
    _page("Delivery history", "distribution.history", "sent delivered failed sms email whatsapp history messages delivery", "Every payslip sent, and whether it arrived"),
    _page("Delivery analytics", "distribution.analytics", "analytics delivery rates channels stats statistics", "Delivery rates by channel"),
    _page("Audit trail and expenses", "audit.audit_trail", "audit log trail history changes changed who expenses spend", "Who did what, and recorded spend"),
    _page("Statutory rates", "statutory.index", "statutory rates paye gra tax bands ssnit ssf tier pension rate versions", "PAYE bands and SSNIT rates in use"),
    _page("New statutory rate version", "statutory.new", "new rate version paye bands update change rates tax", "Record a change to PAYE or SSNIT"),
    _page("Desktop licences", "licences.ledger", "licences licence license desktop trial keys app", "Licences issued for the desktop app"),
    _page("Database health", "main.db_health", "database health db status ops connection", "Is the database reachable and current"),
    _page("Team", "team.bureau_team", "team colleagues colleague staff users user people logins login invite access members officer administrator admin",
          "Who can sign in, and invite colleagues"),
    _page("Notifications", "notifications.inbox", "notifications notification alerts inbox bell messages", "Things that need your attention"),
    _page("Log out", "auth.logout", "log out sign out logout signout exit leave", "End this session"),
)

COMPANY_PAGES = (
    _page("Dashboard", "main.company_dashboard", "home overview start summary", "Your company this month"),
    _page("Employees", "client.employees", "employees employee staff workers worker roster people list", "Everyone on your roster"),
    _page("Add an employee", "client.employee_add", "add new employee hire worker staff create", "Put one person on the roster"),
    _page("Import employees from Excel", "client.employee_import", "import bulk excel upload roster spreadsheet employees many", "Add or update many employees at once"),
    _page("Payroll runs", "client.runs", "runs payroll pay month months history payslips send", "Every run, newest first"),
    _page("Upload a payroll", "client.run_upload", "upload payroll import excel spreadsheet new run monthly start", "Start this month's run from a spreadsheet"),
    _page("Download the payroll template", "client.payroll_template", "template payroll excel blank workbook format columns", "A blank workbook in the format uploads expect"),
    _page("Statutory rates", "client.statutory", "statutory rates paye gra tax bands ssnit ssf tier pension", "PAYE bands and SSNIT rates in use"),
    _page("Expenses", "client.expenses", "expenses expense spend receipts costs", "Your recorded operating spend"),
    _page("Record an expense", "client.expense_add", "add expense receipt record new spend", "Log a cost with its receipt"),
    _page("Audit trail", "client.audit", "audit log trail history changes changed who", "Who did what, and when"),
    _page("Team", "team.company_team", "team colleagues colleague staff users user people logins login invite access members preparer administrator admin",
          "Who can sign in, and invite colleagues"),
    _page("Email branding", "client.branding", "branding brand logo colour color email sender reply", "How payslip emails look"),
    _page("Notifications", "notifications.inbox", "notifications notification alerts inbox bell messages", "Things that need your attention"),
    _page("Log out", "auth.logout", "log out sign out logout signout exit leave", "End this session"),
)

MAX_PAGES = 6


def _words(text):
    return re.findall(r"[a-z0-9]+", (text or "").lower())


def can_open(endpoint, user=None):
    """Would the route let ``user`` in? Read from the route's own guards."""
    from app.bureau_scope import BUREAU_ENDPOINTS, SHARED_ENDPOINTS, is_payrolla_staff

    user = user if user is not None else current_user
    view = current_app.view_functions.get(endpoint)
    if view is None or not getattr(user, "is_authenticated", False):
        return False
    role = normalise_role(getattr(user, "role", None))
    roles = getattr(view, "_required_roles", None)
    if roles is not None and role not in roles:
        return False
    tenant_roles = getattr(view, "_required_tenant_roles", None)
    if tenant_roles is not None and role not in tenant_roles:
        return False
    if getattr(view, "_payrolla_only", False) and not is_payrolla_staff(user):
        return False
    if getattr(view, "_bureau_only", False) and getattr(user, "bureau", None) is None:
        return False
    if is_bureau_user(user) and endpoint not in SHARED_ENDPOINTS and endpoint not in BUREAU_ENDPOINTS \
            and not getattr(view, "_bureau_scoped", False):
        return False
    return True


def pages_for(user=None):
    """The pages this person may open, in their plane, in listing order."""
    user = user if user is not None else current_user
    tenant = getattr(user, "client_company_id", None) is not None
    pages = COMPANY_PAGES if tenant else OPERATOR_PAGES
    role = getattr(user, "role", None)
    return [p for p in pages if (p.visible is None or p.visible(role)) and can_open(p.endpoint, user)]


def _score(page, query_words):
    label = _words(page.label)
    terms = _words(page.terms)
    score = 0
    for word in query_words:
        if any(w.startswith(word) for w in label):
            score += 3
        elif any(w.startswith(word) for w in terms):
            score += 2
        else:
            return 0
    if " ".join(label).startswith(" ".join(query_words)):
        score += 2
    return score


def find_pages(query, user=None, limit=MAX_PAGES):
    """Pages matching every word of ``query``, best first. Each is a dict
    with label, hint and url, ready for a template or JSON."""
    words = _words(query)[:6]
    if not words:
        return []
    scored = []
    for order, page in enumerate(pages_for(user)):
        score = _score(page, words)
        if score:
            scored.append((-score, order, page))
    scored.sort()
    return [
        {"label": p.label, "hint": p.hint, "url": url_for(p.endpoint)}
        for _, _, p in scored[:limit]
    ]


# --- Records ------------------------------------------------------------------
# Search still finds things, not just pages. Every query goes through the same
# scoping as the rest of the app: operator_query for operators (so a bureau sees
# only its own companies), tenant_query for a company's people.


def _best_first(query, *columns):
    """An ORDER BY term that puts a record whose name (or a word in it) starts
    with the query ahead of one that only contains it. Typing "ko" should find
    Kojo Addai before Abena Darko, or the four suggestions fill up with the
    wrong people on any real roster."""
    from sqlalchemy import case, or_

    starts = or_(*(c.ilike(f"{query}%") for c in columns),
                 *(c.ilike(f"% {query}%") for c in columns))
    return case((starts, 0), else_=1)


def operator_records(query, clients=25, employees=25):
    """(client companies, employees) matching ``query`` for an operator."""
    from sqlalchemy import or_

    from app.bureau_scope import operator_query
    from app.models import ClientCompany, Employee
    from app.permissions import can_maintain_roster

    like = f"%{query}%"
    found_clients = (
        operator_query(ClientCompany)
        .filter(or_(ClientCompany.name.ilike(like), ClientCompany.company_code.ilike(like)))
        .order_by(_best_first(query, ClientCompany.name, ClientCompany.company_code), ClientCompany.name)
        .limit(clients)
        .all()
    )
    found_employees = []
    # The employee pages are roster pages; don't offer what the role can't open.
    if can_maintain_roster(getattr(current_user, "role", None)):
        found_employees = (
            operator_query(Employee)
            .filter(or_(Employee.full_name.ilike(like), Employee.staff_id.ilike(like)))
            .order_by(_best_first(query, Employee.full_name, Employee.staff_id), Employee.full_name)
            .limit(employees)
            .all()
        )
    return found_clients, found_employees


def company_employees(query, limit=25):
    """The signed-in company's employees matching ``query``."""
    from sqlalchemy import or_

    from app.models import Employee
    from app.tenancy import tenant_query

    like = f"%{query}%"
    return (
        tenant_query(Employee)
        .filter(or_(Employee.full_name.ilike(like), Employee.staff_id.ilike(like)))
        .order_by(_best_first(query, Employee.full_name, Employee.staff_id), Employee.full_name)
        .limit(limit)
        .all()
    )


MIN_RECORD_QUERY = 2


def suggestions(query, user=None):
    """What the search box offers while typing, as JSON-ready groups.

    Pages first (a short word like "tax" should land on the page), then a few
    records. Empty groups are left out.
    """
    user = user if user is not None else current_user
    query = (query or "").strip()[:100]
    groups = [{"label": "Go to", "items": find_pages(query, user)}]
    if len(query) >= MIN_RECORD_QUERY:
        if getattr(user, "client_company_id", None) is not None:
            groups.append({"label": "Employees", "items": [
                {"label": e.full_name, "hint": e.staff_id,
                 "url": url_for("client.employee_edit", emp_id=e.id)}
                for e in company_employees(query, limit=4)
            ]})
        else:
            clients, employees = operator_records(query, clients=3, employees=4)
            groups.append({"label": "Clients", "items": [
                {"label": c.name, "hint": c.company_code or "",
                 "url": url_for("main.client_detail", client_id=c.id)}
                for c in clients
            ]})
            groups.append({"label": "Employees", "items": [
                {"label": e.full_name,
                 "hint": " · ".join(x for x in (e.staff_id, e.client_company.name if e.client_company else "") if x),
                 "url": url_for("employees.edit", client_id=e.client_company_id, emp_id=e.id)}
                for e in employees
            ]})
    return {"groups": [g for g in groups if g["items"]]}
