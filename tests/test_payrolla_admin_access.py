"""The platform role vocabulary: capabilities, the chrisnat_* rename, and its migration.

Three things are pinned here, in order:

  1. ``payrolla_admin`` (SaaS-era platform superuser) has full operator access —
     the policy settled with Sina: it sees the operator nav and may reach
     operator routes gated by ``role_required`` (statutory, audit, …), which the
     legacy operator role lists did not grant.
  2. The pre-rename spellings still grant exactly that same access, so the
     ``chrisnat_* -> payrolla_*`` rename cannot cost anyone a capability.
  3. The data migration that moves the persisted strings round-trips.

Runs on in-memory SQLite (never the production Supabase DB).
"""

import os
import tempfile
import unittest

os.environ["SKIP_DOTENV"] = "true"
os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["SEED_DEMO_DATA"] = "true"
os.environ["PERSISTENCE_REQUIRED"] = "false"

from flask import url_for  # noqa: E402

from app import create_app, db, permissions  # noqa: E402
from app.models import User  # noqa: E402
from app.roles import LEGACY_ROLE_ALIASES, PAYROLLA_ADMIN, PAYROLLA_REVIEWER  # noqa: E402


class PayrollaAdminAccessTestCase(unittest.TestCase):
    def setUp(self):
        self.app = create_app()
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()

    def _login_platform(self):
        resp = self.client.post(
            "/login",
            data={"email": "operator@payrolla.com", "password": "password123"},
        )
        self.assertEqual(resp.status_code, 302)  # -> /dashboard

    def test_predicates_grant_every_operator_capability(self):
        self.assertTrue(permissions.can_operate_payroll(PAYROLLA_ADMIN))
        self.assertTrue(permissions.can_maintain_roster(PAYROLLA_ADMIN))
        self.assertTrue(permissions.can_view_audit(PAYROLLA_ADMIN))
        self.assertTrue(permissions.can_manage_statutory(PAYROLLA_ADMIN))

    def test_reaches_role_required_operator_routes(self):
        # statutory.index is @role_required("admin"); audit is
        # @role_required("admin", "md"). payrolla_admin was in neither list, so
        # before the superuser pass these would 302-bounce to /dashboard.
        self._login_platform()
        with self.app.test_request_context():
            statutory_url = url_for("statutory.index")
            audit_url = url_for("audit.audit_trail")
        self.assertEqual(self.client.get(statutory_url).status_code, 200)
        self.assertEqual(self.client.get(audit_url).status_code, 200)

    def test_sees_operator_nav_links(self):
        self._login_platform()
        body = self.client.get("/dashboard").get_data(as_text=True)
        self.assertIn("Statutory Rates", body)   # can_manage_statutory
        self.assertIn("Expenses", body)          # "Expenses & Audit" (can_view_audit)


class LegacyRoleAliasTestCase(unittest.TestCase):
    """The pre-rename spellings still grant exactly the same access.

    The ``chrisnat_* -> payrolla_*`` rename ships as a data migration, so a row
    can still carry the old string — an un-migrated replica, a restored backup,
    a fixture. ``normalise_role`` folds those onto the current names, and these
    tests pin that: no user loses a capability because of the rename.
    """

    def setUp(self):
        self.app = create_app()
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()

    def test_alias_map_covers_both_renamed_roles(self):
        self.assertEqual(
            LEGACY_ROLE_ALIASES,
            {
                "chrisnat_admin": PAYROLLA_ADMIN,
                "chrisnat_reviewer": PAYROLLA_REVIEWER,
            },
        )

    def test_legacy_admin_keeps_every_operator_capability(self):
        for predicate in (
            permissions.can_operate_payroll,
            permissions.can_maintain_roster,
            permissions.can_view_audit,
            permissions.can_manage_statutory,
        ):
            with self.subTest(predicate=predicate.__name__):
                self.assertEqual(
                    predicate("chrisnat_admin"), predicate(PAYROLLA_ADMIN)
                )
                self.assertTrue(predicate("chrisnat_admin"))

    def test_legacy_roles_are_still_platform_roles(self):
        from app.roles import is_platform_role

        self.assertTrue(is_platform_role("chrisnat_admin"))
        self.assertTrue(is_platform_role("chrisnat_reviewer"))

    def test_user_with_unmigrated_role_still_reaches_operator_routes(self):
        """The end-to-end guarantee: a User row left on the old string logs in
        and reaches the same role_required routes as a migrated one."""
        with self.app.app_context():
            user = User.query.filter_by(email="operator@payrolla.com").first()
            self.assertEqual(user.role, PAYROLLA_ADMIN)
            user.role = "chrisnat_admin"  # simulate an un-migrated row
            db.session.commit()

        resp = self.client.post(
            "/login",
            data={"email": "operator@payrolla.com", "password": "password123"},
        )
        self.assertEqual(resp.status_code, 302)
        with self.app.test_request_context():
            statutory_url = url_for("statutory.index")
            audit_url = url_for("audit.audit_trail")
        self.assertEqual(self.client.get(statutory_url).status_code, 200)
        self.assertEqual(self.client.get(audit_url).status_code, 200)

    def test_legacy_role_renders_under_the_current_label(self):
        from app import format_role_label

        self.assertEqual(format_role_label("chrisnat_admin"), "Payrolla Admin")
        self.assertEqual(format_role_label("chrisnat_reviewer"), "Payrolla Reviewer")


class RoleRenameMigrationTestCase(unittest.TestCase):
    """The rename migration moves persisted role strings, both ways.

    Data-only, so the check is on row values rather than schema: downgrade must
    restore the original strings exactly (a rolled-back deploy runs old code
    that writes ``chrisnat_*``), and upgrade must move every one of them.
    """

    ROLE_COLUMNS = (
        ('user', 'role'),
        ('audit_trail', 'user_role'),
        ('domain_event', 'actor_role'),
        ('distribution_batch', 'initiated_by_role'),
    )

    def test_role_strings_round_trip(self):
        import sqlalchemy as sa
        from flask_migrate import downgrade as fm_downgrade
        from flask_migrate import stamp as fm_stamp
        from flask_migrate import upgrade as fm_upgrade

        from app import create_app as _create_app

        tmp = tempfile.mkdtemp()
        dbfile = os.path.join(tmp, "role_rename.sqlite")
        previous = os.environ.get("DATABASE_URL")
        os.environ["DATABASE_URL"] = f"sqlite:///{dbfile}"
        try:
            app = _create_app()  # create_all builds the current schema + seed
            with app.app_context():
                fm_stamp(revision="head")

                # Seed wrote the new names; every role-bearing table is checked,
                # so a column added to ROLE_COLUMNS but missed by the migration
                # fails here.
                def roles_in(table, column):
                    rows = db.session.execute(
                        sa.text(f'SELECT DISTINCT {column} FROM "{table}"')
                    ).scalars().all()
                    return {r for r in rows if r}

                self.assertIn(
                    PAYROLLA_ADMIN, roles_in('user', 'role')
                )

                fm_downgrade(revision="e5a2c8d13f60")
                after_down = roles_in('user', 'role')
                self.assertIn("chrisnat_admin", after_down)
                self.assertIn("chrisnat_reviewer", after_down)
                self.assertNotIn(PAYROLLA_ADMIN, after_down)
                self.assertNotIn(PAYROLLA_REVIEWER, after_down)

                fm_upgrade(revision="head")
                for table, column in self.ROLE_COLUMNS:
                    with self.subTest(table=table, column=column):
                        values = roles_in(table, column)
                        self.assertFalse(
                            values & set(LEGACY_ROLE_ALIASES),
                            f"{table}.{column} still holds a pre-rename role",
                        )
                after_up = roles_in('user', 'role')
                self.assertIn(PAYROLLA_ADMIN, after_up)
                self.assertIn(PAYROLLA_REVIEWER, after_up)
        finally:
            os.environ["DATABASE_URL"] = previous or "sqlite:///:memory:"


if __name__ == "__main__":
    unittest.main()
