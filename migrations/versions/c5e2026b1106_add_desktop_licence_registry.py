"""Add the desktop licence registry.

Revision ID: c5e2026b1106
Revises: b3d2026b1102
Create Date: 2026-11-06

One row per desktop licence the server issues, with the signed document kept so
a failed email can be resent with the same licence.
"""

from alembic import op
import sqlalchemy as sa

revision = "c5e2026b1106"
down_revision = "b3d2026b1102"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "desktop_licence",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("license_id", sa.String(40), nullable=False),
        sa.Column("install_id", sa.String(64), nullable=False),
        sa.Column("firm_name", sa.String(160), nullable=False),
        sa.Column("plan", sa.String(40), nullable=False),
        sa.Column("expires_on", sa.Date(), nullable=False),
        sa.Column("max_employees", sa.Integer(), nullable=False),
        sa.Column("document", sa.Text(), nullable=False),
        sa.Column("recipient", sa.String(160), nullable=False),
        sa.Column("access_request_id", sa.Integer(),
                  sa.ForeignKey("access_request.id", name="fk_desktop_licence_access_request")),
        sa.Column("issued_by_id", sa.Integer(),
                  sa.ForeignKey("user.id", name="fk_desktop_licence_issued_by")),
        sa.Column("issued_at", sa.DateTime(), nullable=False),
        sa.Column("emailed_at", sa.DateTime()),
        sa.Column("email_error", sa.String(255)),
        sa.UniqueConstraint("license_id", name="uq_desktop_licence_license_id"),
    )
    op.create_index("ix_desktop_licence_access_request_id", "desktop_licence", ["access_request_id"])
    op.create_index("ix_desktop_licence_issued_at", "desktop_licence", ["issued_at"])


def downgrade():
    op.drop_index("ix_desktop_licence_issued_at", table_name="desktop_licence")
    op.drop_index("ix_desktop_licence_access_request_id", table_name="desktop_licence")
    op.drop_table("desktop_licence")
