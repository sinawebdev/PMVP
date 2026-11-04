"""app/rls_guard.py: new tables get RLS, and a deploy can never trip over it.

Most of these need a real PostgreSQL, because the guard is a no-op anywhere
else. Point PAYROLLA_TEST_POSTGRES_URL at a database you can throw away, e.g.
postgresql://postgres@localhost:55432/rls_unit. Each test works inside a
transaction it rolls back (roles too: role DDL is transactional in Postgres),
except the lock test, which has to commit one table and drops it afterwards.
"""
import os
import unittest
import uuid
from unittest import mock

os.environ["SKIP_DOTENV"] = "true"

from sqlalchemy import create_engine, text  # noqa: E402

from app import rls_guard  # noqa: E402
from app.rls_guard import enable_rls_on_unprotected_tables  # noqa: E402

PG_URL = os.environ.get("PAYROLLA_TEST_POSTGRES_URL", "")


class RlsGuardOutsidePostgresTests(unittest.TestCase):
    def test_sqlite_is_left_alone(self):
        engine = create_engine("sqlite://")
        with engine.begin() as conn:
            conn.execute(text("create table t (id integer)"))
            self.assertEqual(enable_rls_on_unprotected_tables(conn), [])
            # The transaction is still usable afterwards.
            self.assertEqual(conn.execute(text("select count(*) from t")).scalar(), 0)


def _rls_on(conn, schema, table):
    return conn.execute(
        text(
            "select c.relrowsecurity from pg_class c join pg_namespace n "
            "on n.oid = c.relnamespace where n.nspname = :s and c.relname = :t"
        ),
        {"s": schema, "t": table},
    ).scalar()


def _ensure_anon(conn):
    conn.execute(text(
        "do $$ begin if not exists (select 1 from pg_roles where rolname = 'anon') "
        "then create role anon nologin; end if; end $$"
    ))


@unittest.skipUnless(PG_URL, "Set PAYROLLA_TEST_POSTGRES_URL to a disposable PostgreSQL database.")
class RlsGuardPostgresTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.engine = create_engine(PG_URL)

    @classmethod
    def tearDownClass(cls):
        cls.engine.dispose()

    def setUp(self):
        self.conn = self.engine.connect()
        self.tx = self.conn.begin()
        self.tag = uuid.uuid4().hex[:8]

    def tearDown(self):
        self.tx.rollback()
        self.conn.close()

    def test_enables_rls_only_where_it_is_off_and_only_in_public(self):
        _ensure_anon(self.conn)
        off, on = f"guard_off_{self.tag}", f"guard_on_{self.tag}"
        other = f"guard_schema_{self.tag}"
        self.conn.execute(text(f"create table public.{off} (id int)"))
        self.conn.execute(text(f"create table public.{on} (id int)"))
        self.conn.execute(text(f"alter table public.{on} enable row level security"))
        self.conn.execute(text(f"create schema {other}"))
        self.conn.execute(text(f"create table {other}.{off} (id int)"))

        enabled = enable_rls_on_unprotected_tables(self.conn)

        self.assertIn(off, enabled)
        self.assertNotIn(on, enabled)
        self.assertTrue(_rls_on(self.conn, "public", off))
        self.assertTrue(_rls_on(self.conn, "public", on))
        self.assertFalse(_rls_on(self.conn, other, off))
        # Nothing left to do on a second run: an ordinary deploy issues no ALTER.
        self.assertEqual(enable_rls_on_unprotected_tables(self.conn), [])

    def test_plain_postgres_without_anon_role_is_left_alone(self):
        exists = self.conn.execute(
            text("select exists (select 1 from pg_roles where rolname = 'anon')")
        ).scalar()
        if exists:
            try:
                with self.conn.begin_nested():
                    self.conn.execute(text("drop role anon"))
            except Exception:  # noqa: BLE001
                self.skipTest("this database has a real anon role in use")
        table = f"guard_plain_{self.tag}"
        self.conn.execute(text(f"create table public.{table} (id int)"))

        self.assertEqual(enable_rls_on_unprotected_tables(self.conn), [])
        self.assertFalse(_rls_on(self.conn, "public", table))

    def test_owner_still_sees_its_rows_and_anon_sees_none(self):
        """The app owns its tables, so RLS without policies only blocks the API.

        Uses an owner with neither superuser nor BYPASSRLS, which is a stricter
        case than production's ``postgres`` role.
        """
        _ensure_anon(self.conn)
        owner = f"guard_owner_{self.tag}"
        table = f"guard_data_{self.tag}"
        self.conn.execute(text(f"create role {owner} nologin nosuperuser nobypassrls"))
        self.conn.execute(text(f"grant create, usage on schema public to {owner}"))
        self.conn.execute(text(f"set local role {owner}"))
        self.conn.execute(text(f"create table public.{table} (id int)"))
        self.conn.execute(text(f"insert into public.{table} values (1), (2)"))
        self.conn.execute(text(f"grant select on public.{table} to anon"))

        self.assertIn(table, enable_rls_on_unprotected_tables(self.conn))

        count = text(f"select count(*) from public.{table}")
        self.assertEqual(self.conn.execute(count).scalar(), 2)
        self.conn.execute(text("set local role anon"))
        self.assertEqual(self.conn.execute(count).scalar(), 0)

    def test_failure_to_list_tables_is_logged_not_raised(self):
        _ensure_anon(self.conn)
        broken = text("select relname from no_such_catalog")
        with mock.patch.object(rls_guard, "_TABLES_WITHOUT_RLS", broken):
            with self.assertLogs("app.rls_guard", "WARNING"):
                self.assertEqual(enable_rls_on_unprotected_tables(self.conn), [])
        # The migration transaction is not aborted; it can still commit.
        self.assertEqual(self.conn.execute(text("select 1")).scalar(), 1)

    def test_table_in_use_is_skipped_after_lock_timeout(self):
        """A busy table must not stall the deploy or the live site's queries."""
        busy = f"guard_busy_{self.tag}"
        fresh = f"guard_fresh_{self.tag}"
        with self.engine.connect().execution_options(isolation_level="AUTOCOMMIT") as admin:
            admin.execute(text(f"create table public.{busy} (id int)"))
        holder = self.engine.connect()
        held = holder.begin()
        try:
            # Stands in for the old instance still serving traffic mid-deploy.
            holder.execute(text(f"lock table public.{busy} in access share mode"))
            _ensure_anon(self.conn)
            self.conn.execute(text(f"create table public.{fresh} (id int)"))

            with mock.patch.object(rls_guard, "LOCK_TIMEOUT", "200ms"):
                with self.assertLogs("app.rls_guard", "WARNING") as logs:
                    enabled = enable_rls_on_unprotected_tables(self.conn)

            self.assertIn(fresh, enabled)
            self.assertNotIn(busy, enabled)
            self.assertIn(busy, "\n".join(logs.output))
            self.assertEqual(self.conn.execute(text("select 1")).scalar(), 1)
        finally:
            held.rollback()
            holder.close()
            with self.engine.connect().execution_options(isolation_level="AUTOCOMMIT") as admin:
                admin.execute(text(f"drop table if exists public.{busy}"))


if __name__ == "__main__":
    unittest.main()
