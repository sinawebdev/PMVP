"""SMS delivery state and short payslip links

SMS Phase 2 (plans/sms-distribution.md):

  * payslip_delivery gains ``units`` (parts the provider billed),
    ``claimed_at`` (when the current attempt was claimed; drives the stale-claim
    recovery) and ``template_version`` (which SMS wording went out; the body
    itself is never stored).
  * ``ix_payslip_delivery_item_channel`` becomes the unique constraint
    ``uq_payslip_delivery_item_channel`` on (payroll_item_id, channel): one row
    per payslip per channel, so two workers cannot each insert one and send.
    Production had 0 duplicates when checked on 29 Sep 2026; on PostgreSQL the
    constraint fails loudly rather than silently if that has changed.
  * distribution_batch gains ``unknown_count`` beside sent_count/failed_count.
  * new table payslip_link: the hashed short codes behind /s/<code>. Both of
    its foreign keys cascade on delete, so the existing run and company delete
    paths take the links with them.

The new statuses (`sending`, `unknown`) need no schema change: status is a
String(16). ``downgrade()`` reverses every step; rows already in those two
statuses keep them, and a downgraded app treats them as neither sent nor failed.

Revision ID: 3f2984c70c03
Revises: a1c5e9b73f40
Create Date: 2026-10-02 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '3f2984c70c03'
down_revision = 'a1c5e9b73f40'
branch_labels = None
depends_on = None


def upgrade():
    # batch_alter_table so this runs on SQLite too, where columns and
    # constraints cannot be altered in place and the table is rebuilt.
    with op.batch_alter_table('payslip_delivery', schema=None) as batch_op:
        batch_op.add_column(sa.Column('units', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('claimed_at', sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column('template_version', sa.String(length=32), nullable=True))
        batch_op.drop_index('ix_payslip_delivery_item_channel')
        batch_op.create_unique_constraint(
            'uq_payslip_delivery_item_channel', ['payroll_item_id', 'channel']
        )

    with op.batch_alter_table('distribution_batch', schema=None) as batch_op:
        batch_op.add_column(sa.Column('unknown_count', sa.Integer(), nullable=True))

    op.create_table(
        'payslip_link',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('code_hash', sa.String(length=64), nullable=False),
        sa.Column('payroll_item_id', sa.Integer(), nullable=False),
        sa.Column('payslip_delivery_id', sa.Integer(), nullable=True),
        sa.Column('token_version', sa.Integer(), nullable=False),
        sa.Column('expires_at', sa.DateTime(), nullable=False),
        sa.Column('revoked_at', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(
            ['payroll_item_id'], ['payroll_item.id'],
            name='fk_payslip_link_payroll_item_id', ondelete='CASCADE',
        ),
        sa.ForeignKeyConstraint(
            ['payslip_delivery_id'], ['payslip_delivery.id'],
            name='fk_payslip_link_payslip_delivery_id', ondelete='CASCADE',
        ),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_payslip_link_code_hash', 'payslip_link', ['code_hash'], unique=True)
    op.create_index(
        'ix_payslip_link_payroll_item_id', 'payslip_link', ['payroll_item_id'], unique=False
    )
    op.create_index(
        'ix_payslip_link_payslip_delivery_id', 'payslip_link', ['payslip_delivery_id'],
        unique=False,
    )


def downgrade():
    op.drop_index('ix_payslip_link_payslip_delivery_id', table_name='payslip_link')
    op.drop_index('ix_payslip_link_payroll_item_id', table_name='payslip_link')
    op.drop_index('ix_payslip_link_code_hash', table_name='payslip_link')
    op.drop_table('payslip_link')

    with op.batch_alter_table('distribution_batch', schema=None) as batch_op:
        batch_op.drop_column('unknown_count')

    with op.batch_alter_table('payslip_delivery', schema=None) as batch_op:
        batch_op.drop_constraint('uq_payslip_delivery_item_channel', type_='unique')
        batch_op.create_index(
            'ix_payslip_delivery_item_channel', ['payroll_item_id', 'channel'], unique=False
        )
        batch_op.drop_column('template_version')
        batch_op.drop_column('claimed_at')
        batch_op.drop_column('units')
