"""add distribution_batch_id to payslip_delivery

Payrolla Phase 3, Slice 6 — link each delivery to the batch that last (re)sent it
so distribution history can attribute a delivery to the initiating operator and
filter by batch. Nullable/additive; existing rows keep NULL.

Revision ID: e3c1a7b9d5f6
Revises: d2b8f6a3c1e4
Create Date: 2026-07-23 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'e3c1a7b9d5f6'
down_revision = 'd2b8f6a3c1e4'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('payslip_delivery', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column('distribution_batch_id', sa.Integer(), nullable=True)
        )
        batch_op.create_index(
            'ix_payslip_delivery_distribution_batch_id',
            ['distribution_batch_id'],
            unique=False,
        )
        batch_op.create_foreign_key(
            'fk_payslip_delivery_distribution_batch',
            'distribution_batch',
            ['distribution_batch_id'],
            ['id'],
        )


def downgrade():
    # Dropping the column removes its foreign key too — on PostgreSQL the column's
    # own FK constraint is dropped with the column, and SQLite batch mode recreates
    # the table without it. This avoids depending on the FK's constraint name
    # (which differs between a migration-built and a create_all()-built schema).
    with op.batch_alter_table('payslip_delivery', schema=None) as batch_op:
        batch_op.drop_index('ix_payslip_delivery_distribution_batch_id')
        batch_op.drop_column('distribution_batch_id')
