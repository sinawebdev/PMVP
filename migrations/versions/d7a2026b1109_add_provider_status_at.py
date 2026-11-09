"""Add payslip_delivery.provider_status_at for SasuSync delivery reports.

Revision ID: d7a2026b1109
Revises: c5e2026b1106
Create Date: 2026-11-09

Reports can arrive out of order, and the later timestamp wins, not the later
request. So the time of the report last applied has to be stored to compare with.
"""

from alembic import op
import sqlalchemy as sa

revision = "d7a2026b1109"
down_revision = "c5e2026b1106"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("payslip_delivery") as batch_op:
        batch_op.add_column(sa.Column("provider_status_at", sa.DateTime(), nullable=True))


def downgrade():
    with op.batch_alter_table("payslip_delivery") as batch_op:
        batch_op.drop_column("provider_status_at")
