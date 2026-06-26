from datetime import datetime, timezone

from flask_login import UserMixin
from werkzeug.security import check_password_hash, generate_password_hash

from app import db


def utc_now():
    return datetime.now(timezone.utc)


class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    email = db.Column(db.String(160), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(40), nullable=False, default="viewer")
    client_company_id = db.Column(db.Integer, db.ForeignKey("client_company.id"))

    client_company = db.relationship("ClientCompany")

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)


class ClientCompany(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(160), nullable=False, unique=True)
    contact_person = db.Column(db.String(120))
    phone = db.Column(db.String(40))
    email = db.Column(db.String(160))
    location = db.Column(db.String(120))
    service_type = db.Column(db.String(120))
    status = db.Column(db.String(20), nullable=False, default="Active")
    created_at = db.Column(db.DateTime, default=utc_now)

    employees = db.relationship("Employee", back_populates="client_company")
    payroll_runs = db.relationship("PayrollRun", back_populates="client_company")
    proposals = db.relationship("Proposal", back_populates="client_company")


class Employee(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    staff_id = db.Column(db.String(60), nullable=False, index=True)
    full_name = db.Column(db.String(160), nullable=False)
    phone = db.Column(db.String(40))
    ghana_card_number = db.Column(db.String(80))
    ssnit_number = db.Column(db.String(80))
    bank_name = db.Column(db.String(120))
    bank_account_number = db.Column(db.String(80))
    momo_number = db.Column(db.String(40))
    email = db.Column(db.String(160))
    employment_type = db.Column(db.String(80))
    service_line = db.Column(db.String(120))
    assigned_client = db.Column(db.String(160))
    client_company_id = db.Column(db.Integer, db.ForeignKey("client_company.id"))
    status = db.Column(db.String(40), default="Active")
    # Preferred payslip delivery channel (sms/whatsapp/email). Honored by "auto" routing,
    # which still falls back to whichever contact the worker actually has.
    preferred_channel = db.Column(db.String(16))
    # Roster-maintained department (free text). Sourced from the employee record, never payroll.
    department = db.Column(db.String(80))
    basic_salary = db.Column(db.Float, default=0)
    created_at = db.Column(db.DateTime, default=utc_now)
    updated_at = db.Column(db.DateTime, default=utc_now, onupdate=utc_now)

    def normalise_staff_id(self):
        """Normalise the staff_id (join key) before save: strip spaces, uppercase.

        "DCL 9" -> "DCL9", "DZ 048" -> "DZ048". Keeps payroll uploads and roster
        records resolvable to the same employee regardless of spacing/case.
        """
        import re

        if self.staff_id:
            self.staff_id = re.sub(r"\s+", "", str(self.staff_id).strip().upper())

    client_company = db.relationship("ClientCompany", back_populates="employees")
    deployments = db.relationship(
        "EmployeeDeployment",
        back_populates="employee",
        cascade="all, delete-orphan",
    )
    assignments = db.relationship("WorkerAssignment", back_populates="employee")
    attendance_records = db.relationship("AttendanceRecord", back_populates="employee")
    cleaning_jobs = db.relationship("CleaningJobWorker", back_populates="employee")


class EmployeeDeployment(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    employee_id = db.Column(db.Integer, db.ForeignKey("employee.id"), nullable=False, index=True)
    client_company_id = db.Column(db.Integer, db.ForeignKey("client_company.id"), nullable=False, index=True)
    role = db.Column(db.String(120), nullable=False)
    start_date = db.Column(db.Date, nullable=False)
    end_date = db.Column(db.Date)
    status = db.Column(db.String(40), nullable=False, default="Active")
    notes = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=utc_now)

    employee = db.relationship("Employee", back_populates="deployments")
    client_company = db.relationship("ClientCompany")


class PayrollRun(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    month = db.Column(db.String(20), nullable=False)
    year = db.Column(db.Integer, nullable=False)
    status = db.Column(db.String(40), nullable=False, default="Draft")
    created_by = db.Column(db.Integer, db.ForeignKey("user.id"))
    reviewed_by = db.Column(db.Integer, db.ForeignKey("user.id"))
    approved_by = db.Column(db.Integer, db.ForeignKey("user.id"))
    client_company_id = db.Column(db.Integer, db.ForeignKey("client_company.id"))
    total_workers = db.Column(db.Integer, default=0)
    total_rows_imported = db.Column(db.Integer, default=0)
    total_unique_workers = db.Column(db.Integer, default=0)
    duplicate_workers_found = db.Column(db.Integer, default=0)
    source_filename = db.Column(db.String(255))
    source_sheet_name = db.Column(db.String(160))
    detected_header_row = db.Column(db.Integer, default=0)
    import_mode = db.Column(db.String(40), default="single_client")
    import_type = db.Column(db.String(80), default="Single Company Upload")
    detected_company_name = db.Column(db.String(160))
    active_workers = db.Column(db.Integer, default=0)
    inactive_workers = db.Column(db.Integer, default=0)
    terminated_workers = db.Column(db.Integer, default=0)
    on_leave_workers = db.Column(db.Integer, default=0)
    unknown_status_workers = db.Column(db.Integer, default=0)
    total_gross_pay = db.Column(db.Float, default=0)
    total_deductions = db.Column(db.Float, default=0)
    total_net_pay = db.Column(db.Float, default=0)
    total_paye = db.Column(db.Float, default=0)
    total_ssnit = db.Column(db.Float, default=0)
    notes = db.Column(db.Text)
    # 'standard' | 'raw'. Null means no file has been uploaded for the run yet.
    # 'raw' marks a billable raw-hours import awaiting Chrisnat pay calculation.
    upload_type = db.Column(db.String(20), nullable=True)
    created_at = db.Column(db.DateTime, default=utc_now)
    reviewed_at = db.Column(db.DateTime)
    approved_at = db.Column(db.DateTime)
    rejected_at = db.Column(db.DateTime)

    client_company = db.relationship("ClientCompany", back_populates="payroll_runs")
    creator = db.relationship("User", foreign_keys=[created_by])
    reviewer = db.relationship("User", foreign_keys=[reviewed_by])
    approver = db.relationship("User", foreign_keys=[approved_by])
    items = db.relationship(
        "PayrollItem", back_populates="payroll_run", cascade="all, delete-orphan"
    )
    voucher = db.relationship(
        "PaymentVoucher", back_populates="payroll_run", uselist=False
    )
    remittances = db.relationship("Remittance", back_populates="payroll_run")

    @property
    def warning_count(self):
        return sum(1 for item in self.items if item.validation_status == "Warning")


class PayrollItem(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    payroll_run_id = db.Column(db.Integer, db.ForeignKey("payroll_run.id"))
    employee_id = db.Column(db.Integer, db.ForeignKey("employee.id"))
    staff_id = db.Column(db.String(60))
    full_name = db.Column(db.String(160))
    status = db.Column(db.String(40))
    service_line = db.Column(db.String(120))
    job_role = db.Column(db.String(120))
    payroll_month = db.Column(db.String(40))
    ssnit_number = db.Column(db.String(80))
    ghana_card_number = db.Column(db.String(80))
    bank_name = db.Column(db.String(120))
    bank_account_number = db.Column(db.String(80))
    momo_number = db.Column(db.String(40))
    email = db.Column(db.String(160))
    basic_salary = db.Column(db.Float, default=0)
    transport_allowance = db.Column(db.Float, default=0)
    housing_allowance = db.Column(db.Float, default=0)
    overtime_hours = db.Column(db.Float, default=0)
    overtime_pay = db.Column(db.Float, default=0)
    other_allowances = db.Column(db.Float, default=0)
    gross_pay = db.Column(db.Float, default=0)
    paye = db.Column(db.Float, default=0)
    ssnit = db.Column(db.Float, default=0)
    tier_2_pension = db.Column(db.Float, default=0)
    loan_deduction = db.Column(db.Float, default=0)
    other_deductions = db.Column(db.Float, default=0)
    total_deductions = db.Column(db.Float, default=0)
    net_pay = db.Column(db.Float, default=0)
    validation_status = db.Column(db.String(40), default="OK")
    warning_notes = db.Column(db.Text)

    payroll_run = db.relationship("PayrollRun", back_populates="items")
    employee = db.relationship("Employee")


class PaymentVoucher(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    payroll_run_id = db.Column(db.Integer, db.ForeignKey("payroll_run.id"))
    voucher_number = db.Column(db.String(80), unique=True, nullable=False)
    total_amount = db.Column(db.Float, default=0)
    gross_payroll = db.Column(db.Float, default=0)
    total_deductions = db.Column(db.Float, default=0)
    net_amount_payable = db.Column(db.Float, default=0)
    prepared_by = db.Column(db.Integer, db.ForeignKey("user.id"))
    reviewed_by = db.Column(db.Integer, db.ForeignKey("user.id"))
    approved_by = db.Column(db.Integer, db.ForeignKey("user.id"))
    status = db.Column(db.String(40), default="Pending Payment")
    created_at = db.Column(db.DateTime, default=utc_now)
    date_approved = db.Column(db.DateTime)
    date_paid = db.Column(db.DateTime)

    payroll_run = db.relationship("PayrollRun", back_populates="voucher")
    preparer = db.relationship("User", foreign_keys=[prepared_by])
    reviewer = db.relationship("User", foreign_keys=[reviewed_by])
    approver = db.relationship("User", foreign_keys=[approved_by])


class Remittance(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    payroll_run_id = db.Column(db.Integer, db.ForeignKey("payroll_run.id"))
    remittance_type = db.Column(db.String(20), nullable=False)
    amount_due = db.Column(db.Float, default=0)
    due_date = db.Column(db.Date)
    status = db.Column(db.String(40), default="Pending")
    date_paid = db.Column(db.Date)
    payment_reference = db.Column(db.String(120))
    notes = db.Column(db.Text)

    payroll_run = db.relationship("PayrollRun", back_populates="remittances")


class Expense(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(180))
    expense_date = db.Column(db.Date, nullable=False)
    category = db.Column(db.String(80), nullable=False)
    description = db.Column(db.String(255), nullable=False)
    amount = db.Column(db.Float, default=0)
    payment_method = db.Column(db.String(80))
    receipt_reference = db.Column(db.String(120))
    receipt_attachment = db.Column(db.String(255))
    paid_by = db.Column(db.Integer, db.ForeignKey("user.id"))
    approved_by = db.Column(db.Integer, db.ForeignKey("user.id"))
    client_company_id = db.Column(db.Integer, db.ForeignKey("client_company.id"))
    payroll_run_id = db.Column(db.Integer, db.ForeignKey("payroll_run.id"))
    status = db.Column(db.String(40), default="Pending")
    recorded_by = db.Column(db.Integer, db.ForeignKey("user.id"))
    created_at = db.Column(db.DateTime, default=utc_now)

    recorder = db.relationship("User", foreign_keys=[recorded_by])
    payer = db.relationship("User", foreign_keys=[paid_by])
    expense_approver = db.relationship("User", foreign_keys=[approved_by])
    client_company = db.relationship("ClientCompany")
    payroll_run = db.relationship("PayrollRun")


class Proposal(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    client_company_id = db.Column(db.Integer, db.ForeignKey("client_company.id"))
    title = db.Column(db.String(180), nullable=False)
    service_summary = db.Column(db.Text, nullable=False)
    proposed_amount = db.Column(db.Float, default=0)
    status = db.Column(db.String(40), default="Draft")
    drafted_by = db.Column(db.Integer, db.ForeignKey("user.id"))
    created_at = db.Column(db.DateTime, default=utc_now)

    client_company = db.relationship("ClientCompany", back_populates="proposals")
    drafter = db.relationship("User")


class ImportBatch(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    client_company_id = db.Column(db.Integer, db.ForeignKey("client_company.id"))
    payroll_month = db.Column(db.String(20), nullable=False)
    payroll_year = db.Column(db.Integer, nullable=False)
    uploaded_by = db.Column(db.Integer, db.ForeignKey("user.id"))
    uploaded_at = db.Column(db.DateTime, default=utc_now)
    original_filename = db.Column(db.String(255))
    import_mode = db.Column(db.String(40), default="single_client")
    source_sheet_name = db.Column(db.String(160))
    status = db.Column(db.String(40), default="Previewed")
    total_rows = db.Column(db.Integer, default=0)
    valid_rows = db.Column(db.Integer, default=0)
    invalid_rows = db.Column(db.Integer, default=0)
    total_workers = db.Column(db.Integer, default=0)
    gross_total = db.Column(db.Float, default=0)
    net_total = db.Column(db.Float, default=0)
    paye_total = db.Column(db.Float, default=0)
    ssnit_total = db.Column(db.Float, default=0)
    validation_summary = db.Column(db.Text)
    payload_json = db.Column(db.Text)
    payroll_run_id = db.Column(db.Integer, db.ForeignKey("payroll_run.id"))

    client_company = db.relationship("ClientCompany")
    uploader = db.relationship("User")
    payroll_run = db.relationship("PayrollRun")


class AuditTrail(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"))
    user_role = db.Column(db.String(40))
    action = db.Column(db.String(120), nullable=False)
    related_record_type = db.Column(db.String(80))
    related_record_id = db.Column(db.Integer)
    notes = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=utc_now)

    user = db.relationship("User")


class WorkerAssignment(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    employee_id = db.Column(db.Integer, db.ForeignKey("employee.id"), nullable=False, index=True)
    client_company_id = db.Column(db.Integer, db.ForeignKey("client_company.id"), nullable=False, index=True)
    role = db.Column(db.String(120), nullable=False)
    site_location = db.Column(db.String(160))
    assignment_start_date = db.Column(db.Date, nullable=False)
    assignment_end_date = db.Column(db.Date)
    status = db.Column(db.String(40), nullable=False, default="Pending")
    shift_type = db.Column(db.String(40), default="Full Day")
    notes = db.Column(db.Text)
    created_by = db.Column(db.Integer, db.ForeignKey("user.id"))
    created_at = db.Column(db.DateTime, default=utc_now)
    updated_at = db.Column(db.DateTime, default=utc_now, onupdate=utc_now)

    employee = db.relationship("Employee", back_populates="assignments")
    client_company = db.relationship("ClientCompany")
    creator = db.relationship("User")
    attendance_records = db.relationship("AttendanceRecord", back_populates="assignment")


class AttendanceRecord(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    employee_id = db.Column(db.Integer, db.ForeignKey("employee.id"), nullable=False, index=True)
    client_company_id = db.Column(db.Integer, db.ForeignKey("client_company.id"), nullable=False, index=True)
    assignment_id = db.Column(db.Integer, db.ForeignKey("worker_assignment.id"))
    work_date = db.Column(db.Date, nullable=False, index=True)
    clock_in = db.Column(db.Time)
    clock_out = db.Column(db.Time)
    hours_worked = db.Column(db.Float)
    overtime_hours = db.Column(db.Float, default=0)
    attendance_status = db.Column(db.String(40), nullable=False, default="Unknown")
    remarks = db.Column(db.Text)
    recorded_by = db.Column(db.Integer, db.ForeignKey("user.id"))
    created_at = db.Column(db.DateTime, default=utc_now)
    updated_at = db.Column(db.DateTime, default=utc_now, onupdate=utc_now)

    employee = db.relationship("Employee", back_populates="attendance_records")
    client_company = db.relationship("ClientCompany")
    assignment = db.relationship("WorkerAssignment", back_populates="attendance_records")
    recorder = db.relationship("User")


class CleaningJob(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    client_company_id = db.Column(db.Integer, db.ForeignKey("client_company.id"), nullable=False, index=True)
    job_title = db.Column(db.String(180), nullable=False)
    job_type = db.Column(db.String(80), nullable=False, default="Regular Cleaning")
    location = db.Column(db.String(180))
    scheduled_date = db.Column(db.Date, nullable=False, index=True)
    start_time = db.Column(db.Time)
    end_time = db.Column(db.Time)
    supervisor_id = db.Column(db.Integer, db.ForeignKey("user.id"))
    status = db.Column(db.String(40), nullable=False, default="Scheduled")
    checklist = db.Column(db.Text)
    notes = db.Column(db.Text)
    created_by = db.Column(db.Integer, db.ForeignKey("user.id"))
    created_at = db.Column(db.DateTime, default=utc_now)
    updated_at = db.Column(db.DateTime, default=utc_now, onupdate=utc_now)

    client_company = db.relationship("ClientCompany")
    supervisor = db.relationship("User", foreign_keys=[supervisor_id])
    creator = db.relationship("User", foreign_keys=[created_by])
    workers = db.relationship("CleaningJobWorker", back_populates="cleaning_job", cascade="all, delete-orphan")
    completion_report = db.relationship("JobCompletionReport", back_populates="cleaning_job", uselist=False)


class CleaningJobWorker(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    cleaning_job_id = db.Column(db.Integer, db.ForeignKey("cleaning_job.id"), nullable=False, index=True)
    employee_id = db.Column(db.Integer, db.ForeignKey("employee.id"), nullable=False, index=True)
    role = db.Column(db.String(60), nullable=False, default="Cleaner")
    attendance_status = db.Column(db.String(40), default="Unknown")
    notes = db.Column(db.Text)

    cleaning_job = db.relationship("CleaningJob", back_populates="workers")
    employee = db.relationship("Employee", back_populates="cleaning_jobs")


class JobCompletionReport(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    cleaning_job_id = db.Column(db.Integer, db.ForeignKey("cleaning_job.id"), nullable=False, unique=True)
    completed_by = db.Column(db.Integer, db.ForeignKey("user.id"))
    completion_status = db.Column(db.String(80), nullable=False)
    workers_present = db.Column(db.Integer, default=0)
    workers_absent = db.Column(db.Integer, default=0)
    checklist_completed = db.Column(db.Boolean, default=False)
    issues_found = db.Column(db.Text)
    client_feedback = db.Column(db.Text)
    supervisor_comments = db.Column(db.Text)
    completed_at = db.Column(db.DateTime, default=utc_now)
    created_at = db.Column(db.DateTime, default=utc_now)

    cleaning_job = db.relationship("CleaningJob", back_populates="completion_report")
    completer = db.relationship("User")


class ClientServiceRequest(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    client_company_id = db.Column(db.Integer, db.ForeignKey("client_company.id"), nullable=False, index=True)
    submitted_by = db.Column(db.Integer, db.ForeignKey("user.id"))
    request_type = db.Column(db.String(80), nullable=False)
    title = db.Column(db.String(180), nullable=False)
    description = db.Column(db.Text)
    requested_date = db.Column(db.Date)
    preferred_start_time = db.Column(db.Time)
    preferred_end_time = db.Column(db.Time)
    location = db.Column(db.String(180))
    number_of_workers_requested = db.Column(db.Integer, default=0)
    priority = db.Column(db.String(40), default="Normal")
    status = db.Column(db.String(40), default="Submitted")
    internal_notes = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=utc_now)
    updated_at = db.Column(db.DateTime, default=utc_now, onupdate=utc_now)

    client_company = db.relationship("ClientCompany")
    submitter = db.relationship("User")


class Invoice(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    client_company_id = db.Column(db.Integer, db.ForeignKey("client_company.id"), nullable=False, index=True)
    invoice_number = db.Column(db.String(80), unique=True, nullable=False)
    invoice_date = db.Column(db.Date, nullable=False)
    due_date = db.Column(db.Date)
    billing_period_start = db.Column(db.Date)
    billing_period_end = db.Column(db.Date)
    invoice_type = db.Column(db.String(80), nullable=False)
    subtotal = db.Column(db.Float, default=0)
    tax_amount = db.Column(db.Float, default=0)
    discount_amount = db.Column(db.Float, default=0)
    total_amount = db.Column(db.Float, default=0)
    amount_paid = db.Column(db.Float, default=0)
    balance_due = db.Column(db.Float, default=0)
    status = db.Column(db.String(40), default="Draft")
    notes = db.Column(db.Text)
    created_by = db.Column(db.Integer, db.ForeignKey("user.id"))
    created_at = db.Column(db.DateTime, default=utc_now)
    updated_at = db.Column(db.DateTime, default=utc_now, onupdate=utc_now)

    client_company = db.relationship("ClientCompany")
    creator = db.relationship("User")
    items = db.relationship("InvoiceItem", back_populates="invoice", cascade="all, delete-orphan")
    payments = db.relationship("InvoicePayment", back_populates="invoice", cascade="all, delete-orphan")


class InvoiceItem(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    invoice_id = db.Column(db.Integer, db.ForeignKey("invoice.id"), nullable=False, index=True)
    description = db.Column(db.String(255), nullable=False)
    quantity = db.Column(db.Float, default=1)
    unit_price = db.Column(db.Float, default=0)
    line_total = db.Column(db.Float, default=0)
    source_type = db.Column(db.String(80), default="Manual")
    source_id = db.Column(db.Integer)

    invoice = db.relationship("Invoice", back_populates="items")


class InvoicePayment(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    invoice_id = db.Column(db.Integer, db.ForeignKey("invoice.id"), nullable=False, index=True)
    payment_date = db.Column(db.Date, nullable=False)
    amount_paid = db.Column(db.Float, default=0)
    payment_method = db.Column(db.String(40), default="Bank Transfer")
    reference_number = db.Column(db.String(120))
    received_by = db.Column(db.Integer, db.ForeignKey("user.id"))
    notes = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=utc_now)

    invoice = db.relationship("Invoice", back_populates="payments")
    receiver = db.relationship("User")


class Supplier(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(160), nullable=False)
    contact_person = db.Column(db.String(120))
    phone = db.Column(db.String(40))
    email = db.Column(db.String(160))
    address = db.Column(db.Text)
    status = db.Column(db.String(40), default="Active")
    created_at = db.Column(db.DateTime, default=utc_now)

    products = db.relationship("Product", back_populates="supplier")


class Product(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    product_name = db.Column(db.String(180), nullable=False)
    sku = db.Column(db.String(80), unique=True, nullable=False)
    category = db.Column(db.String(80), default="General Goods")
    unit = db.Column(db.String(40), default="Each")
    default_unit_price = db.Column(db.Float, default=0)
    current_stock = db.Column(db.Float, default=0)
    reorder_level = db.Column(db.Float, default=0)
    supplier_id = db.Column(db.Integer, db.ForeignKey("supplier.id"))
    status = db.Column(db.String(40), default="Active")
    created_at = db.Column(db.DateTime, default=utc_now)

    supplier = db.relationship("Supplier", back_populates="products")
    movements = db.relationship("InventoryMovement", back_populates="product")


class GoodsSupplyOrder(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    client_company_id = db.Column(db.Integer, db.ForeignKey("client_company.id"), nullable=False, index=True)
    order_number = db.Column(db.String(80), unique=True, nullable=False)
    order_date = db.Column(db.Date, nullable=False)
    requested_delivery_date = db.Column(db.Date)
    delivery_location = db.Column(db.String(180))
    status = db.Column(db.String(40), default="Draft")
    subtotal = db.Column(db.Float, default=0)
    tax_amount = db.Column(db.Float, default=0)
    total_amount = db.Column(db.Float, default=0)
    notes = db.Column(db.Text)
    created_by = db.Column(db.Integer, db.ForeignKey("user.id"))
    related_service_request_id = db.Column(db.Integer, db.ForeignKey("client_service_request.id"))
    created_at = db.Column(db.DateTime, default=utc_now)
    updated_at = db.Column(db.DateTime, default=utc_now, onupdate=utc_now)

    client_company = db.relationship("ClientCompany")
    creator = db.relationship("User")
    service_request = db.relationship("ClientServiceRequest")
    items = db.relationship("GoodsSupplyOrderItem", back_populates="goods_supply_order", cascade="all, delete-orphan")


class GoodsSupplyOrderItem(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    goods_supply_order_id = db.Column(db.Integer, db.ForeignKey("goods_supply_order.id"), nullable=False, index=True)
    product_id = db.Column(db.Integer, db.ForeignKey("product.id"), nullable=False)
    description = db.Column(db.String(255))
    quantity = db.Column(db.Float, default=1)
    unit_price = db.Column(db.Float, default=0)
    line_total = db.Column(db.Float, default=0)

    goods_supply_order = db.relationship("GoodsSupplyOrder", back_populates="items")
    product = db.relationship("Product")


class InventoryMovement(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    product_id = db.Column(db.Integer, db.ForeignKey("product.id"), nullable=False, index=True)
    movement_type = db.Column(db.String(40), nullable=False)
    quantity = db.Column(db.Float, default=0)
    reference_type = db.Column(db.String(80))
    reference_id = db.Column(db.Integer)
    notes = db.Column(db.Text)
    created_by = db.Column(db.Integer, db.ForeignKey("user.id"))
    created_at = db.Column(db.DateTime, default=utc_now)

    product = db.relationship("Product", back_populates="movements")
    creator = db.relationship("User")


# ---------------------------------------------------------------------------
# Payslip distribution (multi-channel delivery of payslip breakdowns).
# A PayrollItem IS the payslip; PayslipDelivery records one send attempt of it
# over a channel so failures are visible and retryable, never silently lost.
# ---------------------------------------------------------------------------

DELIVERY_PENDING = "pending"
DELIVERY_SENT = "sent"
DELIVERY_FAILED = "failed"

CHANNEL_SMS = "sms"
CHANNEL_WHATSAPP = "whatsapp"
CHANNEL_EMAIL = "email"
CHANNEL_AUTO = "auto"
# Concrete channels in fallback-preference order (worker-centric: phone first, since
# Chrisnat workers are reached by momo/phone far more often than email).
DELIVERY_CHANNELS = (CHANNEL_SMS, CHANNEL_WHATSAPP, CHANNEL_EMAIL)


class PayslipDelivery(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    payroll_item_id = db.Column(
        db.Integer, db.ForeignKey("payroll_item.id"), nullable=False, index=True
    )
    payroll_run_id = db.Column(
        db.Integer, db.ForeignKey("payroll_run.id"), nullable=False, index=True
    )
    channel = db.Column(db.String(16), nullable=False, default=CHANNEL_SMS)
    recipient = db.Column(db.String(120))
    status = db.Column(db.String(16), nullable=False, default=DELIVERY_PENDING)
    provider = db.Column(db.String(64))
    error = db.Column(db.String(512))
    attempts = db.Column(db.Integer, nullable=False, default=0)
    sent_at = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=utc_now)
    updated_at = db.Column(db.DateTime, default=utc_now, onupdate=utc_now)

    payroll_item = db.relationship("PayrollItem")
    payroll_run = db.relationship("PayrollRun")


class IdempotencyKey(db.Model):
    """Stores the result of a mutating action keyed by a client-supplied nonce, so a
    retried/double-clicked "Send" replays the original response instead of re-sending."""

    id = db.Column(db.Integer, primary_key=True)
    key = db.Column(db.String(255), nullable=False, unique=True, index=True)
    response_json = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=utc_now)


class RawPayEntry(db.Model):
    """One imported raw-hours line per (payroll run, employee, pay code).

    Stores hours only — gross pay is calculated later by a Chrisnat operator,
    so this table deliberately holds no money fields."""

    __tablename__ = "raw_pay_entries"

    id = db.Column(db.Integer, primary_key=True)
    payroll_run_id = db.Column(
        db.Integer, db.ForeignKey("payroll_run.id"), nullable=False
    )
    employee_id_str = db.Column(db.String(20), nullable=False)  # normalised, e.g. "DCL9"
    pay_code = db.Column(db.String(20), nullable=False)  # e.g. "ABNH01"
    hours = db.Column(db.Numeric(8, 2), nullable=False)
    created_at = db.Column(db.DateTime, default=utc_now)

    run = db.relationship("PayrollRun", backref="raw_entries")
