"""tighten statutory_rates.overtime_junior_monthly_threshold to NOT NULL

The model has declared this column ``nullable=False`` since Phase 4
(app/models.py), but the migration that created it — f1a9d3c07b62 — added it as
``nullable=True``. That migration was written to be a safe no-op against
production, where the column had already been added by hand to break a boot
deadlock, and the looser nullability came along with it.

The result is a schema the chain cannot reproduce: a database built by
``flask db upgrade`` allows NULL in a column the models forbid it in. That is
invisible in practice — ``default=1500.0`` is applied Python-side on every
insert, so no NULL is ever written — but it means "rebuild the database from the
migrations" does not give you the database the application expects, and every
future ``--autogenerate`` run emits this as a phantom diff.

Backfills first, then tightens. The backfill is defensive rather than expected:
if the Python default has done its job there is nothing to update, and on
PostgreSQL the ALTER would fail loudly rather than silently coerce if there
were.
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'a1c5e9b73f40'
down_revision = 'd7b4e2a09c31'
branch_labels = None
depends_on = None

TABLE = 'statutory_rates'
COLUMN = 'overtime_junior_monthly_threshold'
DEFAULT = 1500.0


def upgrade():
    op.execute(
        sa.text(
            f"UPDATE {TABLE} SET {COLUMN} = :default WHERE {COLUMN} IS NULL"
        ).bindparams(default=DEFAULT)
    )
    # batch_alter_table so this works on SQLite too, where a column cannot be
    # altered in place and the table has to be rebuilt.
    with op.batch_alter_table(TABLE, schema=None) as batch_op:
        batch_op.alter_column(
            COLUMN,
            existing_type=sa.Float(),
            existing_server_default=sa.text('1500'),
            nullable=False,
        )


def downgrade():
    with op.batch_alter_table(TABLE, schema=None) as batch_op:
        batch_op.alter_column(
            COLUMN,
            existing_type=sa.Float(),
            existing_server_default=sa.text('1500'),
            nullable=True,
        )
