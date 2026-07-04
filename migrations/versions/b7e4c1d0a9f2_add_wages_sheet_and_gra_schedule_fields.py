"""add wages-sheet and GRA-schedule fields

New persisted columns for the payroll-enhancements spec:
  * payroll_run.total_ssnit_employer — employer SSF total, previously computed
    in memory and discarded.
  * payroll_item — upload-sourced fields (pay_difference, loan_advance,
    end_of_year_bonus) and calculator outputs previously thrown away
    (ssf_employer, overtime_tax, bonus_tax, bonus_excess, taxable_income)
    plus derived figures (net_basic_wage, annual_salary, annual_salary_15pct).
  * employee.tin — GRA TIN, nullable, no capture workflow yet.

All money columns are Float server_default '0' so existing rows read as 0
rather than NULL (they represent runs calculated before these fields existed).

Revision ID: b7e4c1d0a9f2
Revises: 912a1bf13693
Create Date: 2026-07-03

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'b7e4c1d0a9f2'
down_revision = '912a1bf13693'
branch_labels = None
depends_on = None


PAYROLL_ITEM_MONEY_COLUMNS = (
    'end_of_year_bonus',
    'pay_difference',
    'ssf_employer',
    'overtime_tax',
    'bonus_tax',
    'bonus_excess',
    'taxable_income',
    'net_basic_wage',
    'annual_salary',
    'annual_salary_15pct',
    'loan_advance',
)


def upgrade():
    with op.batch_alter_table('payroll_run', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column('total_ssnit_employer', sa.Float(), nullable=True, server_default='0')
        )

    with op.batch_alter_table('payroll_item', schema=None) as batch_op:
        for column in PAYROLL_ITEM_MONEY_COLUMNS:
            batch_op.add_column(
                sa.Column(column, sa.Float(), nullable=True, server_default='0')
            )

    with op.batch_alter_table('employee', schema=None) as batch_op:
        batch_op.add_column(sa.Column('tin', sa.String(length=80), nullable=True))


def downgrade():
    with op.batch_alter_table('employee', schema=None) as batch_op:
        batch_op.drop_column('tin')

    with op.batch_alter_table('payroll_item', schema=None) as batch_op:
        for column in reversed(PAYROLL_ITEM_MONEY_COLUMNS):
            batch_op.drop_column(column)

    with op.batch_alter_table('payroll_run', schema=None) as batch_op:
        batch_op.drop_column('total_ssnit_employer')
