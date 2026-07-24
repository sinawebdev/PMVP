"""add job_title to employee

Persists the JOB TITLE / Position parsed from the rich RAW-DATA seed workbook,
which previously had nowhere to land on Employee.

Revision ID: f4c2a1b9d7e3
Revises: d2a4f6108e35
Create Date: 2026-07-15 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'f4c2a1b9d7e3'
down_revision = 'd2a4f6108e35'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('employee', schema=None) as batch_op:
        batch_op.add_column(sa.Column('job_title', sa.String(length=120), nullable=True))


def downgrade():
    with op.batch_alter_table('employee', schema=None) as batch_op:
        batch_op.drop_column('job_title')
