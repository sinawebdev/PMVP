"""add next_retry_at to payslip_delivery

PMVP v1 Phase 3, Slice 3 — the retry system. A failed PayslipDelivery whose
next_retry_at is set (and past) is picked up by the worker for an automatic
retry; NULL once the retry limit is exhausted (the "final failure" marker).
Purely additive.

Revision ID: d2b8f6a3c1e4
Revises: c1a9e5f7b3d2
Create Date: 2026-07-22 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'd2b8f6a3c1e4'
down_revision = 'c1a9e5f7b3d2'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('payslip_delivery', schema=None) as batch_op:
        batch_op.add_column(sa.Column('next_retry_at', sa.DateTime(), nullable=True))


def downgrade():
    with op.batch_alter_table('payslip_delivery', schema=None) as batch_op:
        batch_op.drop_column('next_retry_at')
