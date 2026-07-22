"""add scheduled_for to distribution_batch

PMVP v1 Phase 3, Slice 7 — scheduled distribution. A batch with scheduled_for
set sits in the new `scheduled` status until the worker activates it at/after
that time. Additive column; existing rows keep NULL (run-as-soon-as-possible).

Revision ID: f4d2b8c6a1e7
Revises: e3c1a7b9d5f6
Create Date: 2026-07-23 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'f4d2b8c6a1e7'
down_revision = 'e3c1a7b9d5f6'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('distribution_batch', schema=None) as batch_op:
        batch_op.add_column(sa.Column('scheduled_for', sa.DateTime(), nullable=True))
        batch_op.create_index(
            'ix_distribution_batch_scheduled_for', ['scheduled_for'], unique=False
        )


def downgrade():
    with op.batch_alter_table('distribution_batch', schema=None) as batch_op:
        batch_op.drop_index('ix_distribution_batch_scheduled_for')
        batch_op.drop_column('scheduled_for')
