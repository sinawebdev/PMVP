"""add expense_receipt

Expense receipt attachments. One receipt per expense, so ``expense_id`` is
UNIQUE — the "single attachment slot" the form offers is enforced by the schema
rather than by convention.

The row holds metadata only; the bytes live in the configured
:mod:`app.storage` backend under ``storage_key``. That key is backend-neutral
(not a filesystem path), so moving to Supabase Storage needs no data change.

``client_company_id`` is denormalised from the parent expense so a receipt is
tenant-scopable without a join, matching the other tenant-owned models in
``app/tenancy.py``. It is NOT NULL: a receipt with no owning tenant could not be
isolation-checked.

New table only — nothing existing is altered, so this applies to a populated
database with no backfill and no downtime.

Revision ID: b2f6a9c04e71
Revises: a4e7c2b81d95
Create Date: 2026-07-28 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'b2f6a9c04e71'
down_revision = 'a4e7c2b81d95'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'expense_receipt',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('expense_id', sa.Integer(), nullable=False),
        sa.Column('client_company_id', sa.Integer(), nullable=False),
        sa.Column('original_filename', sa.String(length=255), nullable=False),
        sa.Column('storage_key', sa.String(length=400), nullable=False),
        sa.Column('content_type', sa.String(length=100), nullable=False),
        sa.Column('byte_size', sa.Integer(), nullable=False),
        sa.Column('uploaded_at', sa.DateTime(), nullable=False),
        sa.Column('uploaded_by', sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(
            ['client_company_id'], ['client_company.id'],
            name='fk_expense_receipt_client_company_id',
        ),
        sa.ForeignKeyConstraint(
            ['expense_id'], ['expense.id'], name='fk_expense_receipt_expense_id',
        ),
        sa.ForeignKeyConstraint(
            ['uploaded_by'], ['user.id'], name='fk_expense_receipt_uploaded_by',
        ),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('expense_receipt', schema=None) as batch_op:
        # Unique: one receipt per expense.
        batch_op.create_index(
            'ix_expense_receipt_expense_id', ['expense_id'], unique=True
        )
        # Non-unique: the tenant filter every scoped query applies.
        batch_op.create_index(
            'ix_expense_receipt_client_company_id', ['client_company_id'], unique=False
        )


def downgrade():
    with op.batch_alter_table('expense_receipt', schema=None) as batch_op:
        batch_op.drop_index('ix_expense_receipt_client_company_id')
        batch_op.drop_index('ix_expense_receipt_expense_id')
    op.drop_table('expense_receipt')
