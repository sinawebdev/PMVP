"""Switch Row Level Security on for any table a migration leaves without it.

Production runs on Supabase, which also serves every table in the ``public``
schema through its REST API. Anyone with the project's anon key (a key that is
meant to be public) can call that API as the ``anon`` role. RLS with no
policies denies that role everything, which is all ``scripts/rls_stage1.sql``
does (MULTI_TENANT.md, "Row-Level Security").

That script only covers the tables that exist when someone runs it. Alembic
creates tables with RLS off, so every table added since the script last ran
stayed readable through the API until someone re-ran it by hand. On 2026-11-04
three were found that way, one of them ``access_request``, which holds names and
emails from the landing page. ``migrations/env.py`` now calls this after any
migration step, in the same transaction, so a new table is never committed
without RLS.

It is built so it cannot get in the way of a deploy:

- It only acts on PostgreSQL with an ``anon`` role, i.e. Supabase. SQLite (tests,
  the desktop app) and plain Postgres are left alone.
- It only alters tables whose RLS is off, so a deploy that adds no table issues
  no ALTER and takes no lock.
- It never fails the migration. Each table gets its own savepoint and a short
  lock timeout. A table it cannot change is logged and skipped, and the
  migrations still commit.
- The app does not notice. RLS is ENABLEd, not FORCEd, so it never applies to a
  table's owner. Migrations and the app connect with the same DATABASE_URL role,
  which creates, and so owns, these tables. On Supabase that role (``postgres``)
  also has BYPASSRLS.

It never creates, changes or drops a policy. To let the Supabase API read a
table on purpose, give the table a policy and leave RLS on. If RLS is switched
off by hand, the next migration switches it back on.
"""
import logging

from sqlalchemy import text

_log = logging.getLogger(__name__)

# How long to wait for a table's lock before skipping that table. A table the
# migration just created is already locked by this transaction, so this only
# matters for an existing table that someone switched RLS off on while the
# running site was using it. Waiting there would stall the site's queries on
# that table, so skip it and say so instead.
LOCK_TIMEOUT = "5s"

_ON_SUPABASE = text("select exists (select 1 from pg_roles where rolname = 'anon')")

_TABLES_WITHOUT_RLS = text(
    """
    select c.relname
    from pg_class c
    join pg_namespace n on n.oid = c.relnamespace
    where n.nspname = 'public'
      and c.relkind in ('r', 'p')
      and not c.relrowsecurity
    order by c.relname
    """
)


def enable_rls_on_unprotected_tables(connection, log=_log):
    """Enable RLS on each ``public`` table that lacks it. Returns the names done.

    Call it inside the migration transaction, after the migrations have run.
    It never raises.
    """
    if connection.dialect.name != "postgresql":
        return []

    try:
        with connection.begin_nested():
            if not connection.execute(_ON_SUPABASE).scalar():
                return []
            tables = list(connection.execute(_TABLES_WITHOUT_RLS).scalars())
    except Exception as exc:  # noqa: BLE001 - a guard must not break a deploy
        log.warning("RLS check skipped, could not list tables: %s", exc)
        return []

    if not tables:
        return []

    # SET LOCAL lasts until this transaction ends, and only the commit is left.
    connection.execute(text(f"set local lock_timeout = '{LOCK_TIMEOUT}'"))
    quote = connection.dialect.identifier_preparer.quote_identifier
    enabled, skipped = [], []
    for table in tables:
        try:
            with connection.begin_nested():
                connection.execute(
                    text(f"alter table public.{quote(table)} enable row level security")
                )
            enabled.append(table)
        except Exception as exc:  # noqa: BLE001 - skip this table, keep going
            skipped.append(table)
            log.warning("Could not enable Row Level Security on %s: %s", table, exc)

    if enabled:
        log.info("Enabled Row Level Security on: %s", ", ".join(enabled))
    if skipped:
        log.warning(
            "Row Level Security is still OFF on: %s. Supabase's public API can "
            "read these tables. Run scripts/rls_stage1.sql in the SQL editor.",
            ", ".join(skipped),
        )
    return enabled
