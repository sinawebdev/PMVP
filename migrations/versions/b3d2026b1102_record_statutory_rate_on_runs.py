"""Record which statutory rate version each payroll run was calculated with.

Revision ID: b3d2026b1102
Revises: a1c2026b1101
Create Date: 2026-11-02

Runs calculated before this column existed keep NULL: the version they used
can be inferred from their period, but it was not recorded, and this migration
does not pretend otherwise.
"""

from alembic import op
import sqlalchemy as sa

revision = "b3d2026b1102"
down_revision = "a1c2026b1101"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("payroll_run") as batch:
        batch.add_column(sa.Column("statutory_rate_id", sa.Integer(), nullable=True))
        batch.create_foreign_key("fk_payroll_run_statutory_rate", "statutory_rates",
                                 ["statutory_rate_id"], ["id"])


def downgrade():
    with op.batch_alter_table("payroll_run") as batch:
        batch.drop_constraint("fk_payroll_run_statutory_rate", type_="foreignkey")
        batch.drop_column("statutory_rate_id")
