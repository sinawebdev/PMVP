"""Let a company or bureau admin take a colleague's access away.

Revision ID: c6d2026b1120
Revises: b5c2026b1114
Create Date: 2026-11-20

A login can't simply be deleted: payroll runs, vouchers, expenses and the audit
trail all point at the person who did the work. user.disabled_at marks a login
that has been removed from its team instead. A removed login cannot sign in, an
open session ends on its next request, and an unaccepted invite link stops
working. NULL means the login is in use, which is every existing row.
"""

from alembic import op
import sqlalchemy as sa

revision = "c6d2026b1120"
down_revision = "b5c2026b1114"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("user") as batch:
        batch.add_column(sa.Column("disabled_at", sa.DateTime(), nullable=True))


def downgrade():
    with op.batch_alter_table("user") as batch:
        batch.drop_column("disabled_at")
