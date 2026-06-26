import os
from datetime import date, datetime, time, timedelta, timezone

from app import db
from app.models import (
    AttendanceRecord,
    CleaningJob,
    CleaningJobWorker,
    ClientCompany,
    ClientServiceRequest,
    Employee,
    Expense,
    GoodsSupplyOrder,
    InventoryMovement,
    Invoice,
    InvoicePayment,
    Product,
    JobCompletionReport,
    Supplier,
    PayrollItem,
    PayrollRun,
    User,
    WorkerAssignment,
)
from app.operations_services import apply_attendance_calculations
from app.phase3_services import (
    add_goods_order_item,
    add_invoice_item,
    next_invoice_number,
    next_order_number,
    recalculate_goods_order,
    recalculate_invoice,
)


def seed_default_data():
    seed_users()
    seed_clients()
    if os.getenv("SEED_DEMO_DATA", "false").lower() == "true":
        seed_employees()
        seed_payroll()
        seed_expenses()
    if os.getenv("SEED_ARCHIVED_FEATURES", "false").lower() == "true":
        seed_client_users()
        seed_phase2_operations()
        seed_phase3_portal_billing_goods()
    db.session.commit()


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


def seed_phase2_operations():
    if WorkerAssignment.query.count() or AttendanceRecord.query.count() or CleaningJob.query.count():
        return

    operations_user = User.query.filter_by(email="operations@chrisnat.local").first()
    clients = {client.name: client for client in ClientCompany.query.all()}
    employees = Employee.query.order_by(Employee.staff_id).all()
    if not operations_user or not clients or not employees:
        return

    plan = [
        ("MSC Ghana Ltd", 6),
        ("Stellar Logistics", 4),
        ("Grimaldi Ghana Ltd", 3),
        ("ACS/GMT Shipping", 3),
        ("Portmarine Ltd", 2),
        ("Multipurpose Terminal", 2),
    ]
    employee_index = 0
    today = date.today()
    for client_name, count in plan:
        client = clients.get(client_name)
        for _ in range(count):
            if employee_index >= len(employees) or client is None:
                break
            employee = employees[employee_index]
            db.session.add(
                WorkerAssignment(
                    employee_id=employee.id,
                    client_company_id=client.id,
                    role="Cleaner" if employee_index % 3 else "Team Lead",
                    site_location=client.location or "Client site",
                    assignment_start_date=today - timedelta(days=30 - employee_index),
                    assignment_end_date=None,
                    status="Active",
                    shift_type=["Morning", "Afternoon", "Night", "Full Day", "Rotational"][employee_index % 5],
                    notes="Phase 2 demo assignment.",
                    created_by=operations_user.id,
                )
            )
            employee.client_company_id = client.id
            employee.assigned_client = client.name
            employee_index += 1
    db.session.flush()

    assignments = WorkerAssignment.query.order_by(WorkerAssignment.id).all()
    if not assignments:
        return
    attendance_statuses = ["Present", "Present", "Late", "Absent", "On Leave"]
    for index in range(30):
        assignment = assignments[index % len(assignments)]
        status = attendance_statuses[index % len(attendance_statuses)]
        record = AttendanceRecord(
            employee_id=assignment.employee_id,
            client_company_id=assignment.client_company_id,
            assignment_id=assignment.id,
            work_date=today - timedelta(days=index % 7),
            clock_in=None if status == "Absent" else time(8, 0 + (index % 2) * 30),
            clock_out=None if status in {"Absent", "On Leave"} else time(17 + (index % 3), 0),
            attendance_status=status,
            remarks="Phase 2 demo attendance.",
            recorded_by=operations_user.id,
        )
        apply_attendance_calculations(record)
        db.session.add(record)

    job_specs = [
        ("MSC Ghana Ltd", "Office Cleaning at MSC Ghana Ltd", "Office Cleaning", "MSC Tema office", "Completed"),
        ("Stellar Logistics", "Warehouse Cleaning at Stellar Logistics", "Warehouse Cleaning", "Stellar warehouse", "Completed"),
        ("Grimaldi Ghana Ltd", "Deep Cleaning at Grimaldi Ghana Ltd", "Deep Cleaning", "Grimaldi office", "Completed"),
        ("Portmarine Ltd", "Emergency Cleaning at Portmarine Ltd", "Emergency Cleaning", "Portmarine yard", "Scheduled"),
        ("Multipurpose Terminal", "Terminal Cleaning at Multipurpose Terminal", "Regular Cleaning", "Terminal block", "Scheduled"),
    ]
    created_jobs = []
    for index, (client_name, title, job_type, location, status) in enumerate(job_specs):
        client = clients.get(client_name)
        if not client:
            continue
        job = CleaningJob(
            client_company_id=client.id,
            job_title=title,
            job_type=job_type,
            location=location,
            scheduled_date=today + timedelta(days=index - 2),
            start_time=time(8, 0),
            end_time=time(12, 0),
            supervisor_id=operations_user.id,
            status=status,
            checklist="Sweep floors\nMop surfaces\nInspect waste bins\nSupervisor sign-off",
            notes="Phase 2 demo cleaning job.",
            created_by=operations_user.id,
        )
        db.session.add(job)
        created_jobs.append(job)
    db.session.flush()

    for job_index, job in enumerate(created_jobs):
        client_assignments = [assignment for assignment in assignments if assignment.client_company_id == job.client_company_id]
        for worker_index, assignment in enumerate(client_assignments[:3]):
            db.session.add(
                CleaningJobWorker(
                    cleaning_job_id=job.id,
                    employee_id=assignment.employee_id,
                    role="Team Lead" if worker_index == 0 else "Cleaner",
                    attendance_status="Present",
                    notes="Seeded cleaning team member.",
                )
            )
        if job_index < 3:
            db.session.add(
                JobCompletionReport(
                    cleaning_job_id=job.id,
                    completed_by=operations_user.id,
                    completion_status="Completed Successfully" if job_index < 2 else "Completed With Issues",
                    workers_present=max(len(client_assignments[:3]), 1),
                    workers_absent=0,
                    checklist_completed=True,
                    issues_found="" if job_index < 2 else "Minor delay due to access.",
                    client_feedback="Client satisfied with cleaning quality.",
                    supervisor_comments="Team completed assigned checklist.",
                    completed_at=datetime.now(timezone.utc),
                )
            )


def seed_phase3_portal_billing_goods():
    clients = {client.name: client for client in ClientCompany.query.all()}
    admin = User.query.filter_by(email="admin@chrisnat.local").first()
    accounts = User.query.filter_by(email="accounts@chrisnat.local").first()
    operations = User.query.filter_by(email="operations@chrisnat.local").first()
    if not clients or not admin:
        return

    if not ClientServiceRequest.query.count():
        specs = [
            ("MSC Ghana Ltd", "Cleaning Request", "Extra office cleaning", "High"),
            ("Stellar Logistics", "Goods Supply Request", "Safety gloves supply", "Normal"),
            ("Grimaldi Ghana Ltd", "Labour Request", "Weekend support team", "Urgent"),
            ("ACS/GMT Shipping", "Complaint", "Late attendance concern", "High"),
            ("Portmarine Ltd", "General Inquiry", "Monthly service schedule", "Low"),
        ]
        for index, (client_name, request_type, title, priority) in enumerate(specs):
            client = clients.get(client_name)
            if client:
                db.session.add(
                    ClientServiceRequest(
                        client_company_id=client.id,
                        submitted_by=admin.id,
                        request_type=request_type,
                        title=title,
                        description=f"Seeded Phase 3 request for {client.name}.",
                        requested_date=date.today() + timedelta(days=index),
                        location=client.location,
                        number_of_workers_requested=2 + index,
                        priority=priority,
                        status=["Submitted", "Under Review", "Approved", "In Progress", "Completed"][index],
                    )
                )

    if not Supplier.query.count():
        suppliers = [
            "Tema Industrial Supplies",
            "Accra Office Mart",
            "Safety First Ghana",
            "CleanPro Distributors",
            "General Goods Depot",
        ]
        for supplier_name in suppliers:
            db.session.add(
                Supplier(
                    name=supplier_name,
                    contact_person="Sales Desk",
                    phone="0240000000",
                    email=f"{supplier_name.lower().replace(' ', '.')}@example.com",
                    address="Accra, Ghana",
                    status="Active",
                )
            )
        db.session.flush()

    if not Product.query.count():
        supplier_ids = [supplier.id for supplier in Supplier.query.all()]
        products = [
            ("CLN-001", "Liquid Soap", "Cleaning Supplies", "Bottle", 45, 100, 20),
            ("CLN-002", "Disinfectant", "Cleaning Supplies", "Gallon", 90, 80, 15),
            ("CLN-003", "Mop Head", "Cleaning Supplies", "Each", 35, 60, 10),
            ("CLN-004", "Broom", "Cleaning Supplies", "Each", 25, 75, 10),
            ("OFF-001", "A4 Paper", "Office Supplies", "Ream", 60, 50, 10),
            ("OFF-002", "Printer Toner", "Office Supplies", "Each", 320, 12, 4),
            ("SAFE-001", "Safety Gloves", "Safety Equipment", "Pair", 18, 200, 40),
            ("SAFE-002", "Reflective Vest", "Safety Equipment", "Each", 55, 90, 20),
            ("GEN-001", "Plastic Chairs", "General Goods", "Each", 120, 25, 5),
            ("GEN-002", "Waste Bin", "General Goods", "Each", 85, 30, 8),
            ("CON-001", "Paper Towels", "Consumables", "Pack", 40, 70, 15),
            ("CON-002", "Hand Sanitizer", "Consumables", "Bottle", 35, 90, 20),
            ("CLN-005", "Floor Polish", "Cleaning Supplies", "Gallon", 150, 20, 5),
            ("OFF-003", "Pens", "Office Supplies", "Box", 30, 40, 8),
            ("GEN-003", "Buckets", "General Goods", "Each", 28, 55, 10),
        ]
        for index, (sku, name, category, unit, price, stock, reorder) in enumerate(products):
            db.session.add(
                Product(
                    product_name=name,
                    sku=sku,
                    category=category,
                    unit=unit,
                    default_unit_price=price,
                    current_stock=stock,
                    reorder_level=reorder,
                    supplier_id=supplier_ids[index % len(supplier_ids)] if supplier_ids else None,
                    status="Active",
                )
            )
        db.session.flush()

    if not GoodsSupplyOrder.query.count():
        products = Product.query.order_by(Product.id).all()
        for index, client in enumerate(list(clients.values())[:5]):
            order = GoodsSupplyOrder(
                client_company_id=client.id,
                order_number=next_order_number(),
                order_date=date.today() - timedelta(days=index),
                requested_delivery_date=date.today() + timedelta(days=3 + index),
                delivery_location=client.location or "Client site",
                status=["Submitted", "Approved", "Processing", "Delivered", "Draft"][index],
                notes="Seeded Phase 3 goods order.",
                created_by=operations.id if operations else admin.id,
            )
            db.session.add(order)
            db.session.flush()
            add_goods_order_item(order, products[index], 3 + index)
            add_goods_order_item(order, products[index + 1], 2 + index)
            recalculate_goods_order(order)
            if order.status == "Delivered":
                for item in order.items:
                    item.product.current_stock -= item.quantity
                    db.session.add(
                        InventoryMovement(
                            product_id=item.product_id,
                            movement_type="Stock Out",
                            quantity=item.quantity,
                            reference_type="GoodsSupplyOrder",
                            reference_id=order.id,
                            notes="Seeded delivered order.",
                            created_by=operations.id if operations else admin.id,
                        )
                    )

    if not Invoice.query.count():
        invoice_clients = [
            clients[name]
            for name in [
                "MSC Ghana Ltd",
                "Stellar Logistics",
                "Grimaldi Ghana Ltd",
                "ACS/GMT Shipping",
                "Portmarine Ltd",
            ]
            if name in clients
        ]
        for index, client in enumerate(invoice_clients):
            invoice = Invoice(
                client_company_id=client.id,
                invoice_number=next_invoice_number(),
                invoice_date=date.today() - timedelta(days=index * 3),
                due_date=date.today() + timedelta(days=14 - index),
                billing_period_start=date.today().replace(day=1),
                billing_period_end=date.today(),
                invoice_type=["Payroll/Labour Invoice", "Cleaning Services Invoice", "Goods Supply Invoice", "Mixed Services Invoice", "Goods Supply Invoice"][index],
                tax_amount=0,
                discount_amount=0,
                status="Sent",
                notes="Seeded Phase 3 invoice.",
                created_by=accounts.id if accounts else admin.id,
            )
            db.session.add(invoice)
            db.session.flush()
            add_invoice_item(invoice, f"{invoice.invoice_type} for {client.name}", 1, 1500 + (index * 350), invoice.invoice_type, None)
            recalculate_invoice(invoice)
        db.session.flush()

    if not InvoicePayment.query.count():
        for index, invoice in enumerate(Invoice.query.order_by(Invoice.id).offset(2).limit(3).all()):
            payment = InvoicePayment(
                invoice_id=invoice.id,
                payment_date=date.today() - timedelta(days=index),
                amount_paid=round(invoice.total_amount / 2, 2),
                payment_method="Bank Transfer",
                reference_number=f"PAY-SEED-{index + 1}",
                received_by=accounts.id if accounts else admin.id,
                notes="Seeded payment.",
            )
            db.session.add(payment)
            db.session.flush()
            recalculate_invoice(invoice)
