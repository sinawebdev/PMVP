"""add worker-reclaim fields to distribution_batch

PMVP v1 Phase 5 — stuck-batch recovery. A batch left in `running` because its
worker died mid-send is requeued by the worker's reclaim sweep; these columns
attribute the claim (claimed_by_worker) and bound how many times a batch may be
requeued before it is failed as a poison batch (reclaim_count). Both additive:
existing rows get NULL / 0 and behave exactly as before.

Revision ID: a9f2c4e60d18
Revises: c9a1e7b4f832
Create Date: 2026-07-23 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'a9f2c4e60d18'
down_revision = 'c9a1e7b4f832'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('distribution_batch', schema=None) as batch_op:
        batch_op.add_column(sa.Column('claimed_by_worker', sa.String(length=120), nullable=True))
        batch_op.add_column(
            sa.Column('reclaim_count', sa.Integer(), nullable=False, server_default='0')
        )


def downgrade():
    with op.batch_alter_table('distribution_batch', schema=None) as batch_op:
        batch_op.drop_column('reclaim_count')
        batch_op.drop_column('claimed_by_worker')
