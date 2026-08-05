"""Phase 2 — payroll lifecycle progress (presentation).

Pure, status-derived stepper + status-badge helpers (app/payroll_status.py),
exposed as Jinja globals and rendered on the operator runs list. No business
rule lives here — these only visualise the existing status/derived signals.
"""

import os
import types
import unittest

os.environ["SKIP_DOTENV"] = "true"
os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["SEED_DEMO_DATA"] = "true"
os.environ["PERSISTENCE_REQUIRED"] = "false"

from app.payroll_status import (  # noqa: E402
    lifecycle_steps,
    run_progress,
    status_badge_class,
)


def _state(steps, key):
    return next(s["state"] for s in steps if s["key"] == key)


class LifecycleStepsTestCase(unittest.TestCase):
    def test_draft_uncalculated(self):
        steps = lifecycle_steps("Draft", calculated=False)
        self.assertEqual(_state(steps, "draft"), "current")
        self.assertEqual(_state(steps, "calculated"), "upcoming")
        self.assertEqual(_state(steps, "held"), "skipped")  # never held
        self.assertEqual(_state(steps, "processed"), "upcoming")

    def test_draft_calculated_advances(self):
        steps = lifecycle_steps("Draft", calculated=True)
        self.assertEqual(_state(steps, "draft"), "done")
        self.assertEqual(_state(steps, "calculated"), "current")

    def test_approved_marks_prior_done(self):
        steps = lifecycle_steps("Approved", calculated=True)
        for key in ("draft", "calculated", "submitted"):
            self.assertEqual(_state(steps, key), "done", key)
        self.assertEqual(_state(steps, "approved"), "current")
        self.assertEqual(_state(steps, "processed"), "upcoming")
        self.assertEqual(_state(steps, "held"), "skipped")

    def test_held_branch_is_shown_when_held(self):
        steps = lifecycle_steps("Held", held=True)
        self.assertEqual(_state(steps, "held"), "current")
        self.assertEqual(_state(steps, "submitted"), "done")

    def test_processed_distributed_is_complete(self):
        steps = lifecycle_steps("Processed", distributed=True)
        self.assertEqual(_state(steps, "processed"), "done")
        self.assertEqual(_state(steps, "distributed"), "done")
        self.assertNotIn("current", [s["state"] for s in steps])

    def test_processed_not_yet_distributed(self):
        steps = lifecycle_steps("Processed", distributed=False)
        self.assertEqual(_state(steps, "processed"), "current")
        self.assertEqual(_state(steps, "distributed"), "upcoming")

    def test_rejected_is_terminal(self):
        steps = lifecycle_steps("Rejected")
        self.assertEqual(_state(steps, "submitted"), "done")
        self.assertEqual(_state(steps, "approved"), "skipped")
        self.assertNotIn("current", [s["state"] for s in steps])

    def test_badge_classes(self):
        self.assertEqual(status_badge_class("Approved"), "text-bg-success")
        self.assertEqual(status_badge_class("Rejected"), "text-bg-danger")
        self.assertEqual(status_badge_class("Held"), "text-bg-warning")
        self.assertEqual(status_badge_class("Nonsense"), "text-bg-secondary")

    def test_run_progress_derives_flags(self):
        run = types.SimpleNamespace(status="Held", total_workers=5, risk_status="held")
        steps = run_progress(run)
        self.assertEqual(_state(steps, "held"), "current")
        # a run with workers has been calculated
        self.assertEqual(_state(steps, "calculated"), "done")
        # not held -> held step skipped
        run2 = types.SimpleNamespace(status="Approved", total_workers=5, risk_status="accepted")
        self.assertEqual(_state(run_progress(run2), "held"), "skipped")


class RunsListRendersStatusCellTestCase(unittest.TestCase):
    """The runs list states where a run stands, in words.

    It used to render a status pill with `progress_stepper(compact=true)` under
    it — seven 9px dots with their labels switched off by `font-size: 0`,
    because a ~110px column had no room for them. Reading it meant knowing the
    stage order by heart and then counting, and it never answered the question a
    queue is scanned for: what happens next. macros/lifecycle.html::status_cell
    replaces it with the status, the position, and the next stage named."""

    def setUp(self):
        from app import create_app

        self.app = create_app()
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()
        self.ctx = self.app.app_context()
        self.ctx.push()

    def tearDown(self):
        self.ctx.pop()

    def test_operator_runs_list_states_position_and_next_stage(self):
        self.assertEqual(
            self.client.post(
                "/login", data={"email": "admin@payrolla.com", "password": "password123"}
            ).status_code,
            302,
        )
        page = self.client.get("/payroll/runs")
        self.assertEqual(page.status_code, 200)
        html = page.get_data(as_text=True)
        # seed ships at least one payroll run, so the cell must appear
        self.assertIn('class="rs"', html)
        self.assertRegex(html, r"Step \d+ of \d+")
        # The unlabelled dot stepper is gone from the LIST. It still serves the
        # run detail page, where it has the width to draw its labels.
        self.assertNotIn("lifecycle-stepper--compact", html)

    def test_the_cell_counts_only_the_stages_that_apply_to_the_run(self):
        """A run that was never held is on a six-step path, not a seven-step one
        with a greyed-out stage — telling its reader otherwise invents a stage
        it will never reach."""
        from app.payroll_status import status_progress

        never_held = types.SimpleNamespace(
            status="Approved", total_workers=5, risk_status="accepted"
        )
        progress = status_progress(run_progress(never_held))
        self.assertEqual(progress["total"], 6)
        self.assertEqual(progress["position"], 4)
        self.assertEqual(progress["current"], "Approved")
        self.assertEqual(progress["next"], "Processed")

    def test_a_finished_run_has_no_next_stage(self):
        from app.payroll_status import status_progress

        done = types.SimpleNamespace(
            status="Processed", total_workers=5, risk_status="accepted"
        )
        progress = status_progress(run_progress(done, distributed=True))
        self.assertIsNone(progress["next"])
        self.assertEqual(progress["position"], progress["total"])


if __name__ == "__main__":
    unittest.main()
