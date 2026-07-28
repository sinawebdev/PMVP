"""rename chrisnat_* platform roles to payrolla_*

Brand cleanup — the platform role vocabulary carried the founding operator's
name (Chrisnat Limited) rather than the product's. This is a pure rename: the
capabilities behind each role are unchanged, only the persisted string moves.

  chrisnat_admin    -> payrolla_admin
  chrisnat_reviewer -> payrolla_reviewer

Data-only (no schema change). Four columns hold role strings:

  * ``user.role``                       — the permission-bearing column. Every
    permission check in the app reads it, so this is the one that must move for
    the rename to be real.
  * ``audit_trail.user_role``           — historical
  * ``domain_event.actor_role``         — historical
  * ``distribution_batch.initiated_by_role`` — historical

The historical columns are rewritten too. They record *which role* acted, and
the role is the same role — leaving the old spelling there would put two names
for one role in the audit trail and keep retired branding on screen. No event's
meaning changes.

``employee_deployment.role`` is deliberately untouched: that is a job title on a
deployment record, not a permission role, and it shares only the column name.

Safety: every statement is a targeted UPDATE ... WHERE role = <old value>, so
rows carrying any other role are never read or written. No user gains or loses a
capability — :func:`app.roles.normalise_role` already folds the old spellings
onto the new ones, so access is identical before, during and after this runs.

``downgrade`` is the exact inverse, restoring the original strings.

Revision ID: a4e7c2b81d95
Revises: e5a2c8d13f60
Create Date: 2026-07-28 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'a4e7c2b81d95'
down_revision = 'e5a2c8d13f60'
branch_labels = None
depends_on = None


# (table, column) pairs holding a platform role string.
ROLE_COLUMNS = (
    ('user', 'role'),
    ('audit_trail', 'user_role'),
    ('domain_event', 'actor_role'),
    ('distribution_batch', 'initiated_by_role'),
)

RENAMES = (
    ('chrisnat_admin', 'payrolla_admin'),
    ('chrisnat_reviewer', 'payrolla_reviewer'),
)


def _rewrite(pairs):
    """Apply (old -> new) role renames across every role-bearing column.

    Tables are skipped when absent so the migration stays applicable to a
    database stamped before those tables existed. ``user`` is quoted because it
    is a reserved word in PostgreSQL.
    """
    bind = op.get_bind()
    existing = set(sa.inspect(bind).get_table_names())
    for table, column in ROLE_COLUMNS:
        if table not in existing:
            continue
        for old, new in pairs:
            op.execute(
                sa.text(
                    f'UPDATE "{table}" SET {column} = :new WHERE {column} = :old'
                ).bindparams(new=new, old=old)
            )


def upgrade():
    _rewrite(RENAMES)


def downgrade():
    _rewrite(tuple((new, old) for old, new in RENAMES))
