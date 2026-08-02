"""Phase 2, Task 2.4 — the backend can name the ONE next step for a run.

The existing status groups (PENDING/SENDABLE/DELETABLE) only answer "is this
allowed", which is why a Draft run legitimately offers Calculate, Submit,
Approve, Reject and Delete simultaneously. recommended_action_for() answers the
other question — what actually moves this run forward — and every "one
recommended action" surface in later phases reads it.

Also pins the approval gate: an empty run is not approvable by anybody.
"""

import unittest

from app.payroll_status import (
    ACTION_APPROVE,
    ACTION_AWAIT_RISK,
    ACTION_CALCULATE,
    ACTION_DISTRIBUTE,
    ACTION_FIX_IMPORT,
    ACTION_MARK_PROCESSED,
    ACTION_RESOLVE_REJECTION,
    ACTION_REVIEW_HOLD,
    ACTION_SUBMIT,
    APPROVED,
    AUTO_ACCEPTED,
    DRAFT,
    HELD,
    PENDING_APPROVAL,
    PROCESSED,
    REJECTED,
    SUBMITTED,
    recommended_action_for,
)
from app.permissions import APPROVAL_ROLES, can_approve_run


class _Run:
    """Stand-in carrying only the columns the predicate reads."""

    def __init__(self, status, workers=5, net=1000.0, risk_status=None):
        self.status = status
        self.total_workers = workers
        self.total_net_pay = net
        self.risk_status = risk_status


class RecommendedActionTests(unittest.TestCase):
    def _key(self, run, **kw):
        action = recommended_action_for(run, **kw)
        return action["key"] if action else None

    def test_every_lifecycle_state_has_an_answer(self):
        """No status may fall through to an accidental None — a screen with no
        recommended action and no reason is exactly the ambiguity being removed."""
        terminal = {(PROCESSED, True)}
        for status in (
            DRAFT,
            SUBMITTED,
            HELD,
            AUTO_ACCEPTED,
            PENDING_APPROVAL,
            APPROVED,
            PROCESSED,
            REJECTED,
        ):
            for distributed in (False, True):
                with self.subTest(status=status, distributed=distributed):
                    action = recommended_action_for(_Run(status), distributed=distributed)
                    if (status, distributed) in terminal:
                        self.assertIsNone(action)
                    else:
                        self.assertIsNotNone(action)
                        self.assertTrue(action["label"])
                        self.assertTrue(action["why"])

    def test_draft_progression(self):
        self.assertEqual(
            self._key(_Run(DRAFT, workers=0, net=0)), ACTION_FIX_IMPORT
        )
        self.assertEqual(self._key(_Run(DRAFT, workers=5, net=0)), ACTION_CALCULATE)
        self.assertEqual(self._key(_Run(DRAFT, workers=5, net=1000)), ACTION_SUBMIT)

    def test_review_and_approval_states(self):
        self.assertEqual(self._key(_Run(SUBMITTED)), ACTION_AWAIT_RISK)
        self.assertEqual(self._key(_Run(HELD)), ACTION_REVIEW_HOLD)
        self.assertEqual(self._key(_Run(AUTO_ACCEPTED)), ACTION_APPROVE)
        self.assertEqual(self._key(_Run(PENDING_APPROVAL)), ACTION_APPROVE)

    def test_closing_and_distribution(self):
        self.assertEqual(self._key(_Run(APPROVED)), ACTION_MARK_PROCESSED)
        self.assertEqual(self._key(_Run(PROCESSED)), ACTION_DISTRIBUTE)
        self.assertIsNone(self._key(_Run(PROCESSED), distributed=True))

    def test_rejected_run_points_at_resolution(self):
        self.assertEqual(self._key(_Run(REJECTED)), ACTION_RESOLVE_REJECTION)

    def test_exactly_one_action_is_returned(self):
        """The whole point: one next step, not a menu."""
        action = recommended_action_for(_Run(PENDING_APPROVAL))
        self.assertIsInstance(action, dict)
        self.assertEqual(set(action), {"key", "label", "why"})

    def test_it_is_role_blind(self):
        """The recommendation describes the run, not the viewer — permission
        filtering is the caller's job, kept deliberately separate."""
        import inspect

        params = inspect.signature(recommended_action_for).parameters
        self.assertNotIn("role", params)


class EmptyRunIsNotApprovableTests(unittest.TestCase):
    def test_zero_worker_run_is_not_approvable_by_any_role(self):
        empty = _Run(PENDING_APPROVAL, workers=0, net=0)
        for role in APPROVAL_ROLES:
            with self.subTest(role=role):
                self.assertFalse(can_approve_run(role, empty))

    def test_populated_run_is_still_approvable(self):
        populated = _Run(PENDING_APPROVAL, workers=12, net=5000)
        for role in APPROVAL_ROLES:
            with self.subTest(role=role):
                self.assertTrue(can_approve_run(role, populated))

    def test_none_worker_count_is_treated_as_empty(self):
        self.assertFalse(can_approve_run("admin", _Run(DRAFT, workers=None, net=0)))


if __name__ == "__main__":
    unittest.main()
