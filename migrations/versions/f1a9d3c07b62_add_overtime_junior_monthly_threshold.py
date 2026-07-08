"""add overtime_junior_monthly_threshold to statutory_rates

Revision ID: f1a9d3c07b62
Revises: c4d8e1a09f57
Create Date: 2026-07-08

The StatutoryRate model gained `overtime_junior_monthly_threshold` (the GHS 1,500
junior-staff overtime gate) in Phase 4, but no migration was generated for it, so
`flask db upgrade` could not create the column and the app crashed on boot querying
a column that didn't exist.

This migration is written idempotently (inspects the table first) so it is a safe
no-op on any database where the column already exists — including prod, where it was
added by hand to break the boot deadlock — while still creating it correctly on fresh
and local databases. Dialect-agnostic (works on Postgres and SQLite).
"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = 'f1a9d3c07b62'
down_revision = 'c4d8e1a09f57'
branch_labels = None
depends_on = None

TABLE = 'statutory_rates'
COLUMN = 'overtime_junior_monthly_threshold'


def _has_column(bind):
    return COLUMN in {c['name'] for c in sa.inspect(bind).get_columns(TABLE)}


def upgrade():
    bind = op.get_bind()
    if not _has_column(bind):
        op.add_column(TABLE, sa.Column(COLUMN, sa.Float(),
                                       server_default='1500', nullable=True))


def downgrade():
    bind = op.get_bind()
    if _has_column(bind):
        op.drop_column(TABLE, COLUMN)
