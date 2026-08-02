"""reset distribution_worker_heartbeat to stable per-role names

Worker identity used to be the process hostname. On a container platform the
hostname is the pod name and changes on every deploy, so the table's "one row per
named worker, upserted each poll" design silently became "one new row per
release": 36 rows, 36 distinct names, all but the newest belonging to containers
that no longer exist — and all of them rendered on the operator's Distribution
Monitor.

Identity is per-role now (see app/distribution/queue.py), so the accumulated rows
can never be updated again — nothing will ever heartbeat under those names. This
clears them out once. Heartbeats are ephemeral liveness telemetry with no
dependents, and a running worker re-creates its row on the next poll (seconds),
so the delete loses nothing recoverable.

Revision ID: c3f7a2d81e64
Revises: b2f6a9c04e71
Create Date: 2026-07-31 00:00:00.000000

"""
from alembic import op

# revision identifiers, used by Alembic.
revision = 'c3f7a2d81e64'
down_revision = 'b2f6a9c04e71'
branch_labels = None
depends_on = None


def upgrade():
    op.execute("DELETE FROM distribution_worker_heartbeat")


def downgrade():
    # Nothing to restore: the rows named processes that no longer exist, and the
    # table refills itself from the running workers.
    pass
