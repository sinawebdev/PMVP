"""add icu_dues to payroll_item

Revision ID: c9e5b2d478a1
Revises: b8d3a6f29c14
Create Date: 2026-07-10

Raw-hours union members carry a derived ICU dues figure (3% of basic). It needs
its own PayrollItem column so the union-distribution export and the ICU tie-out
validation have an auditable per-worker amount rather than a figure folded into
other_deductions. Standard / non-member rows keep 0.

Idempotent (inspects the table first) and dialect-agnostic (Postgres + SQLite).
"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = 'c9e5b2d478a1'
down_revision = 'b8d3a6f29c14'
branch_labels = None
depends_on = None

TABLE = 'payroll_item'
COLUMN = 'icu_dues'


def _has_column(bind):
    return COLUMN in {c['name'] for c in sa.inspect(bind).get_columns(TABLE)}


def upgrade():
    bind = op.get_bind()
    if not _has_column(bind):
        op.add_column(TABLE, sa.Column(COLUMN, sa.Float(), server_default='0', nullable=True))


def downgrade():
    bind = op.get_bind()
    if _has_column(bind):
        op.drop_column(TABLE, COLUMN)
