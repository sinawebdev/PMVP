"""Payroll bureaus: firms that run payroll for their own client companies.

Revision ID: f2a2026b1112
Revises: e1a2026b1112
Create Date: 2026-11-12

A bureau (e.g. Chrisnat Limited) gets an operator-style interface limited to
its own clients. This adds the bureau table and three links to it:

- client_company.bureau_id  NULL = a company Payrolla runs itself
- user.bureau_id            set for a bureau's own staff (never together with
                            client_company_id: ck_user_one_scope)
- statutory_rates.bureau_id NULL = the default rates everyone uses; set = that
                            bureau's override

Rate versions were unique by effective date. Now the default set is unique by
date, and each bureau's overrides are unique by date within that bureau. Two
partial unique indexes say exactly that on both SQLite and Postgres (a plain
unique (bureau_id, effective_from) would let Postgres accept duplicate default
rows, because NULLs never compare equal there).
"""

from alembic import op
import sqlalchemy as sa

revision = "f2a2026b1112"
down_revision = "e1a2026b1112"
branch_labels = None
depends_on = None

_DEFAULT_ONLY = "bureau_id IS NULL"
_BUREAU_ONLY = "bureau_id IS NOT NULL"


def upgrade():
    op.create_table(
        "bureau",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(160), nullable=False, unique=True),
        sa.Column("code", sa.String(20), nullable=False, unique=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="Active"),
        sa.Column("contact_email", sa.String(160)),
        sa.Column("phone", sa.String(40)),
        sa.Column("created_at", sa.DateTime()),
    )

    with op.batch_alter_table("client_company") as batch:
        batch.add_column(sa.Column("bureau_id", sa.Integer(), nullable=True))
        batch.create_foreign_key("fk_client_company_bureau", "bureau", ["bureau_id"], ["id"])
        batch.create_index("ix_client_company_bureau_id", ["bureau_id"])

    with op.batch_alter_table("user") as batch:
        batch.add_column(sa.Column("bureau_id", sa.Integer(), nullable=True))
        batch.create_foreign_key("fk_user_bureau", "bureau", ["bureau_id"], ["id"])
        batch.create_index("ix_user_bureau_id", ["bureau_id"])
        batch.create_check_constraint(
            "ck_user_one_scope", "NOT (client_company_id IS NOT NULL AND bureau_id IS NOT NULL)"
        )

    with op.batch_alter_table("statutory_rates") as batch:
        batch.drop_index("ix_statutory_rates_effective_from")
        batch.create_index("ix_statutory_rates_effective_from", ["effective_from"], unique=False)
        batch.add_column(sa.Column("bureau_id", sa.Integer(), nullable=True))
        batch.create_foreign_key("fk_statutory_rates_bureau", "bureau", ["bureau_id"], ["id"])
        batch.create_index("ix_statutory_rates_bureau_id", ["bureau_id"])

    op.create_index(
        "uq_statutory_rates_default_date", "statutory_rates", ["effective_from"], unique=True,
        sqlite_where=sa.text(_DEFAULT_ONLY), postgresql_where=sa.text(_DEFAULT_ONLY),
    )
    op.create_index(
        "uq_statutory_rates_bureau_date", "statutory_rates", ["bureau_id", "effective_from"],
        unique=True,
        sqlite_where=sa.text(_BUREAU_ONLY), postgresql_where=sa.text(_BUREAU_ONLY),
    )


def downgrade():
    bind = op.get_bind()
    overrides = bind.execute(sa.text("SELECT COUNT(*) FROM statutory_rates WHERE bureau_id IS NOT NULL")).scalar()
    if overrides:
        raise RuntimeError(
            "Refusing to downgrade: bureau rate overrides exist and the old schema "
            "cannot hold them. Delete or export them first."
        )
    op.drop_index("uq_statutory_rates_bureau_date", table_name="statutory_rates")
    op.drop_index("uq_statutory_rates_default_date", table_name="statutory_rates")
    with op.batch_alter_table("statutory_rates") as batch:
        batch.drop_index("ix_statutory_rates_bureau_id")
        batch.drop_constraint("fk_statutory_rates_bureau", type_="foreignkey")
        batch.drop_column("bureau_id")
        batch.drop_index("ix_statutory_rates_effective_from")
        batch.create_index("ix_statutory_rates_effective_from", ["effective_from"], unique=True)

    with op.batch_alter_table("user") as batch:
        batch.drop_constraint("ck_user_one_scope", type_="check")
        batch.drop_index("ix_user_bureau_id")
        batch.drop_constraint("fk_user_bureau", type_="foreignkey")
        batch.drop_column("bureau_id")

    with op.batch_alter_table("client_company") as batch:
        batch.drop_index("ix_client_company_bureau_id")
        batch.drop_constraint("fk_client_company_bureau", type_="foreignkey")
        batch.drop_column("bureau_id")

    op.drop_table("bureau")
