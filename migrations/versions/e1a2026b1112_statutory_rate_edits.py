"""Record who last edited a statutory rate version, and when.

Revision ID: e1a2026b1112
Revises: d7a2026b1109
Create Date: 2026-11-12

Rate versions become editable in place while no approved or processed run
depends on them. The audit trail keeps the before and after values; these two
columns let the rates page say "edited 12 Nov by …" without a join through it.
"""

from alembic import op
import sqlalchemy as sa

revision = "e1a2026b1112"
down_revision = "d7a2026b1109"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("statutory_rates") as batch:
        batch.add_column(sa.Column("updated_at", sa.DateTime(), nullable=True))
        batch.add_column(sa.Column("updated_by", sa.Integer(), nullable=True))
        batch.create_foreign_key("fk_statutory_rates_updated_by", "user",
                                 ["updated_by"], ["id"])


def downgrade():
    with op.batch_alter_table("statutory_rates") as batch:
        batch.drop_constraint("fk_statutory_rates_updated_by", type_="foreignkey")
        batch.drop_column("updated_by")
        batch.drop_column("updated_at")
