"""Bootstrap data for a fresh Payrolla database.

Two tiers, deliberately separated:

  * **Always** — the platform roster, the two demo client companies, and the
    statutory rate version. Cheap, idempotent, and safe to run on every boot.
  * **``SEED_DEMO_DATA=true``** — a small slice of employees, one payroll run
    and a couple of expenses, so a fresh checkout and the test suite have
    something to render. Kept intentionally light: this runs on every test's
    ``create_app()``.

For a *demonstration-grade* dataset — several months of payroll history,
expenses across every category, both tenants populated — use the
``flask demo-reset`` command (:mod:`app.demo_data`), which is explicit,
operator-triggered, and never runs at boot.

Identities are professional, not demo-flavoured: platform staff on
``@payrolla.com`` and client staff on their own company domain. Roles are
imported from :mod:`app.roles` rather than spelled as literals, so the seeded
roster always matches the vocabulary the permission checks recognise.
"""

import json
import os
from datetime import date, datetime

from app import db
from app.models import (
    ClientCompany,
    Employee,
    Expense,
    PayrollItem,
    PayrollRun,
    StatutoryRate,
    User,
)
from app.roles import PAYROLLA_ADMIN, PAYROLLA_REVIEWER

# Demo passwords. Never used by a real deployment: production provisions its
# credentials in Supabase Auth, and these accounts are only seeded into a fresh
# local/staging database.
DEMO_PASSWORD = "password123"


# --- Platform (operator) roster ---------------------------------------------
# (name, email, role). Every legacy platform role keeps a login so the existing
# permission model stays fully demonstrable. Role strings come from app/roles.py
# rather than being spelled inline, so a seeded account can never drift from the
# vocabulary the permission checks actually recognise.
PLATFORM_USERS = [
    ("Payrolla Administrator", "admin@payrolla.com", "admin"),
    ("Payrolla Operations", "operator@payrolla.com", PAYROLLA_ADMIN),
    ("Payrolla Support", "support@payrolla.com", PAYROLLA_REVIEWER),
    ("Managing Director", "director@payrolla.com", "md"),
    ("Payroll Officer", "payroll@payrolla.com", "payroll_officer"),
    ("Accounts Officer", "accounts@payrolla.com", "accounts_officer"),
    ("Operations Supervisor", "operations@payrolla.com", "operations_supervisor"),
]

# --- Demo client companies ---------------------------------------------------
# Exactly two, both fully configured (code, contact, address). Everything a
# demonstration needs and nothing an audience has to be told to ignore.
DEMO_COMPANIES = [
    {
        "name": "MSC Limited",
        "company_code": "MSC",
        "contact_person": "Ama Mensah",
        "email": "admin@msc.com",
        "phone": "0302 700 100",
        "address": "18 Harbour Road, Tema Port",
        "location": "Tema",
        "service_type": "Port operations support",
        "notes": "Monthly payroll, bank transfer settlement.",
    },
    {
        "name": "Acme Manufacturing Ltd",
        "company_code": "ACME",
        "contact_person": "Kofi Frimpong",
        "email": "admin@acme.com",
        "phone": "0302 800 200",
        "address": "5 Spintex Industrial Avenue",
        "location": "Accra",
        "service_type": "Manufacturing & warehouse operations",
        "notes": "Monthly payroll, mixed bank and mobile-money settlement.",
    },
]

# (local part, display suffix, tenant role) applied to every demo company. The
# split mirrors how a client actually staffs payroll: an administrator and a
# finance lead who can approve and distribute, and a payroll clerk who prepares.
TENANT_USER_TEMPLATE = [
    ("admin", "Administrator", "client_admin"),
    ("finance", "Finance Lead", "client_admin"),
    ("payroll", "Payroll Officer", "client_preparer"),
]


def company_domain(company):
    """The email domain a company's staff logins use, derived from its own
    contact address ("admin@msc.com" -> "msc.com") so identities stay tied to
    the company rather than to a demo domain."""
    return (company["email"].split("@", 1)[1]).lower()


def seed_default_data():
    seed_users()
    seed_clients()
    seed_statutory_rates()
    # One client_admin/finance/payroll login per demo tenant, so the two-tenant
    # zero-cross-visibility story is testable from the first boot. Runs after
    # seed_clients() so the companies exist to bind to.
    seed_tenant_users()
    if os.getenv("SEED_DEMO_DATA", "false").lower() == "true":
        seed_employees()
        seed_payroll()
        seed_expenses()
    db.session.commit()


def seed_users():
    """The platform (operator) roster. ``client_company_id`` stays NULL — that
    NULL is what makes them platform users (app/roles.py)."""
    for name, email, role in PLATFORM_USERS:
        if not User.query.filter_by(email=email).first():
            user = User(name=name, email=email, role=role)
            user.set_password(DEMO_PASSWORD)
            db.session.add(user)


def seed_clients():
    for spec in DEMO_COMPANIES:
        if not ClientCompany.query.filter_by(name=spec["name"]).first():
            db.session.add(ClientCompany(status="Active", **spec))
    db.session.flush()


def seed_tenant_users():
    """Three staff logins per demo company, each hard-bound to that company.

    Idempotent: every user is guarded by an email-existence check, so re-running
    on an already-seeded database is a no-op.
    """
    for spec in DEMO_COMPANIES:
        company = ClientCompany.query.filter_by(name=spec["name"]).first()
        if company is None:
            continue
        domain = company_domain(spec)
        for local_part, title, role in TENANT_USER_TEMPLATE:
            email = f"{local_part}@{domain}"
            if User.query.filter_by(email=email).first():
                continue
            user = User(
                name=f"{spec['company_code']} {title}",
                email=email,
                role=role,
                client_company_id=company.id,
            )
            user.set_password(DEMO_PASSWORD)
            db.session.add(user)


# Initial statutory rate version from the ACS workbook's live PAYE formula
# (January 2026), CORRECTED against the GRA 2026 schedule: the sheet started
# the 35% band at 50,000 but GRA's annual 605,000 / 12 = 50,416.67 — the
# sheet's cumulative constant 13,728.67 is right, its threshold was low.
# VERIFY against the current GRA circular / SSNIT gazette before relying on
# these long-term — PAYE bands are revised regularly.
INITIAL_PAYE_BANDS = [
    {"over": 50416.67, "rate": 0.35, "base": 13728.67},
    {"over": 19896.67, "rate": 0.30, "base": 4572.67},
    {"over": 3896.67, "rate": 0.25, "base": 572.67},
    {"over": 730, "rate": 0.175, "base": 18.5},
    {"over": 600, "rate": 0.10, "base": 5.5},
    {"over": 490, "rate": 0.05, "base": 0},
]


def seed_statutory_rates():
    if StatutoryRate.query.count():
        return
    db.session.add(
        StatutoryRate(
            effective_from=date(2026, 1, 1),
            ssf_employee_rate=0.055,
            ssf_employer_rate=0.13,
            paye_bands_json=json.dumps(INITIAL_PAYE_BANDS),
            # Concessionary flat rates: overtime <=50% of monthly basic at 5%,
            # excess at 10%; bonus <=15% of annual basic at 5%, excess taxed
            # at the marginal rate (verified against the ACS live formulas).
            overtime_rate_low=0.05,
            overtime_rate_high=0.10,
            overtime_basic_threshold=0.50,
            bonus_rate=0.05,
            bonus_annual_basic_threshold=0.15,
            # Union (ICU) dues: 3% of basic wage for seeded members (raw-hours
            # engine), verified against all 137 members in the DZ Jan-2026 sheet.
            icu_member_rate=0.03,
            # GRA junior-staff gate for the overtime concession:
            # GHS 18,000/year qualifying income = 1,500/month. Overtime
            # earners above this get a visible warning (§7.1).
            overtime_junior_monthly_threshold=1500.0,
            notes=(
                "Seeded from ACS workbook PAYE formula (January 2026) and the "
                "SSNIT 5.5%/13% split in the client workbooks; 35% band "
                "threshold corrected to GRA's 50,416.67 (the sheet used "
                "50,000). Verify against the current GRA circular before "
                "treating as permanent."
            ),
        )
    )


def seed_employees():
    if Employee.query.count():
        return
    names = [
        ("CN-001", "Kwame Mensah", "MSC Limited", 2600),
        ("CN-002", "Akosua Osei", "MSC Limited", 2400),
        ("CN-003", "Yaw Boateng", "Acme Manufacturing Ltd", 2800),
        ("CN-004", "Ama Serwaa", "MSC Limited", 2300),
        ("CN-005", "Kofi Appiah", "Acme Manufacturing Ltd", 3100),
        ("CN-006", "Efua Nyarko", "Acme Manufacturing Ltd", 2500),
        ("CN-007", "Kojo Antwi", "Acme Manufacturing Ltd", 2700),
        ("CN-008", "Abena Darko", "MSC Limited", 2350),
        ("CN-009", "Nana Adu", "Acme Manufacturing Ltd", 2950),
        ("CN-010", "Esi Amponsah", "Acme Manufacturing Ltd", 2450),
        ("CN-011", "Akua Boateng", "MSC Limited", 2550),
        ("CN-012", "Kojo Addai", "MSC Limited", 2650),
        ("CN-013", "Afia Darko", "MSC Limited", 2350),
        ("CN-014", "Yaw Antwi", "Acme Manufacturing Ltd", 2750),
        ("CN-015", "Abigail Tetteh", "Acme Manufacturing Ltd", 2300),
        ("CN-016", "Samuel Nartey", "Acme Manufacturing Ltd", 2850),
        ("CN-017", "Linda Ofori", "Acme Manufacturing Ltd", 2450),
        ("CN-018", "Isaac Quaye", "MSC Limited", 2650),
        ("CN-019", "Mavis Adjei", "MSC Limited", 2500),
        ("CN-020", "Daniel Asamoah", "Acme Manufacturing Ltd", 2700),
    ]
    for index, (staff_id, full_name, client_name, salary) in enumerate(names, start=1):
        client = ClientCompany.query.filter_by(name=client_name).first()
        employee = Employee(
            staff_id=staff_id,
            full_name=full_name,
            phone=f"02400000{index:02d}",
            ghana_card_number=f"GHA-00000000-{index}",
            ssnit_number=f"SSNIT-{100000 + index}",
            bank_name="GCB Bank",
            bank_account_number=f"10020030{index:02d}",
            momo_number=f"05500000{index:02d}",
            employment_type="Outsourced Staff",
            service_line="Personnel Outsourcing",
            assigned_client=client_name,
            client_company_id=client.id if client else None,
            status="Active",
            basic_salary=salary,
        )
        db.session.add(employee)
    db.session.flush()


def seed_payroll():
    if PayrollRun.query.count():
        return
    admin = User.query.filter_by(email="admin@payrolla.com").first()
    director = User.query.filter_by(email="director@payrolla.com").first()
    client = ClientCompany.query.filter_by(name="MSC Limited").first()
    now = datetime.now()
    employees = Employee.query.filter(
        Employee.client_company_id == client.id,
        Employee.staff_id.in_(["CN-001", "CN-002", "CN-008"]),
    ).all()
    payroll_run = PayrollRun(
        month=now.strftime("%B"),
        year=now.year,
        status="Approved",
        created_by=admin.id,
        approved_by=director.id,
        client_company_id=client.id,
        total_workers=len(employees),
        total_rows_imported=len(employees),
        duplicate_workers_found=0,
        source_filename="seed_msc_payroll.xlsx",
        import_type="Single Company Upload",
        # Company detection retired — leave detected_company_name null (see app/payroll.py).
        notes="Seed payroll run for demo.",
    )
    db.session.add(payroll_run)
    db.session.flush()

    for employee in employees:
        transport = 250
        housing = 300
        overtime = 100
        gross = employee.basic_salary + transport + housing + overtime
        paye = round(gross * 0.08, 2)
        ssnit = round(gross * 0.055, 2)
        deductions = paye + ssnit
        net = gross - deductions
        item = PayrollItem(
            payroll_run_id=payroll_run.id,
            employee_id=employee.id,
            staff_id=employee.staff_id,
            full_name=employee.full_name,
            ssnit_number=employee.ssnit_number,
            basic_salary=employee.basic_salary,
            transport_allowance=transport,
            housing_allowance=housing,
            overtime_pay=overtime,
            other_allowances=0,
            gross_pay=gross,
            paye=paye,
            ssnit=ssnit,
            other_deductions=0,
            total_deductions=deductions,
            net_pay=net,
            validation_status="OK",
        )
        db.session.add(item)
        payroll_run.total_gross_pay += gross
        payroll_run.total_deductions += deductions
        payroll_run.total_net_pay += net
        payroll_run.total_paye += paye
        payroll_run.total_ssnit += ssnit


def seed_expenses():
    if Expense.query.count():
        return
    accounts = User.query.filter_by(email="accounts@payrolla.com").first()
    client = ClientCompany.query.filter_by(name="MSC Limited").first()
    db.session.add(
        Expense(
            title="Office internet",
            expense_date=date.today(),
            category="Internet",
            description="Monthly fibre subscription for the site office",
            amount=450,
            payment_method="Bank Transfer",
            receipt_reference="REC-001",
            client_company_id=client.id if client else None,
            status="Recorded",
            recorded_by=accounts.id,
        )
    )
    db.session.add(
        Expense(
            title="Staff transport",
            expense_date=date.today(),
            category="Travel",
            description="Staff movement to client site",
            amount=780,
            payment_method="Mobile Money",
            receipt_reference="MOMO-002",
            client_company_id=client.id if client else None,
            status="Recorded",
            recorded_by=accounts.id,
        )
    )
