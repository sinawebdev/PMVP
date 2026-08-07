"""add payslip_token_version to payroll_item

Gives the no-login payslip link something server-side to check against, so a
token can be revoked before it expires (see app/distribution/tokens.py).

Added nullable, backfilled, then made NOT NULL — the three-step shape used by
the other column migrations here (e.g. 912a1bf13693). `payroll_item` predates
Alembic in this project (the tables came from db.create_all(); the initial
baseline only adds foreign keys), but that only prevents rebuilding the schema
from scratch — ALTER TABLE ... ADD COLUMN against the existing table is
unaffected, and this runs through the same `flask db upgrade` the deploy already
executes before gunicorn starts.

Revision ID: d7b4e2a09c31
Revises: c3f7a2d81e64
Create Date: 2026-08-07 16:20:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'd7b4e2a09c31'
down_revision = 'c3f7a2d81e64'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('payroll_item', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column('payslip_token_version', sa.Integer(), nullable=True)
        )

    # Existing payslips start at 0, matching the model default, so links issued
    # after this deploy verify against a known value rather than NULL.
    op.execute(
        "UPDATE payroll_item SET payslip_token_version = 0 "
        "WHERE payslip_token_version IS NULL"
    )

    # Only safe once every row has a value. NOT NULL is what lets the verifier
    # treat a missing version as a bug rather than a case to tolerate.
    with op.batch_alter_table('payroll_item', schema=None) as batch_op:
        batch_op.alter_column(
            'payslip_token_version',
            existing_type=sa.Integer(),
            nullable=False,
            server_default='0',
        )


def downgrade():
    with op.batch_alter_table('payroll_item', schema=None) as batch_op:
        batch_op.drop_column('payslip_token_version')
