"""add icu_member_rate to statutory_rates

Revision ID: b8d3a6f29c14
Revises: a7c2f4e18b90
Create Date: 2026-07-10

The raw-hours engine deducts union (ICU) dues as a fraction of basic wage for
seeded members. That fraction is a statutory-style rate and must be config, not
a Python constant, so it lives on StatutoryRate (effective-dated) as
``icu_member_rate`` (default 0.03 = 3%, verified against the DZ Jan-2026 sheet).

Idempotent (inspects the table first) and dialect-agnostic (Postgres + SQLite).
"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = 'b8d3a6f29c14'
down_revision = 'a7c2f4e18b90'
branch_labels = None
depends_on = None

TABLE = 'statutory_rates'
COLUMN = 'icu_member_rate'


def _has_column(bind):
    return COLUMN in {c['name'] for c in sa.inspect(bind).get_columns(TABLE)}


def upgrade():
    bind = op.get_bind()
    if not _has_column(bind):
        op.add_column(
            TABLE,
            sa.Column(COLUMN, sa.Float(), server_default='0.03', nullable=False),
        )


def downgrade():
    bind = op.get_bind()
    if _has_column(bind):
        op.drop_column(TABLE, COLUMN)
