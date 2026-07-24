"""add tenant branding pack to client_company

Payrolla Phase 4, Slice 5 — per-tenant branding for payslip emails: brand name,
accent colour, email sender name, and reply-to. Each NULL falls back to the
global config, so unset tenants are unchanged. Additive.

Revision ID: c9a1e7b4f832
Revises: b8f4d1e6c920
Create Date: 2026-07-23 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'c9a1e7b4f832'
down_revision = 'b8f4d1e6c920'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('client_company', schema=None) as batch_op:
        batch_op.add_column(sa.Column('brand_name', sa.String(length=120), nullable=True))
        batch_op.add_column(sa.Column('brand_color', sa.String(length=16), nullable=True))
        batch_op.add_column(sa.Column('email_from_name', sa.String(length=120), nullable=True))
        batch_op.add_column(sa.Column('email_reply_to', sa.String(length=160), nullable=True))


def downgrade():
    with op.batch_alter_table('client_company', schema=None) as batch_op:
        batch_op.drop_column('email_reply_to')
        batch_op.drop_column('email_from_name')
        batch_op.drop_column('brand_color')
        batch_op.drop_column('brand_name')
