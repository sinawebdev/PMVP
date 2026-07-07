"""add bank_branch to employee and payroll_item

Revision ID: e3b9c7a41f52
Revises: b7e4c1d0a9f2
Create Date: 2026-07-07 11:20:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'e3b9c7a41f52'
down_revision = 'b7e4c1d0a9f2'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('employee', schema=None) as batch_op:
        batch_op.add_column(sa.Column('bank_branch', sa.String(length=120), nullable=True))

    with op.batch_alter_table('payroll_item', schema=None) as batch_op:
        batch_op.add_column(sa.Column('bank_branch', sa.String(length=120), nullable=True))


def downgrade():
    with op.batch_alter_table('payroll_item', schema=None) as batch_op:
        batch_op.drop_column('bank_branch')

    with op.batch_alter_table('employee', schema=None) as batch_op:
        batch_op.drop_column('bank_branch')
