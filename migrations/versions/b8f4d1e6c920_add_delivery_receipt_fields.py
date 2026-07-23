"""add delivery-receipt fields to payslip_delivery

PMVP v1 Phase 4, Slice 4 — WhatsApp/SMS delivery receipts. Store the provider's
message id (to correlate an async callback), the provider-reported delivery
status, and the delivered timestamp. Additive; existing rows keep NULL.

Revision ID: b8f4d1e6c920
Revises: a7e3c9f21b48
Create Date: 2026-07-23 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'b8f4d1e6c920'
down_revision = 'a7e3c9f21b48'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('payslip_delivery', schema=None) as batch_op:
        batch_op.add_column(sa.Column('provider_message_id', sa.String(length=128), nullable=True))
        batch_op.add_column(sa.Column('provider_status', sa.String(length=32), nullable=True))
        batch_op.add_column(sa.Column('delivered_at', sa.DateTime(), nullable=True))
        batch_op.create_index(
            'ix_payslip_delivery_provider_message_id', ['provider_message_id'], unique=False
        )


def downgrade():
    with op.batch_alter_table('payslip_delivery', schema=None) as batch_op:
        batch_op.drop_index('ix_payslip_delivery_provider_message_id')
        batch_op.drop_column('delivered_at')
        batch_op.drop_column('provider_status')
        batch_op.drop_column('provider_message_id')
