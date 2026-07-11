"""add Employee.pay_type and raw_upload_archives

Revision ID: d2a4f6108e35
Revises: c9e5b2d478a1
Create Date: 2026-07-10

Phase 7 web integration:
  * Employee.pay_type — explicit 'hourly'/'salaried' classification for the raw
    engine (replaces the inferred-from-rate-rows heuristic at compute time).
  * raw_upload_archives — durable Postgres-blob preservation of the original
    raw-upload workbook bytes (with sha256), written inside the seed-confirm
    transaction so seeded context can never exist without its source workbook.

Idempotent (inspects first) and dialect-agnostic (Postgres + SQLite).
"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = 'd2a4f6108e35'
down_revision = 'c9e5b2d478a1'
branch_labels = None
depends_on = None


def _has_column(bind, table, column):
    return column in {c['name'] for c in sa.inspect(bind).get_columns(table)}


def _has_table(bind, table):
    return table in sa.inspect(bind).get_table_names()


def upgrade():
    bind = op.get_bind()
    if not _has_column(bind, 'employee', 'pay_type'):
        op.add_column('employee', sa.Column('pay_type', sa.String(length=10), nullable=True))
    if not _has_table(bind, 'raw_upload_archives'):
        op.create_table(
            'raw_upload_archives',
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('payroll_run_id', sa.Integer(), nullable=False),
            sa.Column('filename', sa.String(length=255), nullable=True),
            sa.Column('content', sa.LargeBinary(), nullable=False),
            sa.Column('sha256', sa.String(length=64), nullable=False),
            sa.Column('upload_kind', sa.String(length=10), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=True),
            sa.ForeignKeyConstraint(['payroll_run_id'], ['payroll_run.id']),
            sa.PrimaryKeyConstraint('id'),
        )
        op.create_index(
            'ix_raw_upload_archives_payroll_run_id',
            'raw_upload_archives', ['payroll_run_id'], unique=False,
        )


def downgrade():
    bind = op.get_bind()
    if _has_table(bind, 'raw_upload_archives'):
        op.drop_index('ix_raw_upload_archives_payroll_run_id', table_name='raw_upload_archives')
        op.drop_table('raw_upload_archives')
    if _has_column(bind, 'employee', 'pay_type'):
        op.drop_column('employee', 'pay_type')
