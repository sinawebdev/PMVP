"""Remember which company a payroll bureau grew out of.

Revision ID: b5c2026b1114
Revises: a4b2026b1112
Create Date: 2026-11-14

"Set up as payroll bureau" moves a company's logins to the new bureau, so the
company itself is left with none and was flagged "awaiting credentials" for
good. client_company.bureau_home marks that company; its bureau's staff count
as its logins. Existing conversions are backfilled: the converted company is
the one whose bureau carries its name, which is how the conversion names it.
"""

from alembic import op
import sqlalchemy as sa

revision = "b5c2026b1114"
down_revision = "a4b2026b1112"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("client_company") as batch:
        batch.add_column(
            sa.Column("bureau_home", sa.Boolean(), nullable=False, server_default=sa.false())
        )
    op.get_bind().execute(
        sa.text(
            "UPDATE client_company SET bureau_home = :yes "
            "WHERE bureau_id IS NOT NULL AND EXISTS ("
            " SELECT 1 FROM bureau WHERE bureau.id = client_company.bureau_id"
            " AND lower(bureau.name) = lower(client_company.name))"
        ),
        {"yes": True},
    )


def downgrade():
    with op.batch_alter_table("client_company") as batch:
        batch.drop_column("bureau_home")
