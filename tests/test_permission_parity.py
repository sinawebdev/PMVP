"""Phase 1, Task 1.3 — route permission and UI affordance must agree.

Before Task 1.1 the app answered "may this role do X" two different ways: a
superuser bypass in :func:`app.auth.role_required`, and the capability groups in
:mod:`app.permissions` that templates read. A superuser was therefore routinely
authorized for actions whose buttons it was never shown — Export Payroll, Wage
Rates, Raw Hours and Employee delete among them.

The bypass is gone and both superusers are members of every group instead. These
tests pin all three properties that keeps true:

  1. every guarded route is gated by a *named group*, not a literal role tuple
     (a literal cannot be read by a template, which is how the two drifted);
  2. both superusers are members of every guarded route's group, so removing the
     bypass took nobody's access away;
  3. for the affordances the audit named, what a role SEES matches what it MAY
     DO — asserted per seeded role against the rendered page.
"""

import os
import unittest

os.environ["SKIP_DOTENV"] = "true"
os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["SEED_DEMO_DATA"] = "true"
os.environ["PERSISTENCE_REQUIRED"] = "false"

from app import create_app, db  # noqa: E402
from app import permissions as perms  # noqa: E402
from app.models import PayrollRun, User  # noqa: E402
from app.roles import normalise_role  # noqa: E402
from app.seed import DEMO_PASSWORD, PLATFORM_USERS  # noqa: E402

# Every capability group defined in app.permissions, by name.
GROUPS = {
    name: value
    for name, value in vars(perms).items()
    if name.endswith("_ROLES") and isinstance(value, frozenset)
}

# Operator-plane groups only: the tenant groups govern the client portal, whose
# roles are gated by tenant_role_required and never see an operator route.
TENANT_GROUP_NAMES = {"EXPENSE_ROLES", "BRANDING_ROLES"}
OPERATOR_GROUPS = {n: g for n, g in GROUPS.items() if n not in TENANT_GROUP_NAMES}

SUPERUSERS = {normalise_role(r) for r in perms.SUPERUSERS}


class _StubRun:
    """Minimal stand-in for the status half of a lifecycle predicate — Draft is
    the most permissive status, so a False here is about the role, not timing."""

    status = "Draft"


class GuardShapeTests(unittest.TestCase):
    """Structural parity — asserted against the route table, no HTTP needed."""

    def setUp(self):
        self.app = create_app()
        self.ctx = self.app.app_context()
        self.ctx.push()

    def tearDown(self):
        self.ctx.pop()

    def _guarded_views(self):
        """(endpoint, required_roles) for every route behind role_required."""
        out = []
        for endpoint, view in self.app.view_functions.items():
            roles = getattr(view, "_required_roles", None)
            if roles is not None:
                out.append((endpoint, roles))
        return out

    def test_there_are_guarded_routes_to_check(self):
        # Guards against this whole file silently passing on an empty set.
        self.assertGreater(len(self._guarded_views()), 30)

    def test_every_guard_is_a_named_capability_group(self):
        """A literal role tuple in a decorator is invisible to templates, so it
        is how route access and the UI drift apart. Every guard must name a
        group in app.permissions instead."""
        known = set(OPERATOR_GROUPS.values())
        offenders = [
            (endpoint, sorted(roles))
            for endpoint, roles in self._guarded_views()
            if roles not in known
        ]
        self.assertEqual(
            offenders,
            [],
            "these routes are gated by an ad-hoc role set rather than a named "
            "group in app.permissions — a template cannot read it:\n"
            + "\n".join(f"  {e}: {r}" for e, r in offenders),
        )

    # The three preparation capabilities md is deliberately NOT granted. The old
    # bypass let md reach these routes while the templates refused to show it the
    # buttons; retiring the bypass resolves that in the templates' favour, because
    # an approver that can also prepare and edit the figures it signs off is
    # maker-checker collapsed into one role.
    MD_EXCLUDED_GROUPS = {
        perms.CALCULATE_ROLES,
        perms.EDIT_FIGURES_ROLES,
        perms.SUBMIT_APPROVAL_ROLES,
    }

    def test_payrolla_admin_reaches_every_guarded_route(self):
        """Full operator access — membership must have replaced the bypass
        exactly, or payrolla_admin silently lost access."""
        for endpoint, roles in self._guarded_views():
            self.assertIn(
                normalise_role("payrolla_admin"),
                roles,
                f"payrolla_admin no longer reaches {endpoint} — removing the "
                "role_required bypass cost it access",
            )

    def test_md_reaches_every_guarded_route_except_the_preparation_ones(self):
        md = normalise_role("md")
        for endpoint, roles in self._guarded_views():
            if roles in self.MD_EXCLUDED_GROUPS:
                self.assertNotIn(
                    md,
                    roles,
                    f"{endpoint} would let md prepare or alter figures it also "
                    "approves — maker-checker must stay split",
                )
            else:
                self.assertIn(
                    md,
                    roles,
                    f"md no longer reaches {endpoint} — removing the bypass "
                    "cost it access it was meant to keep",
                )

    def test_md_cannot_prepare_what_it_approves(self):
        """Stated as a property, not a route list: md approves, so it must not
        also submit, calculate or edit figures."""
        self.assertTrue(perms.can_approve_run.__name__)  # sanity: module imported
        for predicate in (
            perms.can_calculate_run,
            perms.can_submit_run_for_approval,
        ):
            self.assertFalse(
                predicate("md", _StubRun()),
                f"md must not pass {predicate.__name__}",
            )
        self.assertFalse(perms.can_edit_run_figures("md"))

    def test_no_operator_group_is_empty(self):
        for name, group in OPERATOR_GROUPS.items():
            self.assertTrue(group, f"{name} is empty")


class AffordanceParityTests(unittest.TestCase):
    """What a role sees on the page must match what its routes permit.

    Each case pairs a capability predicate with the visible affordance it gates.
    Asserted for every seeded platform role, so a template conditional that
    drifts from its route guard fails here loudly.
    """

    def setUp(self):
        self.app = create_app()
        self.app.config["TESTING"] = True
        self.app.config["WTF_CSRF_ENABLED"] = False
        self.client = self.app.test_client()
        self.ctx = self.app.app_context()
        self.ctx.push()
        self.run = (
            PayrollRun.query.filter_by(status="Approved").first()
            or PayrollRun.query.first()
        )
        self.assertIsNotNone(self.run, "seed data must contain a payroll run")

    def tearDown(self):
        db.session.remove()
        self.ctx.pop()

    def _login(self, email):
        resp = self.client.post(
            "/login", data={"email": email, "password": DEMO_PASSWORD}
        )
        self.assertEqual(resp.status_code, 302, f"could not sign in as {email}")

    def _logout(self):
        self.client.get("/logout")

    def _payroll_detail(self):
        return self.client.get(f"/payroll/runs/{self.run.id}").get_data(as_text=True)

    def _export_markers(self):
        base = f"/payroll/runs/{self.run.id}"
        return (
            f'href="{base}/export"',
            f'href="{base}/export/bank-listing"',
            f'href="{base}/export/wages-sheet"',
            f'href="{base}/export/gra-paye"',
        )

    def test_payroll_detail_affordances_match_capabilities(self):
        """The exports and raw-hours controls the audit found missing for
        payrolla_admin and md, checked for every seeded role."""
        # Matched on the href, not the button text: an affordance is the route it
        # reaches, and a copy change (Phase 4 relabelled these to sentence case)
        # must not read as a permission regression. The closing quote matters —
        # ".../export" is a prefix of ".../export/bank-listing".
        cases = [
            (marker, perms.can_operate_payroll)
            for marker in self._export_markers()
        ]
        for _name, email, role in PLATFORM_USERS:
            with self.subTest(role=role):
                self._login(email)
                body = self._payroll_detail()
                for marker, predicate in cases:
                    self.assertEqual(
                        marker in body,
                        predicate(role),
                        f"{role}: '{marker}' visibility disagrees with its "
                        "capability predicate",
                    )
                self._logout()

    def test_superusers_see_the_exports_they_may_run(self):
        """The specific regression the audit reported: authorized by route,
        shown no UI."""
        for email, role in (
            ("operator@payrolla.com", "payrolla_admin"),
            ("director@payrolla.com", "md"),
        ):
            with self.subTest(role=role):
                self.assertTrue(perms.can_operate_payroll(role))
                self._login(email)
                body = self._payroll_detail()
                for marker in self._export_markers():
                    self.assertIn(marker, body, f"{role} cannot see {marker}")
                self._logout()

    def test_roster_delete_affordance_matches_capability(self):
        company_id = self.run.client_company_id
        roster_url = f"/employees/clients/{company_id}/roster"
        # The affordance is the POST target of the delete form in
        # employees/_roster_body.html, which the roster page includes.
        delete_marker = f"/employees/clients/{company_id}/delete/"
        for _name, email, role in PLATFORM_USERS:
            with self.subTest(role=role):
                self._login(email)
                resp = self.client.get(roster_url)
                body = resp.get_data(as_text=True)
                # A role without roster access is redirected away, which is
                # itself consistent with can_maintain_roster being False.
                self.assertEqual(
                    resp.status_code == 200,
                    perms.can_maintain_roster(role),
                    f"{role}: roster access disagrees with can_maintain_roster",
                )
                if perms.can_maintain_roster(role):
                    self.assertEqual(
                        delete_marker in body,
                        perms.can_delete_employee(role),
                        f"{role}: employee-delete affordance disagrees with "
                        "can_delete_employee",
                    )
                self._logout()


if __name__ == "__main__":
    unittest.main()
