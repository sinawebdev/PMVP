"""add notes to expense

Operations & Onboarding sprint, Phase 2 — client expense management. The
expense form captures free-text notes alongside the one-line description;
additive and nullable, so every existing expense row stays valid.

Revision ID: e5a2c8d13f60
Revises: d7f1b2c5a934
Create Date: 2026-07-28 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'e5a2c8d13f60'
down_revision = 'd7f1b2c5a934'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('expense', schema=None) as batch_op:
        batch_op.add_column(sa.Column('notes', sa.Text(), nullable=True))


def downgrade():
    with op.batch_alter_table('expense', schema=None) as batch_op:
        batch_op.drop_column('notes')
