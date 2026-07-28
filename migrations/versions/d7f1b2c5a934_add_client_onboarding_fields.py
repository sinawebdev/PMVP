"""add onboarding fields to client_company

Operations & Onboarding sprint, Phase 1 — the Payrolla admin now onboards a
client company with a short company code, a postal address, and free-text
onboarding notes. All three are additive and nullable, so every existing
company row stays valid; ``company_code`` carries a unique index because it is
quoted as an identifier (exports, support), and NULLs do not collide under that
constraint in either SQLite or PostgreSQL.

Revision ID: d7f1b2c5a934
Revises: b1c3e5f70a29
Create Date: 2026-07-28 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'd7f1b2c5a934'
down_revision = 'b1c3e5f70a29'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('client_company', schema=None) as batch_op:
        batch_op.add_column(sa.Column('company_code', sa.String(length=20), nullable=True))
        batch_op.add_column(sa.Column('address', sa.String(length=255), nullable=True))
        batch_op.add_column(sa.Column('notes', sa.Text(), nullable=True))
        batch_op.create_index(
            'ix_client_company_company_code', ['company_code'], unique=True
        )


def downgrade():
    with op.batch_alter_table('client_company', schema=None) as batch_op:
        batch_op.drop_index('ix_client_company_company_code')
        batch_op.drop_column('notes')
        batch_op.drop_column('address')
        batch_op.drop_column('company_code')
