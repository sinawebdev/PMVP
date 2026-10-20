"""Dated deduction eligibility, approved relief history and payroll snapshots.

Revision ID: 9d2026c1020
Revises: 8b62026a0901
Create Date: 2026-10-20

No employee is declared junior or approved for a tax concession by migration.
Existing payroll amounts stay untouched. The September-and-later rate versions
use the corrected deduction policy on subsequent calculations; earlier rate
versions keep the explicit legacy policy for historical reproducibility.
This is an implementation activation boundary, not a new legal commencement
date for the underlying overtime/PF/relief rules.
"""

from datetime import date
from alembic import op
import sqlalchemy as sa

revision = "9d2026c1020"
down_revision = "8b62026a0901"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("employee") as batch:
        batch.add_column(sa.Column("overtime_is_junior", sa.Boolean(), nullable=True))
        batch.add_column(sa.Column("overtime_qualifying_annual_income", sa.Float(), nullable=True))
        batch.add_column(sa.Column("overtime_income_year", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("pf_scheme_approved", sa.Boolean(), nullable=False, server_default=sa.false()))
        batch.add_column(sa.Column("pf_scheme_reference", sa.String(160), nullable=True))
        batch.add_column(sa.Column("tax_relief_approval_ref", sa.String(160), nullable=True))
        batch.add_column(sa.Column("tax_relief_category", sa.String(80), nullable=True))
        batch.add_column(sa.Column("tax_relief_valid_from", sa.Date(), nullable=True))
        batch.add_column(sa.Column("tax_relief_valid_to", sa.Date(), nullable=True))

    op.create_table(
        "employee_tax_relief_approval",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("employee_id", sa.Integer(), sa.ForeignKey("employee.id"), nullable=False),
        sa.Column("monthly_amount", sa.Float(), nullable=False),
        sa.Column("reference", sa.String(160), nullable=False),
        sa.Column("category", sa.String(80), nullable=False),
        sa.Column("valid_from", sa.Date(), nullable=False),
        sa.Column("valid_to", sa.Date(), nullable=False),
        sa.Column("recorded_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_employee_tax_relief_approval_employee_id", "employee_tax_relief_approval", ["employee_id"])
    with op.batch_alter_table("payroll_item") as batch:
        for name in (
            "pf_fund_employer", "pf_allowable_employee", "pf_employer_taxable",
            "tax_relief_applied", "overtime_ordinary_pay", "pensionable_salary",
            "ssnit_remittance", "tier_2_remittance",
        ):
            batch.add_column(sa.Column(name, sa.Float(), nullable=True, server_default="0"))
        batch.add_column(sa.Column("deductions_policy_version", sa.Integer(), nullable=False, server_default="0"))

    with op.batch_alter_table("statutory_rates") as batch:
        batch.add_column(sa.Column("deductions_policy_version", sa.Integer(), nullable=False, server_default="0"))
        batch.add_column(sa.Column("ssnit_min_insurable", sa.Float(), nullable=False, server_default="587.80"))
        batch.add_column(sa.Column("ssnit_max_insurable", sa.Float(), nullable=False, server_default="69000"))
        batch.add_column(sa.Column("ssnit_limits_year", sa.Integer(), nullable=False, server_default="2026"))
        batch.add_column(sa.Column("ssnit_min_monthly_remittance", sa.Float(), nullable=False, server_default="79.40"))
        batch.add_column(sa.Column("provident_fund_tax_limit", sa.Float(), nullable=False, server_default="0.165"))
        batch.add_column(sa.Column("overtime_qualifying_annual_limit", sa.Float(), nullable=False, server_default="18000"))
    rates = sa.table("statutory_rates", sa.column("effective_from", sa.Date()), sa.column("deductions_policy_version", sa.Integer()))
    op.get_bind().execute(rates.update().where(rates.c.effective_from >= date(2026, 9, 1)).values(deductions_policy_version=1))


def downgrade():
    # Removing approval evidence/calculation snapshots is not a safe rollback.
    # Restore application code without destroying the financial audit trail.
    raise RuntimeError(
        "This migration contains payroll approval evidence and calculation "
        "snapshots. A destructive schema downgrade requires an audited data "
        "recovery plan; automatic downgrade is refused."
    )
