"""Deleting a client company and everything that hangs off it.

Shared by ``flask demo-reset`` (app.demo_data) and the staff-only demo workspace
(app.demo_workspace). Both stage their deletes in one transaction and commit
once, so a failure rolls back to exactly where it started.

The order matters on Postgres, which enforces every foreign key (the SQLite test
database does not): grandchildren (payslip links, deliveries, raw entries) go
before children (payroll items, vouchers), children before parents (runs,
employees), and parents before the company itself. Rows that merely *reference*
a departing user (an approved run, a recorded expense elsewhere) are not deleted:
their user column is re-pointed at a retained login, so history keeps a human
attribution instead of decaying to "System".
"""

from flask import current_app
from sqlalchemy import or_

from app import db
from app.distribution.links import PayslipLink
from app.models import (
    AccessRequest,
    AuditTrail,
    DesktopLicence,
    DistributionBatch,
    DomainEvent,
    Employee,
    EmployeeDeployment,
    EmployeeTaxReliefApproval,
    Expense,
    ExpenseReceipt,
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


def _ids(query):
    return [row[0] for row in query.all()]


def _bulk_delete(model, condition):
    """Delete rows matching ``condition`` without loading them.

    ``synchronize_session=False`` because the callers discard the session's
    identity map straight afterwards (``db.session.expire_all()``); loading
    every row just to evict it would make a purge of a real dataset needlessly
    slow.
    """
    return model.query.filter(condition).delete(synchronize_session=False)


def release_user_references(user_ids, replacement_user_id=None):
    """Re-point every column that references a departing user.

    Rows owned by OTHER records (an approved payroll run, a recorded expense, a
    statutory rate version, a reviewed access request) are never deleted just
    because the user who touched them is going away; that would destroy real
    history. Each such column is set to ``replacement_user_id`` (normally a
    retained staff login) so the record keeps a human attribution, or NULL when
    there is no replacement.

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
        (ExpenseReceipt, ("uploaded_by",)),
        (ImportBatch, ("uploaded_by",)),
        (Proposal, ("drafted_by",)),
        (DistributionBatch, ("initiated_by_user_id",)),
        (StatutoryRate, ("created_by", "updated_by")),
        (DomainEvent, ("actor_user_id",)),
        (AuditTrail, ("user_id",)),
        (AccessRequest, ("reviewed_by_id",)),
        (DesktopLicence, ("issued_by_id",)),
    ]
    for model, names in columns:
        for name in names:
            column = getattr(model, name)
            model.query.filter(column.in_(user_ids)).update(
                {column: replacement_user_id}, synchronize_session=False
            )
    _bulk_delete(Notification, Notification.user_id.in_(user_ids))


def purge_client_company(company, replacement_user_id=None, drop_company=True, keep_users=False):
    """Delete everything belonging to ``company``, child-first.

    With ``drop_company=False`` the company row itself survives; that is how a
    retained demo tenant is emptied before being rebuilt. ``keep_users=True``
    also keeps the company's logins (their ids and passwords), while still
    removing their notifications, events and audit rows; it needs
    ``drop_company=False``, since a login cannot outlive its company.

    Staged only; the caller owns the commit. Returns the storage keys of the
    receipt files whose rows were deleted. Pass them to
    :func:`delete_stored_files` AFTER the commit, so a rollback never leaves a
    receipt row pointing at a file that is already gone.
    """
    if keep_users and drop_company:
        raise ValueError("keep_users needs drop_company=False: a login cannot outlive its company")
    cid = company.id
    run_ids = _ids(db.session.query(PayrollRun.id).filter_by(client_company_id=cid))
    employee_ids = _ids(db.session.query(Employee.id).filter_by(client_company_id=cid))
    user_ids = _ids(db.session.query(User.id).filter_by(client_company_id=cid))

    # 1. Grandchildren of a run: links point at items and deliveries, and
    #    deliveries point at items and batches, so they go before either.
    if run_ids:
        item_ids = _ids(db.session.query(PayrollItem.id).filter(PayrollItem.payroll_run_id.in_(run_ids)))
        if item_ids:
            # Postgres cascades these from the items; SQLite does not, so say it.
            _bulk_delete(PayslipLink, PayslipLink.payroll_item_id.in_(item_ids))
        _bulk_delete(PayslipDelivery, PayslipDelivery.payroll_run_id.in_(run_ids))
        _bulk_delete(RawUploadArchive, RawUploadArchive.payroll_run_id.in_(run_ids))
        _bulk_delete(RawPayEntry, RawPayEntry.payroll_run_id.in_(run_ids))
        _bulk_delete(Remittance, Remittance.payroll_run_id.in_(run_ids))
        _bulk_delete(PaymentVoucher, PaymentVoucher.payroll_run_id.in_(run_ids))
        _bulk_delete(PayrollItem, PayrollItem.payroll_run_id.in_(run_ids))

    # 2. Records attached to the tenant or to one of its runs. A receipt hangs
    #    off its expense, so it goes first; its file goes after the commit.
    any_run = run_ids or [-1]
    expense_ids = _ids(
        db.session.query(Expense.id).filter(
            or_(Expense.client_company_id == cid, Expense.payroll_run_id.in_(any_run))
        )
    )
    receipt_filter = or_(
        ExpenseReceipt.client_company_id == cid,
        ExpenseReceipt.expense_id.in_(expense_ids or [-1]),
    )
    stored_keys = [
        row[0] for row in db.session.query(ExpenseReceipt.storage_key).filter(receipt_filter).all()
    ]
    _bulk_delete(ExpenseReceipt, receipt_filter)
    _bulk_delete(
        DistributionBatch,
        or_(DistributionBatch.client_company_id == cid, DistributionBatch.payroll_run_id.in_(any_run)),
    )
    if expense_ids:
        _bulk_delete(Expense, Expense.id.in_(expense_ids))
    _bulk_delete(
        ImportBatch,
        or_(ImportBatch.client_company_id == cid, ImportBatch.payroll_run_id.in_(any_run)),
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
    any_employee = employee_ids or [-1]
    _bulk_delete(EmployeeTaxReliefApproval, EmployeeTaxReliefApproval.employee_id.in_(any_employee))
    _bulk_delete(
        WageRateProfile,
        or_(WageRateProfile.client_company_id == cid, WageRateProfile.employee_id.in_(any_employee)),
    )
    _bulk_delete(
        EmployeeDeployment,
        or_(EmployeeDeployment.client_company_id == cid, EmployeeDeployment.employee_id.in_(any_employee)),
    )
    _bulk_delete(Proposal, Proposal.client_company_id == cid)
    _bulk_delete(PayrollRun, PayrollRun.client_company_id == cid)
    _bulk_delete(Employee, Employee.client_company_id == cid)

    # 5. The tenant's own users. Their audit entries go with them: an audit row
    #    for a purged tenant has nothing left to explain.
    if user_ids:
        _bulk_delete(AuditTrail, AuditTrail.user_id.in_(user_ids))
        if not keep_users:
            release_user_references(user_ids, replacement_user_id)
            _bulk_delete(User, User.id.in_(user_ids))

    if drop_company:
        # An access request that created the company keeps its history, not its link.
        AccessRequest.query.filter(AccessRequest.client_company_id == cid).update(
            {AccessRequest.client_company_id: None}, synchronize_session=False
        )
        db.session.delete(company)
    db.session.flush()
    return stored_keys


def delete_stored_files(keys):
    """Best-effort removal of receipt files after the purge has committed.

    A file that will not delete is logged and left: an orphaned file in the
    storage root is harmless, a failed purge because of one is not."""
    if not keys:
        return
    from app.storage import StorageError, get_storage

    storage = get_storage()
    for key in keys:
        try:
            storage.delete(key)
        except StorageError:
            current_app.logger.warning("[purge] could not delete stored file %s", key)
