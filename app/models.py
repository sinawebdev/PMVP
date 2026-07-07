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
    bank_branch = db.Column(db.String(120))
    bank_account_number = db.Column(db.String(80))
    momo_number = db.Column(db.String(40))
    email = db.Column(db.String(160))
    # GRA Taxpayer Identification Number — captured when known, exported on the
    # GRA PAYE schedule; no dedicated capture workflow yet (hand-fill allowed).
    tin = db.Column(db.String(80))
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
    # GRA tax relief (marriage/dependents/disability/age) as a flat monthly
    # amount subtracted from ordinary taxable income before the PAYE bands.
    # Standing employee data, not a monthly input. The amount per relief
    # category is legally set — enter it from the current GRA circular; the
    # code deliberately hardcodes no category figures.
    tax_relief_monthly = db.Column(db.Float, default=0)
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
    # Employer-side SSF (13%) for the whole run — a Chrisnat cost, not a
    # payslip deduction, persisted so remittances stop re-deriving it.
    total_ssnit_employer = db.Column(db.Float, default=0)
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
    bank_branch = db.Column(db.String(120))
    bank_account_number = db.Column(db.String(80))
    momo_number = db.Column(db.String(40))
    email = db.Column(db.String(160))
    basic_salary = db.Column(db.Float, default=0)
    transport_allowance = db.Column(db.Float, default=0)
    housing_allowance = db.Column(db.Float, default=0)
    # Dedicated earnings columns for the ACS/VBA payslip shape.
    medical_allowance = db.Column(db.Float, default=0)
    productivity_bonus = db.Column(db.Float, default=0)
    # One-off end-of-year/13th-month bonus — kept separate from the monthly
    # productivity bonus; both share the ANNUAL concession cap on StatutoryRate.
    end_of_year_bonus = db.Column(db.Float, default=0)
    overtime_hours = db.Column(db.Float, default=0)
    overtime_pay = db.Column(db.Float, default=0)
    other_allowances = db.Column(db.Float, default=0)
    # Arrears/back-pay for prior periods, sourced from the upload. Joins this
    # period's taxable gross and is taxed normally — no deferred treatment.
    pay_difference = db.Column(db.Float, default=0)
    gross_pay = db.Column(db.Float, default=0)
    paye = db.Column(db.Float, default=0)
    ssnit = db.Column(db.Float, default=0)
    # Employer SSF (13% of basic) — not a payslip deduction; persisted for the
    # Wages Sheet export and employer remittance figures.
    ssf_employer = db.Column(db.Float, default=0)
    # Calculator outputs previously discarded, persisted for the GRA schedule:
    # concessionary overtime/bonus tax components, the bonus excess that joins
    # ordinary taxable income, and the chargeable income itself.
    overtime_tax = db.Column(db.Float, default=0)
    bonus_tax = db.Column(db.Float, default=0)
    bonus_excess = db.Column(db.Float, default=0)
    taxable_income = db.Column(db.Float, default=0)
    # Derived figures — always computed from basic salary and the active
    # StatutoryRate, never read from an upload.
    net_basic_wage = db.Column(db.Float, default=0)
    annual_salary = db.Column(db.Float, default=0)
    annual_salary_15pct = db.Column(db.Float, default=0)
    tier_2_pension = db.Column(db.Float, default=0)
    # "PF FUND / EMPLOYEE" (ACS RAW DATA column AA): a pre-tax deduction —
    # it reduces taxable income before PAYE as well as net pay.
    pf_fund_employee = db.Column(db.Float, default=0)
    loan_deduction = db.Column(db.Float, default=0)
    # Cash advanced to the worker with this payroll — opposite cash direction
    # to loan_deduction (adds to net pay, not taxable income).
    loan_advance = db.Column(db.Float, default=0)
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


class StatutoryRate(db.Model):
    """One effective-dated version of Ghana's statutory payroll rates.

    A calculation always uses the version active as of the payroll run's
    period — never "whatever's current" — so past runs stay reproducible
    when rates change. Rates live here, never as Python constants (the
    source Excel hardcoded the tax formula into a cell; don't repeat that).

    ``paye_bands_json`` is an ordered list (highest threshold first) of
    ``{"over": t, "rate": r, "base": b}`` entries meaning: if monthly
    taxable income >= t, tax = (taxable - t) * r + b. Income below the
    lowest threshold is untaxed.
    """

    __tablename__ = "statutory_rates"

    id = db.Column(db.Integer, primary_key=True)
    effective_from = db.Column(db.Date, nullable=False, unique=True, index=True)
    ssf_employee_rate = db.Column(db.Float, nullable=False)  # e.g. 0.055
    ssf_employer_rate = db.Column(db.Float, nullable=False)  # e.g. 0.13
    paye_bands_json = db.Column(db.Text, nullable=False)
    # Concessionary flat-rate treatment under Ghana PAYE — overtime and bonus
    # are taxed separately from the marginal bands, not blended into them.
    overtime_rate_low = db.Column(db.Float, nullable=False, default=0.05)
    overtime_rate_high = db.Column(db.Float, nullable=False, default=0.10)
    # Overtime up to this fraction of basic MONTHLY salary taxes at the low rate.
    overtime_basic_threshold = db.Column(db.Float, nullable=False, default=0.50)
    bonus_rate = db.Column(db.Float, nullable=False, default=0.05)
    # Bonus up to this fraction of ANNUAL basic salary taxes at bonus_rate;
    # the excess joins ordinary taxable income.
    bonus_annual_basic_threshold = db.Column(db.Float, nullable=False, default=0.15)
    notes = db.Column(db.Text)
    created_by = db.Column(db.Integer, db.ForeignKey("user.id"))
    created_at = db.Column(db.DateTime, default=utc_now)

    creator = db.relationship("User")

    @property
    def paye_bands(self):
        import json

        return json.loads(self.paye_bands_json or "[]")

    @classmethod
    def active_for(cls, on_date):
        """The rate version in force on ``on_date`` (latest effective_from <= date)."""
        return (
            cls.query.filter(cls.effective_from <= on_date)
            .order_by(cls.effective_from.desc())
            .first()
        )

    def compute_paye(self, taxable_income):
        """Monthly PAYE for ``taxable_income`` using this version's bands.
        Decimal arithmetic with ROUND_HALF_UP — a marginal result landing
        exactly on a half-pesewa (e.g. 56.125) rounds up, never to-even."""
        from app.money import D, money

        taxable_income = D(taxable_income)
        for band in self.paye_bands:
            over = D(band["over"])
            if taxable_income >= over:
                return money(
                    (taxable_income - over) * D(band["rate"]) + D(band["base"])
                )
        return 0.0

    def compute_overtime_tax(self, overtime_pay, basic_salary):
        """Concessionary overtime tax: the portion up to
        ``overtime_basic_threshold`` of basic MONTHLY salary at the low rate,
        the excess at the high rate. Never enters the marginal bands.
        Full precision inside, rounded once at the end, matching the source
        workbook formulas (per-component rounding disagrees by a pesewa on
        real rows)."""
        from app.money import D, money

        overtime_pay = D(overtime_pay)
        if overtime_pay <= 0:
            return 0.0
        cap = D(basic_salary) * D(self.overtime_basic_threshold)
        low_portion = min(overtime_pay, cap)
        high_portion = max(overtime_pay - cap, D(0))
        return money(
            low_portion * D(self.overtime_rate_low)
            + high_portion * D(self.overtime_rate_high)
        )

    def split_bonus(self, bonus, basic_salary, already_used=0):
        """(concessionary tax, excess) for a one-off bonus: the portion up to
        ``bonus_annual_basic_threshold`` of ANNUAL basic salary is taxed flat
        at ``bonus_rate``; the excess joins ordinary taxable income.

        ``already_used`` is bonus-concession cedis this employee already
        consumed in OTHER payroll runs within the same tax year — the cap is
        annual, not per-run, so what's left here is this run's cap minus
        whatever earlier runs already used. Caller sums that figure (see
        ``bonus_concession_used_ytd`` in payroll_calculations); this assumes
        basic salary doesn't change mid-year, since the cap is derived from
        THIS run's basic salary, not stored separately per tax year."""
        from app.money import D, money

        bonus = D(bonus)
        if bonus <= 0:
            return 0.0, 0.0
        cap = D(basic_salary) * D(12) * D(self.bonus_annual_basic_threshold)
        remaining_cap = max(cap - D(already_used), D(0))
        concession = min(bonus, remaining_cap)
        excess = max(bonus - remaining_cap, D(0))
        return money(concession * D(self.bonus_rate)), money(excess)


class WageRateProfile(db.Model):
    """Hourly rate for one pay-code category, per client (employee row = override).

    Rates are the client's own pay structure (e.g. the DZ workbook's O.T./rate
    columns) — client-specific numbers entered as data, never hardcoded.
    ``employee_id`` NULL means the client-wide default for that pay code; a row
    with an employee_id overrides the default for that worker only.

    ``category`` drives the statutory treatment of amounts earned under the
    pay code: 'basic' (normal hours — attracts SSF and ordinary PAYE),
    'overtime' (concessionary flat-rate tax), 'bonus' (concessionary up to the
    annual-basic cap), 'allowance' (shift allowances etc. — ordinary taxable
    income, no SSF).
    """

    CATEGORY_BASIC = "basic"
    CATEGORY_OVERTIME = "overtime"
    CATEGORY_BONUS = "bonus"
    CATEGORY_ALLOWANCE = "allowance"
    CATEGORIES = (CATEGORY_BASIC, CATEGORY_OVERTIME, CATEGORY_BONUS, CATEGORY_ALLOWANCE)

    __tablename__ = "wage_rate_profiles"
    __table_args__ = (
        db.UniqueConstraint(
            "client_company_id", "employee_id", "pay_code",
            name="uq_wage_rate_scope_code",
        ),
    )

    id = db.Column(db.Integer, primary_key=True)
    client_company_id = db.Column(
        db.Integer, db.ForeignKey("client_company.id"), nullable=False, index=True
    )
    employee_id = db.Column(
        db.Integer, db.ForeignKey("employee.id"), nullable=True, index=True
    )
    pay_code = db.Column(db.String(20), nullable=False)  # matches RawPayEntry.pay_code
    hourly_rate = db.Column(db.Float, nullable=False)
    category = db.Column(db.String(20), nullable=False, default=CATEGORY_BASIC)
    description = db.Column(db.String(120))  # e.g. "Normal hours", "Saturday OT"
    created_at = db.Column(db.DateTime, default=utc_now)
    updated_at = db.Column(db.DateTime, default=utc_now, onupdate=utc_now)

    client_company = db.relationship("ClientCompany")
    employee = db.relationship("Employee")

    @classmethod
    def profile_for(cls, client_company_id, employee_id, pay_code):
        """Employee-specific profile if one exists, else the client default; None if neither."""
        if employee_id is not None:
            override = cls.query.filter_by(
                client_company_id=client_company_id,
                employee_id=employee_id,
                pay_code=pay_code,
            ).first()
            if override:
                return override
        return cls.query.filter_by(
            client_company_id=client_company_id,
            employee_id=None,
            pay_code=pay_code,
        ).first()

    @classmethod
    def rate_for(cls, client_company_id, employee_id, pay_code):
        """Hourly rate resolved via :meth:`profile_for`; None if unconfigured."""
        profile = cls.profile_for(client_company_id, employee_id, pay_code)
        return profile.hourly_rate if profile else None


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
