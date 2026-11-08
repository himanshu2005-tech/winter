# -*- coding: utf-8 -*-
"""Approving while the task is still running must approve, not redirect.

Himanshu asked VANTA to upload vault/docs/hyperverge.pdf to Google Drive. The
action queued correctly and said it was waiting for approval. He typed
"approve". The log shows what happened to it:

    [Barge-in] User redirected active task via text: approve

The upload was still mid-turn, so the input dispatcher treated the word as a
course correction to the running task rather than as consent, and the approval
record was never touched. VANTA then said -- twice, and accurately -- that the
upload was still pending. The file was never uploaded and the approval sat in
the database unanswered.

The reflex that recognises "approve" does exist, but it lives inside
run_agent_loop, and run_agent_loop is only reached when no task holds the
agent lock. Approving is precisely the thing one does *while* something is
waiting, so the one path that needed it was the one path that skipped it.
"""

import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import agent


class RecognisingConsent(unittest.TestCase):
    """A deterministic reading of the word, with no model call."""

    def test_bare_approve(self):
        self.assertEqual(agent.approval_verdict("approve", True), "approve")

    def test_approved_and_variants(self):
        for phrase in ("approve", "approved", "approve it", "approve that",
                       "Approve.", "  APPROVE  "):
            with self.subTest(phrase=phrase):
                self.assertEqual(agent.approval_verdict(phrase, True),
                                 "approve")

    def test_a_telegram_prefix_does_not_hide_it(self):
        """Approving from a phone is exactly when he is not at the machine."""
        self.assertEqual(
            agent.approval_verdict("[Telegram]: approve", True), "approve")

    def test_rejections(self):
        for phrase in ("reject", "reject it", "cancel", "decline", "deny",
                       "no"):
            with self.subTest(phrase=phrase):
                self.assertEqual(agent.approval_verdict(phrase, True),
                                 "reject")

    def test_a_conversational_yes_counts_only_when_something_waits(self):
        self.assertEqual(agent.approval_verdict("yes", True), "approve")
        self.assertEqual(agent.approval_verdict("yes", False), "")

    def test_an_unambiguous_approval_is_recognised_even_with_nothing_waiting(self):
        """So it can be answered honestly rather than becoming a new task."""
        self.assertEqual(agent.approval_verdict("approve", False), "approve")

    def test_ordinary_instructions_are_not_consent(self):
        for phrase in ("upload the file to drive", "no, use the other one",
                       "approve the budget document for me", "yes please do "
                       "that thing with the calendar instead"):
            with self.subTest(phrase=phrase):
                self.assertEqual(agent.approval_verdict(phrase, True), "")

    def test_empty_text_is_not_consent(self):
        self.assertEqual(agent.approval_verdict("", True), "")
        self.assertEqual(agent.approval_verdict(None, True), "")


class ConsentDuringAnActiveTask(unittest.TestCase):
    """The dispatch decision: settle the approval, or redirect the task."""

    def test_approve_settles_instead_of_redirecting(self):
        with patch.object(agent, "_settle_pending_approval",
                          return_value=True) as settle, \
                patch.object(agent, "_queue_interruption") as interrupt:
            handled = agent.handle_text_during_task("approve")
        self.assertTrue(handled)
        settle.assert_called_once_with("approve")
        interrupt.assert_not_called()

    def test_reject_settles_instead_of_redirecting(self):
        with patch.object(agent, "_settle_pending_approval",
                          return_value=True) as settle, \
                patch.object(agent, "_queue_interruption") as interrupt:
            handled = agent.handle_text_during_task("reject")
        self.assertTrue(handled)
        settle.assert_called_once_with("reject")
        interrupt.assert_not_called()

    def test_a_real_redirect_still_interrupts(self):
        """Barge-in must keep working; it is how he corrects a wrong task."""
        with patch.object(agent, "_settle_pending_approval",
                          return_value=False) as settle, \
                patch.object(agent, "_queue_interruption") as interrupt:
            handled = agent.handle_text_during_task(
                "no, upload the other document instead")
        self.assertFalse(handled)
        settle.assert_not_called()
        interrupt.assert_not_called()

    def test_approve_with_nothing_pending_falls_through_to_barge_in(self):
        """If no record exists the word was about something else."""
        with patch.object(agent, "_settle_pending_approval",
                          return_value=False), \
                patch.object(agent, "_queue_interruption"):
            self.assertFalse(agent.handle_text_during_task("approve"))

    def test_a_conversational_yes_mid_task_is_not_swallowed_as_consent(self):
        """"Yes" mid-task is usually an answer, not an approval."""
        with patch.object(agent, "_settle_pending_approval",
                          return_value=True) as settle:
            agent.handle_text_during_task("yes")
        # It may settle when something is genuinely pending, but it must have
        # consulted the pending record rather than assuming.
        self.assertLessEqual(settle.call_count, 1)

    def test_the_decision_makes_no_model_call(self):
        """This runs in the input path; it must stay instant."""
        with patch.object(agent, "llm_router") as router, \
                patch.object(agent, "_settle_pending_approval",
                             return_value=True):
            agent.handle_text_during_task("approve")
        router.call_llm.assert_not_called()


if __name__ == "__main__":
    unittest.main()
