"""add dedicated meal/welfare/IOU columns, overtime source, junior OT threshold

Dedicated columns for the ACS RAW DATA inputs that previously folded silently
into other_allowances / other_deductions (spec §3: don't fold — it destroys
the audit trail the sheet keeps): L MEALS, AC WELFARE, AE IOU. Plus the
overtime_source marker (§2 hybrid overtime model) and the GRA junior-staff
qualifying threshold on statutory_rates (§7.1).

Revision ID: c4d8e1a09f57
Revises: e3b9c7a41f52
Create Date: 2026-07-07 12:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'c4d8e1a09f57'
down_revision = 'e3b9c7a41f52'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('payroll_item', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column('meal_allowance', sa.Float(), nullable=True, server_default='0')
        )
        batch_op.add_column(
            sa.Column('welfare_deduction', sa.Float(), nullable=True, server_default='0')
        )
        batch_op.add_column(
            sa.Column('iou_deduction', sa.Float(), nullable=True, server_default='0')
        )
        batch_op.add_column(
            sa.Column(
                'overtime_source',
                sa.String(length=20),
                nullable=True,
                server_default='manual',
            )
        )

    with op.batch_alter_table('statutory_rates', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                'overtime_junior_monthly_threshold',
                sa.Float(),
                nullable=False,
                server_default='1500',
            )
        )


def downgrade():
    with op.batch_alter_table('statutory_rates', schema=None) as batch_op:
        batch_op.drop_column('overtime_junior_monthly_threshold')

    with op.batch_alter_table('payroll_item', schema=None) as batch_op:
        batch_op.drop_column('overtime_source')
        batch_op.drop_column('iou_deduction')
        batch_op.drop_column('welfare_deduction')
        batch_op.drop_column('meal_allowance')
