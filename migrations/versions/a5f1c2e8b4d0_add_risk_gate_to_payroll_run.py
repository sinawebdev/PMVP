"""add risk gate fields to payroll_run

Payrolla Phase 5 — persists the risk-gate verdict from app/risk.py on each run:
risk_status ('held' | 'accepted' | NULL), the human-readable risk_reasons, and
the risk_checked_at timestamp. Additive and nullable, so existing rows are
unaffected until a run is scored.

Revision ID: a5f1c2e8b4d0
Revises: f4c2a1b9d7e3
Create Date: 2026-07-16 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'a5f1c2e8b4d0'
down_revision = 'f4c2a1b9d7e3'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('payroll_run', schema=None) as batch_op:
        batch_op.add_column(sa.Column('risk_status', sa.String(length=16), nullable=True))
        batch_op.add_column(sa.Column('risk_reasons', sa.Text(), nullable=True))
        batch_op.add_column(sa.Column('risk_checked_at', sa.DateTime(), nullable=True))


def downgrade():
    with op.batch_alter_table('payroll_run', schema=None) as batch_op:
        batch_op.drop_column('risk_checked_at')
        batch_op.drop_column('risk_reasons')
        batch_op.drop_column('risk_status')
