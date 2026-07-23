"""add distribution_worker_heartbeat

PMVP v1 Phase 4 — worker deployment hardening. One row per named worker process,
upserted on each poll, so the monitoring dashboard can see an external
`flask distribution-worker` process (the previous heartbeat was in-process only).
New table; purely additive.

Revision ID: a7e3c9f21b48
Revises: f4d2b8c6a1e7
Create Date: 2026-07-23 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'a7e3c9f21b48'
down_revision = 'f4d2b8c6a1e7'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'distribution_worker_heartbeat',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('worker_name', sa.String(length=120), nullable=False),
        sa.Column('host', sa.String(length=120), nullable=True),
        sa.Column('pid', sa.Integer(), nullable=True),
        sa.Column('status', sa.String(length=16), nullable=False),
        sa.Column('started_at', sa.DateTime(), nullable=True),
        sa.Column('last_poll_at', sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('distribution_worker_heartbeat', schema=None) as batch_op:
        batch_op.create_index(
            'ix_distribution_worker_heartbeat_worker_name', ['worker_name'], unique=True
        )
        batch_op.create_index(
            'ix_distribution_worker_heartbeat_last_poll_at', ['last_poll_at'], unique=False
        )


def downgrade():
    op.drop_table('distribution_worker_heartbeat')
