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
    employment_type = db.Column(db.String(80))
    service_line = db.Column(db.String(120))
    assigned_client = db.Column(db.String(160))
    client_company_id = db.Column(db.Integer, db.ForeignKey("client_company.id"))
    status = db.Column(db.String(40), default="Active")
    basic_salary = db.Column(db.Float, default=0)
    created_at = db.Column(db.DateTime, default=utc_now)

    client_company = db.relationship("ClientCompany", back_populates="employees")


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
    status = db.Column(db.String(40), default="Pending")
    recorded_by = db.Column(db.Integer, db.ForeignKey("user.id"))
    created_at = db.Column(db.DateTime, default=utc_now)

    recorder = db.relationship("User", foreign_keys=[recorded_by])
    payer = db.relationship("User", foreign_keys=[paid_by])
    expense_approver = db.relationship("User", foreign_keys=[approved_by])
    client_company = db.relationship("ClientCompany")


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
