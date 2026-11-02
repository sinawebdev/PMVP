"""Access requests from the landing page, and invite state on users.

Revision ID: a1c2026b1101
Revises: 9d2026c1020
Create Date: 2026-11-01

Existing users get NULL in both invite columns, which reads as "active": they
already sign in with a password, so nothing about their access changes.
"""

from alembic import op
import sqlalchemy as sa

revision = "a1c2026b1101"
down_revision = "9d2026c1020"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("user") as batch:
        batch.add_column(sa.Column("invited_at", sa.DateTime(), nullable=True))
        batch.add_column(sa.Column("activated_at", sa.DateTime(), nullable=True))

    op.create_table(
        "access_request",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("company_name", sa.String(160), nullable=False),
        sa.Column("contact_name", sa.String(120), nullable=False),
        sa.Column("email", sa.String(160), nullable=False),
        sa.Column("phone", sa.String(40), nullable=True),
        sa.Column("location", sa.String(120), nullable=True),
        sa.Column("employee_band", sa.String(20), nullable=True),
        sa.Column("interest", sa.String(20), nullable=False),
        sa.Column("message", sa.Text(), nullable=True),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("decline_reason", sa.String(255), nullable=True),
        sa.Column("client_company_id", sa.Integer(), sa.ForeignKey("client_company.id"), nullable=True),
        sa.Column("reviewed_by_id", sa.Integer(), sa.ForeignKey("user.id"), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
    )
    with op.batch_alter_table("access_request") as batch:
        batch.create_index("ix_access_request_status", ["status"])
        batch.create_index("ix_access_request_created_at", ["created_at"])


def downgrade():
    with op.batch_alter_table("access_request") as batch:
        batch.drop_index("ix_access_request_created_at")
        batch.drop_index("ix_access_request_status")
    op.drop_table("access_request")
    with op.batch_alter_table("user") as batch:
        batch.drop_column("activated_at")
        batch.drop_column("invited_at")
