"""Demo data reset — a clean, demonstration-grade dataset in one command.

``flask demo-reset`` rebuilds the database's *demo* content: it removes every
client company that is not one of the professional demo tenants, removes
obsolete platform logins, and then populates the survivors with several months
of realistic payroll history, employees, expenses and payslip deliveries — so no
screen an audience reaches is empty.

Three properties this module is built around:

**Explicit.** Nothing here runs at boot. :mod:`app.seed` stays cheap because it
executes on every ``create_app()`` (including every test); this heavier dataset
is only ever produced by an operator typing the command.

**Referentially safe.** Deleting a tenant means deleting everything that hangs
off it, in an order that never leaves a dangling foreign key: grandchildren
(payslip deliveries, raw entries) before children (payroll items, vouchers)
before parents (runs, employees) before the company itself. Rows that merely
*reference* a departing user (an approved run, a recorded expense) are not
deleted — their user column is re-pointed at the retained platform admin, so
history keeps an attribution instead of decaying to "System".

**Idempotent.** Running it twice produces the same database. Companies are
matched by name, and the rebuild purges a demo tenant's data before recreating
it, so a half-finished run never leaves duplicates behind.

The statutory figures are NOT invented: every payroll item is produced by the
real :class:`~app.payroll_calculations.salaried.SalariedCalculator` against the
active :class:`~app.models.StatutoryRate`, so demo payslips are arithmetically
identical to production ones.
"""

from calendar import month_name
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import or_

from app import db
from app.models import (
    DELIVERY_SENT,
    AuditTrail,
    ClientCompany,
    DistributionBatch,
    DomainEvent,
    Employee,
    EmployeeDeployment,
    Expense,
    ImportBatch,
    Notification,
    PaymentVoucher,
    PayrollItem,
    PayrollRun,
    PayslipDelivery,
    Proposal,
    RawPayEntry,
    RawUploadArchive,
    Remittance,
    StatutoryRate,
    User,
    WageRateProfile,
)
from app.payroll_calculations.salaried import SalariedCalculator
from app.payroll_status import APPROVED, HELD, PENDING_APPROVAL, PROCESSED
from app.seed import DEMO_COMPANIES, PLATFORM_USERS, seed_clients, seed_tenant_users, seed_users

# How many months of payroll history each demo tenant gets. Six fills the
# dashboard's six-period charts exactly.
DEFAULT_MONTHS = 6

# Lifecycle states for the most RECENT runs, newest first; everything older is
# closed. Anchoring on the newest end (rather than cycling from the oldest) is
# both realistic — last month's payroll is still moving, last year's is paid —
# and means a short history still shows several states, which is what makes the
# status donut worth looking at.
RUN_STATUS_TAIL = (PENDING_APPROVAL, HELD, APPROVED)

# (staff suffix, name, job title, department, monthly basic).
DEMO_ROSTERS = {
    "MSC": [
        ("001", "Kwame Mensah", "Operations Supervisor", "Operations", 4200),
        ("002", "Akosua Osei", "Finance Officer", "Finance", 3800),
        ("003", "Yaw Boateng", "Crane Operator", "Terminal", 3100),
        ("004", "Abena Darko", "Documentation Clerk", "Administration", 2450),
        ("005", "Kojo Addai", "Forklift Operator", "Terminal", 2900),
        ("006", "Afia Owusu", "Safety Officer", "HSE", 3300),
        ("007", "Isaac Quaye", "Stevedore", "Terminal", 2200),
        ("008", "Mavis Adjei", "HR Officer", "People", 3050),
        ("009", "Nana Adu", "Warehouse Lead", "Warehouse", 3400),
        ("010", "Esi Amponsah", "Accounts Assistant", "Finance", 2600),
    ],
    "ACME": [
        ("001", "Kofi Appiah", "Plant Manager", "Production", 5200),
        ("002", "Efua Nyarko", "Production Planner", "Production", 3600),
        ("003", "Samuel Nartey", "Machine Operator", "Production", 2700),
        ("004", "Linda Ofori", "Quality Inspector", "Quality", 2950),
        ("005", "Yaw Antwi", "Maintenance Technician", "Maintenance", 3150),
        ("006", "Abigail Tetteh", "Store Keeper", "Warehouse", 2400),
        ("007", "Daniel Asamoah", "Logistics Coordinator", "Logistics", 3250),
        ("008", "Akua Boateng", "Payroll Clerk", "Finance", 2800),
    ],
}

# Recurring monthly spend, cycled across the charted months so the category
# breakdown has real shape instead of one flat slice.
DEMO_EXPENSE_PLAN = [
    ("Utilities", "Electricity and water for the site", 1850),
    ("Internet", "Fibre and site connectivity", 620),
    ("Fuel", "Generator diesel and fleet fuel", 2400),
    ("Maintenance", "Plant and equipment servicing", 1450),
    ("Office Supplies", "Stationery and consumables", 380),
    ("Travel", "Client site visits and staff transport", 940),
    ("Miscellaneous", "Sundry operational costs", 310),
]

BANKS = ("GCB Bank", "Ecobank Ghana", "Absa Bank Ghana", "Fidelity Bank", "Stanbic Bank")


# --- Purge -------------------------------------------------------------------


def _ids(query):
    return [row[0] for row in query.all()]


def _bulk_delete(model, condition):
    """Delete rows matching ``condition`` without loading them.

    ``synchronize_session=False`` because the caller discards the session's
    identity map immediately afterwards (``db.session.expire_all()`` at the end
    of the purge); loading every row just to evict it would make a reset of a
    real dataset needlessly slow.
    """
    return model.query.filter(condition).delete(synchronize_session=False)


def release_user_references(user_ids, replacement_user_id=None):
    """Re-point every column that references a departing user.

    Rows owned by OTHER records (an approved payroll run, a recorded expense, a
    statutory rate version) are never deleted just because the user who touched
    them is going away — that would destroy real history. Each such column is
    set to ``replacement_user_id`` (normally the retained platform admin) so the
    record keeps a human attribution, or NULL when there is no replacement.

    ``Notification.user_id`` is the one exception: a notification exists only for
    its recipient, so an unread notice for a deleted user is deleted, not
    reassigned.
    """
    if not user_ids:
        return
    columns = [
        (PayrollRun, ("created_by", "reviewed_by", "approved_by")),
        (PaymentVoucher, ("prepared_by", "reviewed_by", "approved_by")),
        (Expense, ("paid_by", "approved_by", "recorded_by")),
        (ImportBatch, ("uploaded_by",)),
        (Proposal, ("drafted_by",)),
        (DistributionBatch, ("initiated_by_user_id",)),
        (StatutoryRate, ("created_by",)),
        (DomainEvent, ("actor_user_id",)),
        (AuditTrail, ("user_id",)),
    ]
    for model, names in columns:
        for name in names:
            column = getattr(model, name)
            model.query.filter(column.in_(user_ids)).update(
                {column: replacement_user_id}, synchronize_session=False
            )
    _bulk_delete(Notification, Notification.user_id.in_(user_ids))


def purge_client_company(company, replacement_user_id=None, drop_company=True):
    """Delete everything belonging to ``company``, child-first.

    With ``drop_company=False`` the company row itself survives — that is how a
    retained demo tenant is emptied before being rebuilt. Staged only; the caller
    owns the commit, so a failure rolls the whole reset back rather than leaving
    a half-deleted tenant.
    """
    cid = company.id
    run_ids = _ids(db.session.query(PayrollRun.id).filter_by(client_company_id=cid))
    employee_ids = _ids(db.session.query(Employee.id).filter_by(client_company_id=cid))
    user_ids = _ids(db.session.query(User.id).filter_by(client_company_id=cid))

    # 1. Grandchildren of a run: deliveries reference items AND batches, so they
    #    must go before either.
    if run_ids:
        _bulk_delete(PayslipDelivery, PayslipDelivery.payroll_run_id.in_(run_ids))
        _bulk_delete(RawUploadArchive, RawUploadArchive.payroll_run_id.in_(run_ids))
        _bulk_delete(RawPayEntry, RawPayEntry.payroll_run_id.in_(run_ids))
        _bulk_delete(Remittance, Remittance.payroll_run_id.in_(run_ids))
        _bulk_delete(PaymentVoucher, PaymentVoucher.payroll_run_id.in_(run_ids))
        _bulk_delete(PayrollItem, PayrollItem.payroll_run_id.in_(run_ids))

    # 2. Records attached to the tenant or to one of its runs.
    _bulk_delete(
        DistributionBatch,
        or_(
            DistributionBatch.client_company_id == cid,
            DistributionBatch.payroll_run_id.in_(run_ids or [-1]),
        ),
    )
    _bulk_delete(
        Expense,
        or_(
            Expense.client_company_id == cid,
            Expense.payroll_run_id.in_(run_ids or [-1]),
        ),
    )
    _bulk_delete(
        ImportBatch,
        or_(
            ImportBatch.client_company_id == cid,
            ImportBatch.payroll_run_id.in_(run_ids or [-1]),
        ),
    )

    # 3. Event log + its fan-out. Notifications point at events, so they go first.
    event_ids = _ids(
        db.session.query(DomainEvent.id).filter(
            or_(
                DomainEvent.client_company_id == cid,
                DomainEvent.actor_user_id.in_(user_ids or [-1]),
            )
        )
    )
    _bulk_delete(
        Notification,
        or_(
            Notification.client_company_id == cid,
            Notification.event_id.in_(event_ids or [-1]),
            Notification.user_id.in_(user_ids or [-1]),
        ),
    )
    if event_ids:
        _bulk_delete(DomainEvent, DomainEvent.id.in_(event_ids))

    # 4. Roster satellites, then the roster and the runs themselves.
    _bulk_delete(
        WageRateProfile,
        or_(
            WageRateProfile.client_company_id == cid,
            WageRateProfile.employee_id.in_(employee_ids or [-1]),
        ),
    )
    _bulk_delete(
        EmployeeDeployment,
        or_(
            EmployeeDeployment.client_company_id == cid,
            EmployeeDeployment.employee_id.in_(employee_ids or [-1]),
        ),
    )
    _bulk_delete(Proposal, Proposal.client_company_id == cid)
    _bulk_delete(PayrollRun, PayrollRun.client_company_id == cid)
    _bulk_delete(Employee, Employee.client_company_id == cid)

    # 5. The tenant's own users. Their audit entries go with them — an audit row
    #    for a deleted demo tenant has nothing left to explain.
    if user_ids:
        _bulk_delete(AuditTrail, AuditTrail.user_id.in_(user_ids))
        release_user_references(user_ids, replacement_user_id)
        _bulk_delete(User, User.id.in_(user_ids))

    if drop_company:
        db.session.delete(company)
    db.session.flush()


def purge_obsolete_platform_users(replacement_user_id=None):
    """Remove platform logins that are not part of the professional roster.

    Returns the emails removed. The replacement admin is never removed, even if
    the roster were misconfigured, so the reset can always attribute history to
    somebody.
    """
    keep = {email for _name, email, _role in PLATFORM_USERS}
    obsolete = (
        User.query.filter(User.client_company_id.is_(None), User.email.notin_(keep))
        .filter(User.id != (replacement_user_id or -1))
        .all()
    )
    if not obsolete:
        return []
    emails = [user.email for user in obsolete]
    user_ids = [user.id for user in obsolete]
    release_user_references(user_ids, replacement_user_id)
    _bulk_delete(User, User.id.in_(user_ids))
    db.session.flush()
    return emails


# --- Build -------------------------------------------------------------------


def _recent_periods(months, today=None):
    """The last ``months`` calendar periods, oldest first, as (month name, year)."""
    today = today or date.today()
    periods = []
    year, month = today.year, today.month
    for _ in range(months):
        periods.append((month_name[month], year))
        month -= 1
        if month == 0:
            month, year = 12, year - 1
    return list(reversed(periods))


def build_employees(company):
    """The company's roster, keyed off its company code. Returns the employees."""
    roster = DEMO_ROSTERS.get(company.company_code, [])
    employees = []
    for index, (suffix, full_name, job_title, department, basic) in enumerate(roster):
        employee = Employee(
            staff_id=f"{company.company_code}{suffix}",
            full_name=full_name,
            job_title=job_title,
            department=department,
            basic_salary=basic,
            pay_type="salaried",
            phone=f"0244{index:03d}{company.id:03d}",
            momo_number=f"0554{index:03d}{company.id:03d}",
            email=f"{full_name.split()[0].lower()}.{full_name.split()[-1].lower()}"
            f"@{company.email.split('@', 1)[1]}",
            ssnit_number=f"SSN{company.company_code}{suffix}",
            ghana_card_number=f"GHA-{company.id:04d}{suffix}-1",
            bank_name=BANKS[index % len(BANKS)],
            bank_branch="Main Branch",
            bank_account_number=f"{company.id}00{index:04d}77",
            employment_type="Permanent",
            service_line=company.service_type,
            assigned_client=company.name,
            client_company_id=company.id,
            status="Active",
        )
        db.session.add(employee)
        employees.append(employee)
    db.session.flush()
    return employees


def _period_inputs(employee, month_index):
    """This period's variable pay inputs for one employee.

    Deterministic (derived from the staff id and the month index, never random)
    so two resets produce the same figures and a demo can be rehearsed.
    """
    seed = sum(ord(character) for character in employee.staff_id) + month_index
    basic = employee.basic_salary or 0
    return {
        "transport_allowance": round(basic * 0.10, 2),
        "housing_allowance": round(basic * 0.08, 2),
        # Overtime lands on roughly half the roster in any given month.
        "overtime_pay": round(basic * 0.06, 2) if seed % 2 == 0 else 0,
        # A December production bonus, and a smaller one every fourth month.
        "productivity_bonus": round(basic * 0.15, 2) if seed % 4 == 0 else 0,
        "other_deductions": 50 if seed % 3 == 0 else 0,
    }


def run_status_for(index, total, offset=0):
    """The lifecycle state of the run at ``index`` in a ``total``-month history.

    ``offset`` shifts which in-flight state the newest run lands on, so two demo
    tenants are not staring at identical queues."""
    from_end = total - 1 - index
    if from_end < len(RUN_STATUS_TAIL):
        return RUN_STATUS_TAIL[(from_end + offset) % len(RUN_STATUS_TAIL)]
    return PROCESSED


def build_payroll_history(company, employees, periods, operator, approver, status_offset=0):
    """One payroll run per period with real calculated items. Returns the runs."""
    runs = []
    for index, (month, year) in enumerate(periods):
        status = run_status_for(index, len(periods), status_offset)
        rate = StatutoryRate.active_for(date(year, list(month_name).index(month), 1))
        if rate is None:
            continue
        calculator = SalariedCalculator(rate)
        created_at = datetime(year, list(month_name).index(month), 26, 9, 0)

        run = PayrollRun(
            month=month,
            year=year,
            status=status,
            client_company_id=company.id,
            created_by=operator.id if operator else None,
            approved_by=approver.id if approver and status in (APPROVED, PROCESSED) else None,
            approved_at=created_at + timedelta(days=1) if status in (APPROVED, PROCESSED) else None,
            upload_type="standard",
            import_mode="single_client",
            import_type="Single Company Upload",
            source_filename=f"{company.company_code.lower()}_payroll_{month.lower()}_{year}.xlsx",
            total_workers=len(employees),
            total_rows_imported=len(employees),
            total_unique_workers=len(employees),
            active_workers=len(employees),
            created_at=created_at,
            notes=f"{month} {year} payroll for {company.name}.",
        )
        if status == HELD:
            run.risk_status = "held"
            run.risk_reasons = "Net pay moved more than 15% against the previous period"
            run.risk_checked_at = created_at
        db.session.add(run)
        db.session.flush()

        for employee in employees:
            result = calculator.calculate_for_employee(
                employee, **_period_inputs(employee, index)
            )
            item = PayrollItem(
                payroll_run_id=run.id,
                employee_id=employee.id,
                staff_id=employee.staff_id,
                full_name=employee.full_name,
                status=employee.status,
                job_role=employee.job_title,
                payroll_month=f"{month} {year}",
                ssnit_number=employee.ssnit_number,
                ghana_card_number=employee.ghana_card_number,
                bank_name=employee.bank_name,
                bank_branch=employee.bank_branch,
                bank_account_number=employee.bank_account_number,
                momo_number=employee.momo_number,
                email=employee.email,
                validation_status="OK",
                **result.as_payroll_item_fields(),
            )
            db.session.add(item)
            run.total_gross_pay += result.gross_pay
            run.total_deductions += result.total_deductions
            run.total_net_pay += result.net_pay
            run.total_paye += result.paye
            run.total_ssnit += result.ssnit
            run.total_ssnit_employer += result.ssf_employer
        runs.append(run)
    db.session.flush()
    return runs


def build_deliveries(runs):
    """Mark a closed run's payslips as delivered, so the delivery-rate stat and
    the distribution surfaces have something real to show."""
    for run in runs:
        if run.status != PROCESSED:
            continue
        for item in run.items:
            db.session.add(
                PayslipDelivery(
                    payroll_item_id=item.id,
                    payroll_run_id=run.id,
                    channel="email" if item.email else "sms",
                    recipient=item.email or item.momo_number,
                    status=DELIVERY_SENT,
                    provider="console",
                    attempts=1,
                    sent_at=(run.approved_at or run.created_at) + timedelta(hours=2),
                )
            )


def build_expenses(company, periods, recorder):
    """Three expenses per charted month, rotating through the category list."""
    for index, (month, year) in enumerate(periods):
        month_number = list(month_name).index(month)
        for slot in range(3):
            category, description, base = DEMO_EXPENSE_PLAN[
                (index * 3 + slot) % len(DEMO_EXPENSE_PLAN)
            ]
            db.session.add(
                Expense(
                    title=description,
                    expense_date=date(year, month_number, min(4 + slot * 9, 28)),
                    category=category,
                    description=description,
                    # A steady drift month to month, so the by-month chart is not
                    # a flat line.
                    amount=round(base * (1 + index * 0.04), 2),
                    payment_method="Bank Transfer" if slot % 2 == 0 else "Mobile Money",
                    client_company_id=company.id,
                    status="Recorded",
                    recorded_by=recorder.id if recorder else None,
                )
            )


def build_activity(company, runs, actor):
    """A couple of timeline entries per tenant, written through the same logs the
    app writes to — never a special demo-only table."""
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    db.session.add(
        AuditTrail(
            user_id=actor.id if actor else None,
            user_role=actor.role if actor else "system",
            action="Client company onboarded",
            related_record_type="ClientCompany",
            related_record_id=company.id,
            notes=f"{company.name} ({company.company_code}) onboarded.",
            created_at=company.created_at or now,
        )
    )
    for run in runs:
        if run.status not in (APPROVED, PROCESSED):
            continue
        db.session.add(
            AuditTrail(
                user_id=actor.id if actor else None,
                user_role=actor.role if actor else "system",
                action="Payroll approval",
                related_record_type="PayrollRun",
                related_record_id=run.id,
                notes=f"{company.name} {run.month} {run.year} approved "
                f"({run.total_workers} workers).",
                created_at=run.approved_at or run.created_at,
            )
        )


# --- Orchestration -----------------------------------------------------------


def reset_demo_data(months=DEFAULT_MONTHS):
    """Rebuild the demo dataset. Returns a summary dict; commits once at the end.

    Everything happens in ONE transaction: if any step raises, the caller's
    rollback leaves the database exactly as it was rather than half-reset.
    """
    seed_users()  # make sure the professional roster exists before anything else
    db.session.flush()
    admin = User.query.filter_by(email="admin@payrolla.com").first()
    operator = User.query.filter_by(email="operator@payrolla.com").first() or admin
    approver = User.query.filter_by(email="director@payrolla.com").first() or admin
    accounts = User.query.filter_by(email="accounts@payrolla.com").first() or admin

    keep_names = {spec["name"] for spec in DEMO_COMPANIES}
    removed_companies = []
    for company in ClientCompany.query.all():
        if company.name in keep_names:
            # A retained tenant is emptied, not dropped — its id, and any login
            # already bound to it, survive the rebuild.
            purge_client_company(company, replacement_user_id=admin.id, drop_company=False)
        else:
            removed_companies.append(company.name)
            purge_client_company(company, replacement_user_id=admin.id)

    removed_users = purge_obsolete_platform_users(replacement_user_id=admin.id)

    # Recreate/refresh the two professional tenants and their staff logins.
    seed_clients()
    seed_tenant_users()
    db.session.flush()

    periods = _recent_periods(months)
    summary = {
        "removed_companies": removed_companies,
        "removed_users": removed_users,
        "months": months,
        "companies": [],
    }
    for offset, spec in enumerate(DEMO_COMPANIES):
        company = ClientCompany.query.filter_by(name=spec["name"]).first()
        if company is None:
            continue
        employees = build_employees(company)
        runs = build_payroll_history(
            company, employees, periods, operator, approver, status_offset=offset
        )
        build_deliveries(runs)
        build_expenses(company, periods, accounts)
        build_activity(company, runs, operator)
        summary["companies"].append(
            {
                "name": company.name,
                "code": company.company_code,
                "employees": len(employees),
                "runs": len(runs),
                "payroll_total": round(sum(run.total_net_pay for run in runs), 2),
            }
        )

    db.session.commit()
    db.session.expire_all()
    return summary
