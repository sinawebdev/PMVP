"""The demo workspace: sample bureau and client logins on the live site.

Payrolla staff need to see exactly what a bureau and a client company see, and
the live site has no shell to seed anything (Render's free plan), so this is
driven from a staff-only page (app.demo_page). It creates, rebuilds and removes
one fixed shape of fake data:

* a payroll bureau, "Demo Payroll Bureau", with its own in-house company (the
  shape a self-run sign-up or Chrisnat has), and its bureau admin login;
* two client companies of that bureau, the first with a company admin and a
  payroll preparer login on the client portal;
* six months of calculated payroll per company (the real SalariedCalculator,
  the bureau's real rates), with expenses, delivered payslips and activity.

Three rules this module is built around:

**Only demo rows are ever deleted.** Every destructive step goes through the
guard (:func:`demo_bureau`, :func:`demo_companies`), which refuses before any
change unless exactly one bureau is flagged ``is_demo`` and the flagged
companies are exactly that bureau's companies. The final deletes filter on the
flag again and check the row count. Payrolla staff logins have neither a
company nor a bureau, so they can never be among the demo logins.

**Nothing is sent.** All contacts are made up, on RFC 2606 ``example.com``
addresses, but the real guarantee is app.distribution.demo: a demo company's
payslips never reach a provider. Login invites are the one real email, and the
page (not this module) sends them.

**Logins survive a refresh.** Refresh empties and rebuilds the data, but keeps
the logins (ids, passwords, invite state) so nobody has to accept a new invite.
"""

from datetime import timedelta

from app import db
from app.demo_data import (
    _recent_periods,
    build_activity,
    build_deliveries,
    build_employees,
    build_expenses,
    build_payroll_history,
)
from app.employee_audit import seeding
from app.models import (
    BATCH_CANCELLED,
    BATCH_QUEUED,
    BATCH_RUNNING,
    BATCH_SCHEDULED,
    AccessRequest,
    AuditTrail,
    Bureau,
    ClientCompany,
    DistributionBatch,
    DomainEvent,
    Notification,
    StatutoryRate,
    User,
)
from app.payroll_status import PROCESSED
from app.roles import BUREAU_ADMIN, CLIENT_ADMIN, CLIENT_PREPARER
from app.tenant_purge import _bulk_delete, purge_client_company, release_user_references

DEFAULT_MONTHS = 6

BUREAU_SPEC = {
    "name": "Demo Payroll Bureau",
    "code": "DEMO",
    "contact_email": "office@demo-bureau.example.com",
    "phone": "0200000000",
}

# (key, company fields, staff-id prefix, roster). "home" is the bureau's own
# company; "client_a" carries the client-portal logins. The "(Demo)" suffix
# keeps a real client's name free for the real client.
COMPANY_SPECS = (
    ("home", {
        "name": "Demo Payroll Bureau", "company_code": "DEMO",
        "contact_person": "Adjoa Quartey", "email": "office@demo-bureau.example.com",
        "phone": "0200000000", "location": "Accra", "service_type": "Payroll services",
    }, "DPB", [
        ("001", "Adjoa Mensima Quartey", "Payroll Manager", "Payroll", 5400),
        ("002", "Fiifi Baidoo", "Payroll Officer", "Payroll", 3600),
        ("003", "Nhyira Asamoah", "Client Accounts Officer", "Accounts", 3300),
        ("004", "Selorm Agbeko", "Office Administrator", "Administration", 2500),
    ]),
    ("client_a", {
        "name": "Volta Harbour Logistics (Demo)", "company_code": "DEMO-VOLTA",
        "contact_person": "Edem Kpodo", "email": "payroll@volta-harbour.example.com",
        "phone": "0200000100", "location": "Tema", "service_type": "Logistics",
    }, "VHL", [
        ("001", "Edem Kpodo", "Operations Manager", "Operations", 5600),
        ("002", "Delali Ahiable", "Fleet Coordinator", "Logistics", 3500),
        ("003", "Kekeli Dzorka", "Dispatch Clerk", "Logistics", 2300),
        ("004", "Senyo Amedzro", "Truck Driver", "Fleet", 2700),
        ("005", "Mawuli Fiagbe", "Truck Driver", "Fleet", 2700),
        ("006", "Esinam Gbeve", "Warehouse Supervisor", "Warehouse", 3200),
        ("007", "Yayra Dogbe", "Forklift Operator", "Warehouse", 2400),
        ("008", "Elikem Akakpo", "Accounts Officer", "Finance", 3400),
        ("009", "Dzifa Atsu", "HR Assistant", "People", 2600),
        ("010", "Kafui Nyamekye", "Security Officer", "Security", 2100),
    ]),
    ("client_b", {
        "name": "Asante Textiles (Demo)", "company_code": "DEMO-ASANTE",
        "contact_person": "Akwasi Bonsu", "email": "hr@asante-textiles.example.com",
        "phone": "0200000200", "location": "Kumasi", "service_type": "Manufacturing",
    }, "ATX", [
        ("001", "Akwasi Bonsu", "Production Manager", "Production", 5000),
        ("002", "Adwoa Frimpong", "Textile Designer", "Design", 3700),
        ("003", "Kwadwo Agyeman", "Loom Operator", "Production", 2500),
        ("004", "Afua Sarpong", "Dye House Technician", "Production", 2600),
        ("005", "Yaw Kyei", "Machine Mechanic", "Maintenance", 3000),
        ("006", "Abena Pokua", "Quality Controller", "Quality", 2900),
        ("007", "Kofi Duah", "Sales Representative", "Sales", 3100),
        ("008", "Akosua Nimako", "Stores Clerk", "Warehouse", 2300),
    ]),
)

# The three logins, in the order the page asks for them.
LOGIN_ROLES = (
    ("bureau_admin", BUREAU_ADMIN, "Bureau admin"),
    ("client_admin", CLIENT_ADMIN, "Client company admin"),
    ("client_preparer", CLIENT_PREPARER, "Client payroll preparer"),
)


class DemoGuardError(RuntimeError):
    """The data does not look like the demo workspace; nothing was changed."""


class DemoStateError(RuntimeError):
    """The action does not fit the workspace's current state (already set up,
    a name taken, a send in progress); nothing was changed."""


# --- Guard -------------------------------------------------------------------


def demo_bureau(lock=False):
    """The demo bureau, or None. More than one is a state this module never
    makes, so it refuses rather than guess which to touch. ``lock`` takes a row
    lock (Postgres), so two clicks of Refresh or Remove run one after the other."""
    query = Bureau.query.filter(Bureau.is_demo.is_(True))
    if lock:
        query = query.with_for_update()
    rows = query.all()
    if len(rows) > 1:
        raise DemoGuardError("More than one demo bureau exists, so nothing was changed.")
    return rows[0] if rows else None


def demo_companies(bureau):
    """The demo bureau's companies, oldest first, after proving the flagged
    companies and the bureau's companies are the same set."""
    flagged = {cid for (cid,) in db.session.query(ClientCompany.id).filter(ClientCompany.is_demo.is_(True))}
    owned = {cid for (cid,) in db.session.query(ClientCompany.id).filter(ClientCompany.bureau_id == bureau.id)}
    if flagged != owned:
        raise DemoGuardError(
            "The demo companies and the demo bureau's companies don't match, so nothing was changed."
        )
    return ClientCompany.query.filter(ClientCompany.id.in_(owned)).order_by(ClientCompany.id).all()


def _require_demo(company, bureau):
    if not (company.is_demo and company.bureau_id == bureau.id):
        raise DemoGuardError(f"{company.name} is not a demo company, so nothing was changed.")


def demo_logins(bureau, companies):
    """Every login inside the workspace: the bureau's staff and the companies'
    own users. Payrolla staff (no bureau, no company) can never match."""
    company_ids = [c.id for c in companies] or [-1]
    return (
        User.query.filter(db.or_(User.bureau_id == bureau.id, User.client_company_id.in_(company_ids)))
        .order_by(User.id)
        .all()
    )


def _assign_specs(companies):
    """{spec key: company}. The home company is the bureau's own; the first two
    other demo companies, by age, are client A and B. A company the demo bureau
    added itself comes later and is kept, emptied, by Refresh."""
    home = next((c for c in companies if c.bureau_home), None)
    others = [c for c in companies if c is not home]
    assigned = {"home": home}
    for key, company in zip(("client_a", "client_b"), others):
        assigned[key] = company
    return assigned


def status():
    """What the page shows. Never raises: a broken state is reported."""
    try:
        bureau = demo_bureau()
        if bureau is None:
            return {"exists": False}
        companies = demo_companies(bureau)
    except DemoGuardError as exc:
        return {"exists": True, "problem": str(exc)}
    client_a = _assign_specs(companies).get("client_a")
    return {
        "exists": True,
        "bureau": bureau,
        "companies": companies,
        "portal_company_id": client_a.id if client_a is not None else None,
        "logins": demo_logins(bureau, companies),
    }


# --- Build -------------------------------------------------------------------


def _dress_contacts(employees, slot):
    """Obviously made-up contacts: a sequential 020 block (valid in format, so
    SMS shows as a channel) and no email for every third worker, so the history
    mixes SMS and email. The send block, not the numbers, is what keeps a real
    phone from ever getting one."""
    for index, employee in enumerate(employees):
        number = f"0200{slot}{index + 1:05d}"
        employee.phone = number
        employee.momo_number = number
        if index % 3 == 2:
            employee.email = None


def _build_events(company, runs, actor):
    """The business events the dashboards' activity timelines read (a bureau's
    timeline shows only these): payslips out for each closed run, the risk hold
    on a held one. Dated with the run, like the rest of the history."""
    for run in runs:
        moment = run.approved_at or run.created_at
        if run.status == PROCESSED:
            event_type, summary = "payslips.distributed", f"{run.month} {run.year}: {run.total_workers} payslips sent."
            moment = moment + timedelta(hours=2)
        elif run.risk_status == "held":
            event_type, summary = "run.risk_held", f"{run.month} {run.year}: {run.risk_reasons}."
        else:
            continue
        db.session.add(DomainEvent(
            event_type=event_type, actor_user_id=actor.id if actor else None,
            actor_role=actor.role if actor else "system", client_company_id=company.id,
            subject_type="PayrollRun", subject_id=run.id, summary=summary, created_at=moment,
        ))


def _build(bureau, assigned, logins, months):
    """Rosters and history for the spec'd companies. Staged; the caller commits."""
    bureau_admin = logins.get("bureau_admin")
    client_admin = logins.get("client_admin")
    periods = _recent_periods(months)
    summary = []
    for offset, (key, _fields, prefix, roster) in enumerate(COMPANY_SPECS):
        company = assigned.get(key)
        if company is None:
            continue
        employees = build_employees(company, roster=roster, staff_prefix=prefix)
        # Payroll items copy the contacts, so dress them before the runs.
        _dress_contacts(employees, offset + 1)
        db.session.flush()
        approver = client_admin if key == "client_a" and client_admin else bureau_admin
        runs = build_payroll_history(company, employees, periods, bureau_admin, approver,
                                     status_offset=offset)
        build_deliveries(runs, provider="demo-seed")
        build_expenses(company, periods, bureau_admin)
        build_activity(company, runs, bureau_admin)
        _build_events(company, runs, bureau_admin)
        summary.append({"name": company.name, "employees": len(employees), "runs": len(runs)})
    return summary


def _logins_by_key(bureau, companies, assigned):
    """The three spec logins as {key: user}, found by role and scope."""
    client_a = assigned.get("client_a")
    found = {}
    for user in demo_logins(bureau, companies):
        if user.bureau_id == bureau.id and user.role == BUREAU_ADMIN:
            found.setdefault("bureau_admin", user)
        elif client_a is not None and user.client_company_id == client_a.id:
            key = "client_admin" if user.role == CLIENT_ADMIN else "client_preparer"
            found.setdefault(key, user)
    return found


# --- Actions -----------------------------------------------------------------


def set_up(logins, months=DEFAULT_MONTHS):
    """Create the workspace. ``logins`` is {key: (name, email)} for the three
    keys in LOGIN_ROLES, already validated. Returns (bureau, {key: User});
    commits once. The caller sends the invites."""
    if demo_bureau() is not None:
        raise DemoStateError("The demo workspace is already set up. Use Refresh instead.")
    names = [fields["name"] for _key, fields, _p, _r in COMPANY_SPECS]
    codes = [fields["company_code"] for _key, fields, _p, _r in COMPANY_SPECS]
    if (
        Bureau.query.filter(db.or_(Bureau.name == BUREAU_SPEC["name"], Bureau.code == BUREAU_SPEC["code"])).first()
        or ClientCompany.query.filter(
            db.or_(ClientCompany.name.in_(names), ClientCompany.company_code.in_(codes))
        ).first()
    ):
        raise DemoStateError(
            "A real bureau or company already uses one of the demo names or codes. Nothing was created."
        )

    bureau = Bureau(is_demo=True, status="Active", **BUREAU_SPEC)
    db.session.add(bureau)
    db.session.flush()
    assigned = {}
    for key, fields, _prefix, _roster in COMPANY_SPECS:
        company = ClientCompany(status="Active", is_demo=True, bureau_id=bureau.id,
                                bureau_home=(key == "home"), **fields)
        db.session.add(company)
        assigned[key] = company
    db.session.flush()

    users = {}
    for key, role, _label in LOGIN_ROLES:
        name, email = logins[key]
        scope = {"bureau_id": bureau.id} if role == BUREAU_ADMIN else {"client_company_id": assigned["client_a"].id}
        user = User(name=name, email=email, role=role, password_hash="!", **scope)
        db.session.add(user)
        users[key] = user
    db.session.flush()

    with seeding():
        _build(bureau, assigned, users, months)
        db.session.commit()
    db.session.expire_all()
    return db.session.get(Bureau, bureau.id), {key: db.session.get(User, u.id) for key, u in users.items()}


def _refuse_while_sending(companies):
    """A running send can't be stopped mid-way, so refuse. A queued or scheduled
    one hasn't started, so cancel it before its run is purged."""
    ids = [c.id for c in companies] or [-1]
    batches = DistributionBatch.query.filter(DistributionBatch.client_company_id.in_(ids))
    if batches.filter(DistributionBatch.status == BATCH_RUNNING).first() is not None:
        raise DemoStateError("Payslips are being sent for a demo run right now. Try again in a minute.")
    batches.filter(DistributionBatch.status.in_((BATCH_QUEUED, BATCH_SCHEDULED))).update(
        {DistributionBatch.status: BATCH_CANCELLED}, synchronize_session=False
    )


def _clear_bureau_activity(bureau, user_ids):
    """What the demo logins did that hangs off the bureau rather than a company:
    the bureau's own rate versions, and the logins' audit rows, events and
    notifications."""
    _bulk_delete(StatutoryRate, StatutoryRate.bureau_id == bureau.id)
    if not user_ids:
        return
    _bulk_delete(AuditTrail, AuditTrail.user_id.in_(user_ids))
    event_ids = [eid for (eid,) in db.session.query(DomainEvent.id).filter(DomainEvent.actor_user_id.in_(user_ids))]
    _bulk_delete(
        Notification,
        db.or_(Notification.user_id.in_(user_ids), Notification.event_id.in_(event_ids or [-1])),
    )
    if event_ids:
        _bulk_delete(DomainEvent, DomainEvent.id.in_(event_ids))


def _forget_purged_rows():
    """Drop the session's copies of rows the bulk deletes removed.

    The purge deletes without loading (fast, but the session is not told), so a
    loaded roster or run list would still be in the identity map, and a cascade
    from a loaded collection could try to write a deleted row back. Only the
    bureau, companies and logins are kept, and expired, so they reload."""
    db.session.flush()
    for obj in list(db.session.identity_map.values()):
        if not isinstance(obj, (Bureau, ClientCompany, User)):
            db.session.expunge(obj)
    db.session.expire_all()


def refresh(staff, months=DEFAULT_MONTHS):
    """Empty every demo company and rebuild the spec'd data, keeping the logins.
    Returns (stored file keys to delete after the commit, summary); commits once."""
    bureau = demo_bureau(lock=True)
    if bureau is None:
        raise DemoStateError("There is no demo workspace to refresh. Set it up first.")
    companies = demo_companies(bureau)
    _refuse_while_sending(companies)
    stored_keys = []
    for company in companies:
        _require_demo(company, bureau)
        stored_keys += purge_client_company(company, staff.id, drop_company=False, keep_users=True)
    user_ids = [u.id for u in demo_logins(bureau, companies)]
    _clear_bureau_activity(bureau, user_ids)
    _forget_purged_rows()

    for field, value in BUREAU_SPEC.items():
        setattr(bureau, field, value)
    bureau.status = "Active"
    assigned = _assign_specs(companies)
    for key, fields, _prefix, _roster in COMPANY_SPECS:
        company = assigned.get(key)
        if company is None:
            continue
        for field, value in fields.items():
            setattr(company, field, value)
        company.status = "Active"
    db.session.flush()

    logins = _logins_by_key(bureau, companies, assigned)
    for user in demo_logins(bureau, companies):
        user.disabled_at = None  # a refresh is a fresh start for the walkthrough
    with seeding():
        summary = _build(bureau, assigned, logins, months)
        db.session.commit()
    db.session.expire_all()
    return stored_keys, summary


def remove(staff):
    """Delete the whole workspace, logins included. References elsewhere to a
    demo login are re-pointed at ``staff``. Returns the stored file keys to
    delete after the commit; commits once."""
    bureau = demo_bureau(lock=True)
    if bureau is None:
        raise DemoStateError("There is no demo workspace to remove.")
    staff_id = staff.id  # read now: ``staff`` may be Flask-Login's current_user proxy
    companies = demo_companies(bureau)
    _refuse_while_sending(companies)
    company_ids = [c.id for c in companies]
    bureau_user_ids = [uid for (uid,) in db.session.query(User.id).filter(User.bureau_id == bureau.id)]

    stored_keys = []
    for company in companies:
        _require_demo(company, bureau)
        # The company row is deleted below, by a delete that checks the flag.
        stored_keys += purge_client_company(company, staff_id, drop_company=False)
    _clear_bureau_activity(bureau, bureau_user_ids)
    if bureau_user_ids:
        release_user_references(bureau_user_ids, staff_id)
        _bulk_delete(User, User.id.in_(bureau_user_ids))
    if company_ids:
        AccessRequest.query.filter(AccessRequest.client_company_id.in_(company_ids)).update(
            {AccessRequest.client_company_id: None}, synchronize_session=False
        )
        deleted = ClientCompany.query.filter(
            ClientCompany.id.in_(company_ids), ClientCompany.is_demo.is_(True)
        ).delete(synchronize_session=False)
        if deleted != len(company_ids):
            raise DemoGuardError("A company changed while it was being removed, so nothing was changed.")
    if Bureau.query.filter(Bureau.id == bureau.id, Bureau.is_demo.is_(True)).delete(
        synchronize_session=False
    ) != 1:
        raise DemoGuardError("The demo bureau changed while it was being removed, so nothing was changed.")
    db.session.commit()
    # Drop the session's copies of the deleted rows, keeping the acting login.
    # Matched by identity key, not by loading the object: a deleted row can't load.
    for key, obj in list(db.session.identity_map.items()):
        if key[0] is User and key[1] == (staff_id,):
            continue
        db.session.expunge(obj)
    return stored_keys


def login_for_resend(user_id):
    """A demo login and the org its invite names, or (None, None) if the id is
    not a demo login."""
    bureau = demo_bureau()
    if bureau is None:
        return None, None
    companies = demo_companies(bureau)
    user = next((u for u in demo_logins(bureau, companies) if u.id == user_id), None)
    if user is None:
        return None, None
    org = bureau if user.bureau_id == bureau.id else db.session.get(ClientCompany, user.client_company_id)
    return user, org
