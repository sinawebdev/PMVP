"""add Phase 5 query-backed indexes

PMVP v1 Phase 5 — indexes backed by observed query patterns, all additive:
  * payroll_item.payroll_run_id — the largest table's run filter was unindexed
    (payslip render, distribution, exports, item edit all filter by run).
  * payslip_delivery(status, next_retry_at) — the retry sweep scans this predicate
    on every worker poll.
  * payslip_delivery(payroll_item_id, channel) — _latest_delivery lookup per item.
  * distribution_batch(status, created_at) — claim_next_batch filters+orders here
    on every poll.

Index-only; no data or column changes.

Revision ID: b1c3e5f70a29
Revises: a9f2c4e60d18
Create Date: 2026-07-23 00:00:00.000000

"""
from alembic import op


# revision identifiers, used by Alembic.
revision = 'b1c3e5f70a29'
down_revision = 'a9f2c4e60d18'
branch_labels = None
depends_on = None


def upgrade():
    op.create_index(
        'ix_payroll_item_payroll_run_id', 'payroll_item', ['payroll_run_id'], unique=False
    )
    op.create_index(
        'ix_payslip_delivery_status_next_retry', 'payslip_delivery',
        ['status', 'next_retry_at'], unique=False,
    )
    op.create_index(
        'ix_payslip_delivery_item_channel', 'payslip_delivery',
        ['payroll_item_id', 'channel'], unique=False,
    )
    op.create_index(
        'ix_distribution_batch_status_created', 'distribution_batch',
        ['status', 'created_at'], unique=False,
    )


def downgrade():
    op.drop_index('ix_distribution_batch_status_created', table_name='distribution_batch')
    op.drop_index('ix_payslip_delivery_item_channel', table_name='payslip_delivery')
    op.drop_index('ix_payslip_delivery_status_next_retry', table_name='payslip_delivery')
    op.drop_index('ix_payroll_item_payroll_run_id', table_name='payroll_item')
