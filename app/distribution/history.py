"""Searchable delivery history (Phase 3, Slice 6).

A delivery-level investigative view over every PayslipDelivery, joined to its
run, company, payroll item (employee) and initiating batch/operator. One
function builds the filtered, paginated query; another supplies the dropdown
option lists. Operator-plane, cross-tenant, read-only.
"""
from datetime import datetime, time, timezone

from sqlalchemy import or_
from sqlalchemy.orm import joinedload

from app import db
from app.models import (
    DELIVERY_CANCELLED,
    DELIVERY_FAILED,
    DELIVERY_PENDING,
    DELIVERY_SENT,
    DELIVERY_CHANNELS,
    ClientCompany,
    DistributionBatch,
    PayrollItem,
    PayrollRun,
    PayslipDelivery,
    User,
)

DELIVERY_STATUSES = (DELIVERY_SENT, DELIVERY_FAILED, DELIVERY_CANCELLED, DELIVERY_PENDING)
PER_PAGE = 25


def _parse_date(value, *, end=False):
    """'YYYY-MM-DD' -> aware UTC datetime at day start (or end). None on blank/bad."""
    if not value:
        return None
    try:
        d = datetime.strptime(value.strip(), "%Y-%m-%d").date()
    except (ValueError, AttributeError):
        return None
    moment = time.max if end else time.min
    return datetime.combine(d, moment, tzinfo=timezone.utc)


def filtered_delivery_query(filters):
    """A PayslipDelivery query (joined to item + run) with every `filters` clause
    applied, but no ordering, eager-loading, or pagination. Shared by the history
    list, analytics, and exports so they always agree on what a filter means."""
    query = PayslipDelivery.query.join(
        PayrollItem, PayslipDelivery.payroll_item_id == PayrollItem.id
    ).join(PayrollRun, PayslipDelivery.payroll_run_id == PayrollRun.id)

    company_id = filters.get("company_id")
    if company_id:
        query = query.filter(PayrollRun.client_company_id == company_id)

    run_id = filters.get("run_id")
    if run_id:
        query = query.filter(PayslipDelivery.payroll_run_id == run_id)

    status = filters.get("status")
    if status in DELIVERY_STATUSES:
        query = query.filter(PayslipDelivery.status == status)

    channel = filters.get("channel")
    if channel in DELIVERY_CHANNELS:
        query = query.filter(PayslipDelivery.channel == channel)

    operator_id = filters.get("operator_id")
    if operator_id:
        query = query.join(
            DistributionBatch,
            PayslipDelivery.distribution_batch_id == DistributionBatch.id,
        ).filter(DistributionBatch.initiated_by_user_id == operator_id)

    text = (filters.get("q") or "").strip()
    if text:
        like = f"%{text}%"
        query = query.filter(
            or_(
                PayrollItem.staff_id.ilike(like),
                PayrollItem.full_name.ilike(like),
                PayslipDelivery.recipient.ilike(like),
            )
        )

    date_from = _parse_date(filters.get("date_from"))
    if date_from:
        query = query.filter(PayslipDelivery.updated_at >= date_from)
    date_to = _parse_date(filters.get("date_to"), end=True)
    if date_to:
        query = query.filter(PayslipDelivery.updated_at <= date_to)

    return query


def search_deliveries(filters, page=1):
    """A Flask-SQLAlchemy Pagination of PayslipDelivery rows matching `filters`,
    newest activity first, with relationships eager-loaded (no N+1)."""
    query = (
        filtered_delivery_query(filters)
        .options(
            joinedload(PayslipDelivery.payroll_item),
            joinedload(PayslipDelivery.payroll_run).joinedload(PayrollRun.client_company),
            joinedload(PayslipDelivery.distribution_batch).joinedload(
                DistributionBatch.initiated_by
            ),
        )
        .order_by(PayslipDelivery.updated_at.desc(), PayslipDelivery.id.desc())
    )
    return query.paginate(page=page, per_page=PER_PAGE, error_out=False)


def export_rows(filters, limit=50000):
    """Filtered deliveries as fully-loaded rows for CSV/XLSX export (capped)."""
    return (
        filtered_delivery_query(filters)
        .options(
            joinedload(PayslipDelivery.payroll_item),
            joinedload(PayslipDelivery.payroll_run).joinedload(PayrollRun.client_company),
            joinedload(PayslipDelivery.distribution_batch).joinedload(
                DistributionBatch.initiated_by
            ),
        )
        .order_by(PayslipDelivery.updated_at.desc(), PayslipDelivery.id.desc())
        .limit(limit)
        .all()
    )


def filter_options():
    """Dropdown option lists for the history filter form."""
    companies = (
        ClientCompany.query.filter_by(status="Active")
        .order_by(ClientCompany.name)
        .all()
    )
    # Only users who have actually initiated a distribution.
    operator_ids = [
        row[0]
        for row in db.session.query(DistributionBatch.initiated_by_user_id)
        .filter(DistributionBatch.initiated_by_user_id.isnot(None))
        .distinct()
        .all()
    ]
    operators = (
        User.query.filter(User.id.in_(operator_ids)).order_by(User.name).all()
        if operator_ids
        else []
    )
    return {
        "companies": companies,
        "operators": operators,
        "statuses": DELIVERY_STATUSES,
        "channels": DELIVERY_CHANNELS,
    }
