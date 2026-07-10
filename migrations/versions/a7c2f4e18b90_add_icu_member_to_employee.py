"""add icu_member to employee

Revision ID: a7c2f4e18b90
Revises: f1a9d3c07b62
Create Date: 2026-07-10

The raw-hours engine seeds union (ICU) membership from the source workbook's
ICU-dues column (dues > 0 => member) so the derived 3%-of-basic ICU deduction
can be applied without a client ever uploading it. That needs a persistent
per-employee flag: Employee.icu_member.

Written idempotently (inspects the table first) so it is a safe no-op where the
column already exists, and dialect-agnostic (Postgres + SQLite). A non-null
boolean with server_default '0'/false backfills existing rows to non-member,
which is correct: only raw-seeded workers are union members.
"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = 'a7c2f4e18b90'
down_revision = 'f1a9d3c07b62'
branch_labels = None
depends_on = None

TABLE = 'employee'
COLUMN = 'icu_member'


def _has_column(bind):
    return COLUMN in {c['name'] for c in sa.inspect(bind).get_columns(TABLE)}


def upgrade():
    bind = op.get_bind()
    if not _has_column(bind):
        op.add_column(
            TABLE,
            sa.Column(COLUMN, sa.Boolean(), server_default=sa.false(), nullable=False),
        )


def downgrade():
    bind = op.get_bind()
    if _has_column(bind):
        op.drop_column(TABLE, COLUMN)
