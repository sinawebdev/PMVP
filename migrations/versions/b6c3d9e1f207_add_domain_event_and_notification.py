"""add domain_event and notification tables

Payrolla Phase 6 — the append-only DomainEvent business-event log and the
per-user Notification inbox (in-app fan-out of events). Both are new tables, so
this is purely additive; create_all covers fresh/test DBs, this migration covers
the live production database.

Revision ID: b6c3d9e1f207
Revises: a5f1c2e8b4d0
Create Date: 2026-07-17 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'b6c3d9e1f207'
down_revision = 'a5f1c2e8b4d0'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'domain_event',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('event_type', sa.String(length=80), nullable=False),
        sa.Column('actor_user_id', sa.Integer(), nullable=True),
        sa.Column('actor_role', sa.String(length=40), nullable=True),
        sa.Column('client_company_id', sa.Integer(), nullable=True),
        sa.Column('subject_type', sa.String(length=80), nullable=True),
        sa.Column('subject_id', sa.Integer(), nullable=True),
        sa.Column('summary', sa.Text(), nullable=True),
        sa.Column('payload', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['actor_user_id'], ['user.id']),
        sa.ForeignKeyConstraint(['client_company_id'], ['client_company.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('domain_event', schema=None) as batch_op:
        batch_op.create_index('ix_domain_event_event_type', ['event_type'], unique=False)
        batch_op.create_index('ix_domain_event_client_company_id', ['client_company_id'], unique=False)
        batch_op.create_index('ix_domain_event_created_at', ['created_at'], unique=False)

    op.create_table(
        'notification',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('client_company_id', sa.Integer(), nullable=True),
        sa.Column('event_id', sa.Integer(), nullable=True),
        sa.Column('title', sa.String(length=160), nullable=False),
        sa.Column('body', sa.Text(), nullable=True),
        sa.Column('level', sa.String(length=16), nullable=False),
        sa.Column('read_at', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['user_id'], ['user.id']),
        sa.ForeignKeyConstraint(['client_company_id'], ['client_company.id']),
        sa.ForeignKeyConstraint(['event_id'], ['domain_event.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('notification', schema=None) as batch_op:
        batch_op.create_index('ix_notification_user_id', ['user_id'], unique=False)
        batch_op.create_index('ix_notification_created_at', ['created_at'], unique=False)


def downgrade():
    op.drop_table('notification')
    op.drop_table('domain_event')
