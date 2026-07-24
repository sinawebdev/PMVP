"""Centralised payroll-run lifecycle authorization.

The scattered ``current_user.role in [...] and payroll_run.status in [...]``
expressions in payroll_detail.html (and the ad-hoc ``@role_required(...)`` tuples
in payroll.py) were replaced by the ``can_*_run`` predicates in
app/permissions.py. These tests pin:

  1. The predicate truth tables (role x run-status) — the single source of truth.
  2. That the predicate role halves exactly preserve the pre-refactor template
     role lists for the legacy operator roles.
  3. That ``chrisnat_admin`` — which already reached every one of these routes via
     the role_required superuser bypass — now also SEES the corresponding buttons
     (the deliberate "full operator access" completion, confirmed with Sina).
  4. The rendered payroll_detail buttons match the predicates end-to-end.

Runs on in-memory SQLite (never the production Supabase DB).
"""

import os
import unittest

os.environ["SKIP_DOTENV"] = "true"
os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["SEED_DEMO_DATA"] = "true"
os.environ["PERSISTENCE_REQUIRED"] = "false"

from types import SimpleNamespace  # noqa: E402

from app import create_app, db, permissions  # noqa: E402
from app.models import ClientCompany, PayrollRun  # noqa: E402
from app.payroll_status import (  # noqa: E402
    APPROVED,
    DRAFT,
    PENDING_APPROVAL,
    PROCESSED,
    REJECTED,
)

PREVIEWED = "Previewed"
ALL_STATUSES = (DRAFT, PENDING_APPROVAL, APPROVED, PROCESSED, REJECTED, PREVIEWED)

# Every role that appears anywhere in the operator plane, plus a deliberately
# unprivileged one (operations_supervisor is in none of the lifecycle groups).
ALL_ROLES = (
    "admin",
    "md",
    "payroll_officer",
    "accounts_officer",
    "chrisnat_admin",
    "operations_supervisor",
)


def _run(status):
    return SimpleNamespace(status=status)


class PredicateTruthTableTestCase(unittest.TestCase):
    """The predicates are literal (role, status) truth tables — assert the
    reference expression directly so any drift is caught."""

    # Reference role sets = pre-refactor template lists + chrisnat_admin. If the
    # policy ever changes, THIS is the line that must change with it.
    CALC = {"admin", "chrisnat_admin"}
    EDIT = {"admin", "payroll_officer", "chrisnat_admin"}
    SUBMIT = {"admin", "accounts_officer", "chrisnat_admin"}
    APPROVE = {"admin", "md", "chrisnat_admin"}
    PROCESS = {"admin", "accounts_officer", "md", "chrisnat_admin"}
    DELETE = {"admin", "md", "chrisnat_admin"}
    # Distribution reuses the canonical PAYROLL_ROLES operator group (the four
    # legacy payroll roles + chrisnat_admin) — the same set the /distribution
    # routes gate on — rather than a bespoke lifecycle group.
    DISTRIBUTE = {"admin", "md", "payroll_officer", "accounts_officer", "chrisnat_admin"}

    PENDING = {DRAFT, PENDING_APPROVAL}
    DELETABLE = {DRAFT, PREVIEWED, REJECTED}
    # Payslip distribution gate: a finalized run (Approved or Processed).
    SENDABLE = {APPROVED, PROCESSED}

    def test_can_calculate_run(self):
        for role in ALL_ROLES:
            for status in ALL_STATUSES:
                with self.subTest(role=role, status=status):
                    self.assertEqual(
                        permissions.can_calculate_run(role, _run(status)),
                        role in self.CALC and status in self.PENDING,
                    )

    def test_can_edit_run_figures_is_role_only(self):
        # Status-independent: the grid is viewable read-only at any status.
        for role in ALL_ROLES:
            with self.subTest(role=role):
                self.assertEqual(
                    permissions.can_edit_run_figures(role), role in self.EDIT
                )

    def test_can_submit_run_for_approval(self):
        for role in ALL_ROLES:
            for status in ALL_STATUSES:
                with self.subTest(role=role, status=status):
                    self.assertEqual(
                        permissions.can_submit_run_for_approval(role, _run(status)),
                        role in self.SUBMIT and status == DRAFT,
                    )

    def test_can_approve_and_reject_run(self):
        for role in ALL_ROLES:
            for status in ALL_STATUSES:
                expected = role in self.APPROVE and status in self.PENDING
                with self.subTest(role=role, status=status):
                    self.assertEqual(
                        permissions.can_approve_run(role, _run(status)), expected
                    )
                    # reject shares the approval group + pending-status gate.
                    self.assertEqual(
                        permissions.can_reject_run(role, _run(status)), expected
                    )

    def test_can_mark_run_processed(self):
        for role in ALL_ROLES:
            for status in ALL_STATUSES:
                with self.subTest(role=role, status=status):
                    self.assertEqual(
                        permissions.can_mark_run_processed(role, _run(status)),
                        role in self.PROCESS and status == APPROVED,
                    )

    def test_can_distribute_run(self):
        # The fix: distribution is allowed for a finalized run — Approved OR
        # Processed. "Paid" was renamed to Processed, and a Processed run must
        # stay distributable (the bug hid the button once a run was Processed).
        for role in ALL_ROLES:
            for status in ALL_STATUSES:
                with self.subTest(role=role, status=status):
                    self.assertEqual(
                        permissions.can_distribute_run(role, _run(status)),
                        role in self.DISTRIBUTE and status in self.SENDABLE,
                    )

    def test_can_delete_run(self):
        for role in ALL_ROLES:
            for status in ALL_STATUSES:
                with self.subTest(role=role, status=status):
                    self.assertEqual(
                        permissions.can_delete_run(role, _run(status)),
                        role in self.DELETE and status in self.DELETABLE,
                    )

    def test_role_normalisation(self):
        # Predicates normalise the role string (case/whitespace), like the rest
        # of app/permissions.py.
        self.assertTrue(permissions.can_approve_run("  ADMIN ", _run(DRAFT)))
        self.assertTrue(permissions.can_delete_run("Chrisnat_Admin", _run(REJECTED)))

    def test_chrisnat_admin_matches_admin_on_every_predicate(self):
        # The deliberate inclusion: chrisnat_admin is granted the same lifecycle
        # visibility as admin (it already passed every route via the superuser
        # bypass).
        for status in ALL_STATUSES:
            run = _run(status)
            with self.subTest(status=status):
                self.assertEqual(
                    permissions.can_calculate_run("chrisnat_admin", run),
                    permissions.can_calculate_run("admin", run),
                )
                self.assertEqual(
                    permissions.can_approve_run("chrisnat_admin", run),
                    permissions.can_approve_run("admin", run),
                )
                self.assertEqual(
                    permissions.can_reject_run("chrisnat_admin", run),
                    permissions.can_reject_run("admin", run),
                )
                self.assertEqual(
                    permissions.can_delete_run("chrisnat_admin", run),
                    permissions.can_delete_run("admin", run),
                )
                self.assertEqual(
                    permissions.can_distribute_run("chrisnat_admin", run),
                    permissions.can_distribute_run("admin", run),
                )


class RenderedButtonVisibilityTestCase(unittest.TestCase):
    """The predicates drive the actual payroll_detail buttons end-to-end."""

    def setUp(self):
        self.app = create_app()
        self.app.config["TESTING"] = True
        self.http = self.app.test_client()
        with self.app.app_context():
            self.client_id = ClientCompany.query.first().id

    def _login(self, email):
        resp = self.http.post(
            "/login", data={"email": email, "password": "password123"}
        )
        self.assertIn(resp.status_code, (200, 302))

    def _make_run(self, status):
        with self.app.app_context():
            run = PayrollRun(
                client_company_id=self.client_id,
                month="August",
                year=2099,
                status=status,
            )
            db.session.add(run)
            db.session.commit()
            return run.id

    def _buttons(self, run_id):
        """Set of lifecycle actions whose form/link is present on the detail page."""
        resp = self.http.get(f"/payroll/runs/{run_id}")
        self.assertEqual(resp.status_code, 200)
        body = resp.get_data(as_text=True)
        actions = {
            "calculate": f"/runs/{run_id}/calculate",
            "edit": f"/runs/{run_id}/items/edit",
            "submit": f"/runs/{run_id}/submit-for-approval",
            "approve": f"/runs/{run_id}/approve",
            "reject": f"/runs/{run_id}/reject",
            "mark_paid": f"/runs/{run_id}/mark-paid",
            "distribute": f"/distribution/run/{run_id}",
            "delete": f"/runs/{run_id}/delete",
        }
        return {name for name, frag in actions.items() if frag in body}

    def test_admin_draft_run_shows_full_open_toolset(self):
        self._login("admin@chrisnat.local")
        run_id = self._make_run(DRAFT)
        self.assertEqual(
            self._buttons(run_id),
            {"calculate", "edit", "submit", "approve", "reject", "delete"},
        )

    def test_admin_pending_run_hides_submit_and_delete(self):
        self._login("admin@chrisnat.local")
        run_id = self._make_run(PENDING_APPROVAL)
        buttons = self._buttons(run_id)
        self.assertEqual(buttons, {"calculate", "edit", "approve", "reject"})
        self.assertNotIn("submit", buttons)  # Draft-only
        self.assertNotIn("delete", buttons)  # not a deletable status

    def test_admin_approved_run_shows_mark_processed_edit_and_distribute(self):
        self._login("admin@chrisnat.local")
        run_id = self._make_run(APPROVED)
        buttons = self._buttons(run_id)
        # Approved is a SENDABLE status, so Distribute Payslips appears alongside
        # Mark Processed and Edit Figures.
        self.assertEqual(buttons, {"edit", "mark_paid", "distribute"})
        for gone in ("approve", "reject", "calculate", "submit", "delete"):
            self.assertNotIn(gone, buttons)

    def test_admin_processed_run_shows_distribute_and_edit(self):
        # The bug fix: a Processed (terminal) run still offers Distribute
        # Payslips. It used to disappear because the gate checked the dead
        # "Paid" status instead of "Processed".
        self._login("admin@chrisnat.local")
        run_id = self._make_run(PROCESSED)
        buttons = self._buttons(run_id)
        self.assertEqual(buttons, {"edit", "distribute"})
        for gone in ("approve", "reject", "calculate", "submit", "mark_paid", "delete"):
            self.assertNotIn(gone, buttons)

    def test_distribute_button_visible_only_for_sendable_statuses(self):
        # Distribution is offered for finalized runs (Approved, Processed) and
        # for no other lifecycle state.
        self._login("admin@chrisnat.local")
        for status in (APPROVED, PROCESSED):
            with self.subTest(status=status):
                self.assertIn("distribute", self._buttons(self._make_run(status)))
        for status in (DRAFT, PENDING_APPROVAL, REJECTED, PREVIEWED):
            with self.subTest(status=status):
                self.assertNotIn("distribute", self._buttons(self._make_run(status)))

    def test_payroll_officer_only_sees_edit_figures(self):
        self._login("payroll@chrisnat.local")
        run_id = self._make_run(DRAFT)
        self.assertEqual(self._buttons(run_id), {"edit"})

    def test_accounts_officer_can_submit_not_approve(self):
        self._login("accounts@chrisnat.local")
        run_id = self._make_run(DRAFT)
        buttons = self._buttons(run_id)
        self.assertIn("submit", buttons)
        self.assertNotIn("approve", buttons)
        self.assertNotIn("delete", buttons)

    def test_md_approves_and_deletes_but_cannot_submit(self):
        self._login("md@chrisnat.local")
        run_id = self._make_run(DRAFT)
        buttons = self._buttons(run_id)
        self.assertIn("approve", buttons)
        self.assertIn("reject", buttons)
        self.assertIn("delete", buttons)
        self.assertNotIn("submit", buttons)  # md not in the submit group
        self.assertNotIn("edit", buttons)    # md not in the edit-figures group

    def test_chrisnat_admin_now_sees_lifecycle_buttons(self):
        # The behaviour change this refactor intentionally makes: chrisnat_admin
        # could already POST these routes; it now also sees the buttons, matching
        # admin exactly.
        self._login("chrisnat.admin@chrisnat.local")
        draft = self._make_run(DRAFT)
        self.assertEqual(
            self._buttons(draft),
            {"calculate", "edit", "submit", "approve", "reject", "delete"},
        )
        approved = self._make_run(APPROVED)
        self.assertEqual(self._buttons(approved), {"edit", "mark_paid", "distribute"})


class DistributionRouteEnforcementTestCase(unittest.TestCase):
    """The /distribution routes enforce the SAME status rule the button does —
    SENDABLE_STATUSES (Approved or Processed) — so the backend and the UI can
    never drift. Role is gated by ``@role_required(*PAYROLL_ROLES)`` (covered by
    the predicate truth table above)."""

    def setUp(self):
        self.app = create_app()
        self.app.config["TESTING"] = True
        self.http = self.app.test_client()
        with self.app.app_context():
            self.client_id = ClientCompany.query.first().id
        self.http.post(
            "/login", data={"email": "admin@chrisnat.local", "password": "password123"}
        )

    def _make_run(self, status):
        with self.app.app_context():
            run = PayrollRun(
                client_company_id=self.client_id,
                month="August",
                year=2099,
                status=status,
            )
            db.session.add(run)
            db.session.commit()
            return run.id

    def test_processed_run_page_is_sendable(self):
        # The Send controls render only when the run is sendable — Processed must
        # qualify (mirrors the fixed button).
        run_id = self._make_run(PROCESSED)
        resp = self.http.get(f"/distribution/run/{run_id}")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Send now", resp.get_data(as_text=True))

    def test_approved_run_page_is_sendable(self):
        run_id = self._make_run(APPROVED)
        resp = self.http.get(f"/distribution/run/{run_id}")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Send now", resp.get_data(as_text=True))

    def test_draft_run_page_is_not_sendable(self):
        run_id = self._make_run(DRAFT)
        resp = self.http.get(f"/distribution/run/{run_id}")
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn("Send now", resp.get_data(as_text=True))

    def test_send_on_draft_run_is_blocked(self):
        # The write path enforces the status rule too: a non-sendable run's send
        # is rejected before any delivery is attempted.
        run_id = self._make_run(DRAFT)
        resp = self.http.post(
            f"/distribution/run/{run_id}/send", data={}, follow_redirects=True
        )
        self.assertEqual(resp.status_code, 200)
        self.assertIn(
            "Payslips can only be distributed after the payroll run is approved.",
            resp.get_data(as_text=True),
        )

    def test_status_fragment_renders_delivery_table(self):
        # Phase 3, Slice 2 — the htmx-polled fragment renders the same delivery
        # table content as the full page, so a poll swap is indistinguishable
        # from a full reload.
        run_id = self._make_run(APPROVED)
        resp = self.http.get(f"/distribution/run/{run_id}/status-fragment")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Delivery Status", resp.get_data(as_text=True))

    def test_send_payslips_hidden_while_a_batch_is_in_flight(self):
        # Once a distribution is queued for a run, the send/resend controls
        # disappear (an "in progress" badge takes their place) so an operator
        # can't pile up a second batch on top of one still running.
        from app.distribution.queue import enqueue_distribution
        from app.models import CHANNEL_AUTO, User

        run_id = self._make_run(APPROVED)
        with self.app.app_context():
            run = db.session.get(PayrollRun, run_id)
            operator = User.query.filter_by(email="admin@chrisnat.local").first()
            enqueue_distribution(run, CHANNEL_AUTO, False, operator)

        resp = self.http.get(f"/distribution/run/{run_id}")
        self.assertEqual(resp.status_code, 200)
        body = resp.get_data(as_text=True)
        self.assertNotIn("Send now", body)
        self.assertIn("Distribution in progress", body)

        # A second send while one is in flight doesn't queue a duplicate batch.
        self.http.post(f"/distribution/run/{run_id}/send", data={"channel": "auto"})
        with self.app.app_context():
            from app.models import DistributionBatch

            self.assertEqual(
                DistributionBatch.query.filter_by(payroll_run_id=run_id).count(), 1
            )


if __name__ == "__main__":
    unittest.main()
