"""add distribution_batch table

Payrolla Phase 3, Slice 1 — the payslip distribution queue. A DistributionBatch
is one queued "send"/"resend-failed" action for a run; a worker claims it and
runs the existing distribute_run() against it instead of the request thread
blocking on every network send. Purely additive.

Revision ID: c1a9e5f7b3d2
Revises: b6c3d9e1f207
Create Date: 2026-07-22 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'c1a9e5f7b3d2'
down_revision = 'b6c3d9e1f207'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'distribution_batch',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('payroll_run_id', sa.Integer(), nullable=False),
        sa.Column('client_company_id', sa.Integer(), nullable=False),
        sa.Column('channel', sa.String(length=16), nullable=False),
        sa.Column('only_failed', sa.Boolean(), nullable=False),
        sa.Column('status', sa.String(length=16), nullable=False),
        sa.Column('initiated_by_user_id', sa.Integer(), nullable=True),
        sa.Column('initiated_by_role', sa.String(length=40), nullable=True),
        sa.Column('total', sa.Integer(), nullable=True),
        sa.Column('sent_count', sa.Integer(), nullable=True),
        sa.Column('failed_count', sa.Integer(), nullable=True),
        sa.Column('skipped_count', sa.Integer(), nullable=True),
        sa.Column('error', sa.String(length=512), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('started_at', sa.DateTime(), nullable=True),
        sa.Column('finished_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['payroll_run_id'], ['payroll_run.id']),
        sa.ForeignKeyConstraint(['client_company_id'], ['client_company.id']),
        sa.ForeignKeyConstraint(['initiated_by_user_id'], ['user.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('distribution_batch', schema=None) as batch_op:
        batch_op.create_index(
            'ix_distribution_batch_payroll_run_id', ['payroll_run_id'], unique=False
        )
        batch_op.create_index(
            'ix_distribution_batch_client_company_id', ['client_company_id'], unique=False
        )
        batch_op.create_index('ix_distribution_batch_status', ['status'], unique=False)
        batch_op.create_index('ix_distribution_batch_created_at', ['created_at'], unique=False)


def downgrade():
    op.drop_table('distribution_batch')
