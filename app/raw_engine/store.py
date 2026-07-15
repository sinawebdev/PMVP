"""Persist a confirmed :class:`~app.raw_engine.seed.SeedContext` in one
transaction.

Guarantees for the confirm step:
  * **Transactional** — every employee, rate profile and membership flag is
    written inside a single transaction with exactly one commit at the end. Any
    failure mid-write rolls the whole thing back, so a forced mid-failure
    leaves zero rows (GATE 1).
  * **Idempotent** — employees are upserted by (client, staff_id) and each
    employee's raw-element rate profiles are replaced wholesale, so re-seeding
    the same workbook updates in place and never duplicates
    (``uq_wage_rate_scope_code``).
  * **Input only** — writes master fields, basic wage, ICU membership and the
    per-employee rate table; never a computed pay figure.
"""
import hashlib
import os
import shutil
from dataclasses import dataclass

from app import db
from app.models import Employee, PayrollItem, RawUploadArchive, WageRateProfile
from app.money import money
from app.raw_engine.cleaning import normalise_emp_id
from app.raw_engine.mapping import ELEMENTS

# Canonical pay codes this engine owns on an employee — used to replace the
# raw rate table wholesale on re-seed (idempotent "replace, never append").
RAW_PAY_CODES = frozenset(e[0] for e in ELEMENTS)


@dataclass
class SeedResult:
    employees_created: int = 0
    employees_updated: int = 0
    rate_profiles_written: int = 0
    icu_members: int = 0
    workbook_preserved_to: str = ""
    archive_id: int = None


def persist_seed(
    context,
    source_path=None,
    preserve=True,
    run=None,
    source_bytes=None,
    source_filename=None,
) -> SeedResult:
    """Write ``context`` to the database in a single transaction and return a
    :class:`SeedResult`. Rolls back and re-raises on any error.

    When ``run`` and ``source_bytes`` are given (the web seed-confirm path), the
    original workbook bytes are archived to :class:`RawUploadArchive` **inside
    this transaction** — a preservation failure rolls the whole seed back, so
    seeded context can never exist without its source workbook. ``source_path`` +
    ``preserve`` remains the best-effort file-copy fallback for library callers
    without a run.
    """
    result = SeedResult()
    try:
        cid = context.client_company_id
        existing = {
            e.staff_id: e
            for e in Employee.query.filter_by(client_company_id=cid).all()
        }

        for emp in context.employees:
            _upsert_employee(cid, emp, existing, result)

        db.session.flush()

        if run is not None and source_bytes is not None:
            # Durable, FATAL preservation: any failure here aborts the seed.
            archive = archive_upload(run, source_filename, source_bytes, kind="seed")
            result.archive_id = archive.id
        elif preserve and source_path:
            result.workbook_preserved_to = _preserve_workbook(source_path, context)

        from app.audit import record_audit

        record_audit(
            "Raw payroll seed confirmed",
            None,
            f"Client {cid}: {result.employees_created} employees created, "
            f"{result.employees_updated} updated, {result.rate_profiles_written} "
            f"rate rows, {result.icu_members} ICU members.",
        )
        db.session.commit()
        return result
    except Exception:
        db.session.rollback()
        raise


def archive_upload(run, filename, content, kind="seed") -> RawUploadArchive:
    """Persist the original upload bytes for ``run`` with an integrity hash.
    Idempotent: replaces any existing archive for the run. Adds to the session
    and flushes (assigning ``id``) but does not commit — the caller's transaction
    owns the commit, so a seed and its archive succeed or fail together."""
    if content is None:
        raise ValueError("Cannot archive an empty upload — no bytes provided.")
    RawUploadArchive.query.filter_by(payroll_run_id=run.id).delete()
    db.session.flush()
    archive = RawUploadArchive(
        payroll_run_id=run.id,
        filename=filename,
        content=content,
        sha256=hashlib.sha256(content).hexdigest(),
        upload_kind=kind,
    )
    db.session.add(archive)
    db.session.flush()
    return archive


def _upsert_employee(client_company_id, emp, existing, result):
    """Create or update one Employee and replace its raw-element rate profiles.
    Flushes (never commits) so a new employee's id is available for its
    WageRateProfile foreign keys."""
    record = existing.get(emp.staff_id)
    if record is None:
        record = Employee(
            staff_id=emp.staff_id, client_company_id=client_company_id
        )
        db.session.add(record)
        result.employees_created += 1
    else:
        result.employees_updated += 1

    record.full_name = emp.full_name
    record.basic_salary = emp.basic_salary
    record.icu_member = emp.icu_member
    record.ghana_card_number = emp.ghana_card_number or record.ghana_card_number
    record.ssnit_number = emp.ssnit_number or record.ssnit_number
    record.bank_name = emp.bank_name or record.bank_name
    record.bank_branch = emp.bank_branch or record.bank_branch
    record.bank_account_number = (
        emp.bank_account_number or record.bank_account_number
    )
    record.department = emp.department or record.department
    record.job_title = emp.job_title or record.job_title
    record.tax_relief_monthly = emp.tax_relief_monthly
    # Explicit classification, seeded from the workbook's structure as a default
    # (editable later without a re-seed). Compute reads this, never re-infers it.
    record.pay_type = "hourly" if emp.is_hourly else "salaried"
    record.normalise_staff_id()
    db.session.flush()  # assign record.id for the FK below

    if emp.icu_member:
        result.icu_members += 1

    # Replace this employee's raw-element rate table wholesale: drop the old
    # raw rows, then insert the new set. Guarantees idempotency and reflects
    # rate changes exactly, with no chance of a duplicate key.
    stale = WageRateProfile.query.filter(
        WageRateProfile.client_company_id == client_company_id,
        WageRateProfile.employee_id == record.id,
        WageRateProfile.pay_code.in_(RAW_PAY_CODES),
    ).all()
    for row in stale:
        db.session.delete(row)
    if stale:
        db.session.flush()

    for spec in emp.rates:
        db.session.add(
            WageRateProfile(
                client_company_id=client_company_id,
                employee_id=record.id,
                pay_code=spec.pay_code,
                hourly_rate=spec.hourly_rate,
                category=spec.category,
                description=spec.description,
            )
        )
        result.rate_profiles_written += 1


def write_payroll_items(run, payslips):
    """Persist computed payslips to PayrollItem for ``run`` (reused store, so
    distribution/payslip/export code is unchanged). Idempotent: replaces the
    run's existing items and recomputes the run aggregate totals. Runs in a
    single transaction.

    ``payslips``: ``{staff_id: Payslip}``.
    """
    try:
        roster = {
            normalise_emp_id(e.staff_id): e
            for e in Employee.query.filter_by(
                client_company_id=run.client_company_id
            ).all()
        }

        PayrollItem.query.filter_by(payroll_run_id=run.id).delete()
        db.session.flush()

        totals = dict(gross=0.0, net=0.0, paye=0.0, ssnit=0.0, ssnit_er=0.0, ded=0.0)
        count = 0
        for staff_id, slip in payslips.items():
            employee = roster.get(normalise_emp_id(staff_id))
            item = PayrollItem(
                payroll_run_id=run.id,
                employee_id=employee.id if employee else None,
                staff_id=staff_id,
                full_name=employee.full_name if employee else staff_id,
                payroll_month=f"{run.month} {run.year}",
                ssnit_number=employee.ssnit_number if employee else None,
                ghana_card_number=employee.ghana_card_number if employee else None,
                bank_name=employee.bank_name if employee else None,
                bank_branch=employee.bank_branch if employee else None,
                bank_account_number=(
                    employee.bank_account_number if employee else None
                ),
                validation_status="OK",
                **slip.as_payroll_item_fields(),
            )
            db.session.add(item)
            count += 1
            totals["gross"] += slip.gross_pay
            totals["net"] += slip.net_pay
            totals["paye"] += slip.total_tax
            totals["ssnit"] += slip.employee_ssnit
            totals["ssnit_er"] += slip.employer_ssnit
            totals["ded"] += slip.total_deductions

        run.total_workers = count
        run.total_gross_pay = money(totals["gross"])
        run.total_net_pay = money(totals["net"])
        run.total_paye = money(totals["paye"])
        run.total_ssnit = money(totals["ssnit"])
        run.total_ssnit_employer = money(totals["ssnit_er"])
        run.total_deductions = money(totals["ded"])

        from app.audit import record_audit

        record_audit(
            "Raw payroll computed",
            run,
            f"{count} workers for {run.month} {run.year}: gross "
            f"{run.total_gross_pay:.2f}, PAYE {run.total_paye:.2f}, net "
            f"{run.total_net_pay:.2f}.",
        )
        db.session.commit()
        return count
    except Exception:
        db.session.rollback()
        raise


def _preserve_workbook(source_path, context) -> str:
    """Preserve the original workbook bytes for audit. Non-fatal: a preservation
    failure never aborts a valid seed.

    TODO(supabase): upload to the Supabase storage bucket — Render's disk is
    ephemeral, so the local copy below does not survive a restart. Staging to
    the import-session folder keeps the bytes for the life of the instance and
    is a visible placeholder until the bucket is wired.
    """
    try:
        from flask import current_app

        folder = current_app.config.get("IMPORT_SESSION_FOLDER")
        if not folder or not os.path.exists(source_path):
            return ""
        os.makedirs(folder, exist_ok=True)
        base = os.path.basename(source_path)
        dest = os.path.join(
            folder, f"raw_seed_{context.client_company_id}_{base}"
        )
        shutil.copyfile(source_path, dest)
        return dest
    except Exception:  # preservation is best-effort, never blocks a seed
        return ""
