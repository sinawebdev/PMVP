"""Ask applicants whether they run payroll for other companies.

Revision ID: a4b2026b1112
Revises: f2a2026b1112
Create Date: 2026-11-12

"company" (the default, and every request made before the question existed)
or "bureau": a firm that runs payroll for others, approved as a payroll bureau
rather than a single company.
"""

from alembic import op
import sqlalchemy as sa

revision = "a4b2026b1112"
down_revision = "f2a2026b1112"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("access_request") as batch:
        batch.add_column(
            sa.Column("account_type", sa.String(20), nullable=False, server_default="company")
        )


def downgrade():
    with op.batch_alter_table("access_request") as batch:
        batch.drop_column("account_type")
