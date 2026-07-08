import json
import os
from datetime import date, datetime, time, timedelta, timezone

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


def seed_default_data():
    seed_users()
    seed_clients()
    seed_statutory_rates()
    if os.getenv("SEED_DEMO_DATA", "false").lower() == "true":
        seed_employees()
        seed_payroll()
        seed_expenses()
    if os.getenv("SEED_ARCHIVED_FEATURES", "false").lower() == "true":
        seed_client_users()
    db.session.commit()


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


def seed_users():
    users = [
        ("Admin User", "admin@chrisnat.local", "admin"),
        ("Managing Director", "md@chrisnat.local", "md"),
        ("Payroll Officer", "payroll@chrisnat.local", "payroll_officer"),
        ("Accounts Officer", "accounts@chrisnat.local", "accounts_officer"),
        ("Operations Supervisor", "operations@chrisnat.local", "operations_supervisor"),
    ]
    for name, email, role in users:
        if not User.query.filter_by(email=email).first():
            user = User(name=name, email=email, role=role)
            user.set_password("password123")
            db.session.add(user)


def seed_client_users():
    client_users = [
        ("MSC Client User", "msc.client@chrisnat.local", "MSC Ghana Ltd"),
        ("Stellar Client User", "stellar.client@chrisnat.local", "Stellar Logistics"),
        ("Grimaldi Client User", "grimaldi.client@chrisnat.local", "Grimaldi Ghana Ltd"),
    ]
    for name, email, client_name in client_users:
        client = ClientCompany.query.filter_by(name=client_name).first()
        if client and not User.query.filter_by(email=email).first():
            user = User(
                name=name,
                email=email,
                role="client_user",
                client_company_id=client.id,
            )
            user.set_password("password123")
            db.session.add(user)


def seed_clients():
    clients = [
        ("MSC Ghana Ltd", "Ama Mensah", "Accra", "Port operations support"),
        ("Grimaldi Ghana Ltd", "Kojo Boateng", "Tema", "Logistics staffing"),
        ("ACS/GMT Shipping", "Efua Owusu", "Tema", "Shipping services"),
        ("Portmarine Ltd", "Yaw Addo", "Takoradi", "Marine support"),
        ("Multipurpose Terminal", "Akosua Asante", "Tema", "Terminal labour"),
        ("Stellar Logistics", "Kofi Frimpong", "Accra", "Warehouse operations"),
    ]
    for name, contact, location, service_type in clients:
        if not ClientCompany.query.filter_by(name=name).first():
            db.session.add(
                ClientCompany(
                    name=name,
                    contact_person=contact,
                    phone="0240000000",
                    email=f"{name.lower().replace('/', '').replace(' ', '.')}@example.com",
                    location=location,
                    service_type=service_type,
                    status="Active",
                )
            )
    db.session.flush()


def seed_employees():
    if Employee.query.count():
        return
    names = [
        ("CN-001", "Kwame Mensah", "MSC Ghana Ltd", 2600),
        ("CN-002", "Akosua Osei", "MSC Ghana Ltd", 2400),
        ("CN-003", "Yaw Boateng", "Grimaldi Ghana Ltd", 2800),
        ("CN-004", "Ama Serwaa", "ACS/GMT Shipping", 2300),
        ("CN-005", "Kofi Appiah", "Portmarine Ltd", 3100),
        ("CN-006", "Efua Nyarko", "Multipurpose Terminal", 2500),
        ("CN-007", "Kojo Antwi", "Stellar Logistics", 2700),
        ("CN-008", "Abena Darko", "MSC Ghana Ltd", 2350),
        ("CN-009", "Nana Adu", "Grimaldi Ghana Ltd", 2950),
        ("CN-010", "Esi Amponsah", "Stellar Logistics", 2450),
        ("CN-011", "Akua Boateng", "MSC Ghana Ltd", 2550),
        ("CN-012", "Kojo Addai", "MSC Ghana Ltd", 2650),
        ("CN-013", "Afia Darko", "MSC Ghana Ltd", 2350),
        ("CN-014", "Yaw Antwi", "Stellar Logistics", 2750),
        ("CN-015", "Abigail Tetteh", "Stellar Logistics", 2300),
        ("CN-016", "Samuel Nartey", "Grimaldi Ghana Ltd", 2850),
        ("CN-017", "Linda Ofori", "Grimaldi Ghana Ltd", 2450),
        ("CN-018", "Isaac Quaye", "ACS/GMT Shipping", 2650),
        ("CN-019", "Mavis Adjei", "Portmarine Ltd", 2500),
        ("CN-020", "Daniel Asamoah", "Multipurpose Terminal", 2700),
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
    admin = User.query.filter_by(email="admin@chrisnat.local").first()
    md = User.query.filter_by(email="md@chrisnat.local").first()
    client = ClientCompany.query.filter_by(name="MSC Ghana Ltd").first()
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
        approved_by=md.id,
        client_company_id=client.id,
        total_workers=len(employees),
        total_rows_imported=len(employees),
        duplicate_workers_found=0,
        source_filename="seed_msc_payroll.xlsx",
        import_type="Single Company Upload",
        detected_company_name=client.name,
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
    accounts = User.query.filter_by(email="accounts@chrisnat.local").first()
    db.session.add(
        Expense(
            title="Cleaning supplies",
            expense_date=date.today(),
            category="Cleaning supplies",
            description="Detergents and office cleaning materials",
            amount=450,
            payment_method="Cash",
            receipt_reference="REC-001",
            recorded_by=accounts.id,
        )
    )
    db.session.add(
        Expense(
            title="Staff transport",
            expense_date=date.today(),
            category="Transport",
            description="Staff movement to client site",
            amount=780,
            payment_method="Mobile Money",
            receipt_reference="MOMO-002",
            recorded_by=accounts.id,
        )
    )
