"""Mark the staff-only demo workspace's bureau and companies.

Revision ID: d8f2026b1123
Revises: c6d2026b1120
Create Date: 2026-11-23

The live site needs sample logins so Payrolla staff can see what a bureau and a
client company see, without a shell to seed them (app.demo_workspace). is_demo
marks those rows: a demo company's payslips are never handed to a real SMS or
email provider, and the workspace's Remove deletes only flagged rows. False on
every existing row.
"""

from alembic import op
import sqlalchemy as sa

revision = "d8f2026b1123"
down_revision = "c6d2026b1120"
branch_labels = None
depends_on = None


def upgrade():
    for table in ("bureau", "client_company"):
        with op.batch_alter_table(table) as batch:
            batch.add_column(
                sa.Column("is_demo", sa.Boolean(), nullable=False, server_default=sa.false())
            )


def downgrade():
    for table in ("client_company", "bureau"):
        with op.batch_alter_table(table) as batch:
            batch.drop_column("is_demo")
