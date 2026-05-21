import os
from datetime import date, datetime

from app import db
from app.finance import create_finance_records_for_payroll
from app.models import ClientCompany, Employee, Expense, PayrollItem, PayrollRun, User


def seed_default_data():
    seed_users()
    seed_clients()
    if os.getenv("SEED_DEMO_DATA", "false").lower() == "true":
        seed_employees()
        seed_payroll()
        seed_expenses()
    db.session.commit()


def seed_users():
    users = [
        ("Admin User", "admin@chrisnat.local", "admin"),
        ("Managing Director", "md@chrisnat.local", "md"),
        ("Payroll Officer", "payroll@chrisnat.local", "payroll_officer"),
        ("Accounts Officer", "accounts@chrisnat.local", "accounts_officer"),
    ]
    for name, email, role in users:
        if not User.query.filter_by(email=email).first():
            user = User(name=name, email=email, role=role)
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
    employees = Employee.query.filter_by(client_company_id=client.id).all()
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
    create_finance_records_for_payroll(payroll_run, md.id)


def seed_expenses():
    if Expense.query.count():
        return
    accounts = User.query.filter_by(email="accounts@chrisnat.local").first()
    db.session.add(
        Expense(
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
            expense_date=date.today(),
            category="Transport",
            description="Staff movement to client site",
            amount=780,
            payment_method="Mobile Money",
            receipt_reference="MOMO-002",
            recorded_by=accounts.id,
        )
    )
