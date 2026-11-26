# -*- coding: utf-8 -*-
"""
What may be enforced, and what an unrecognised request must not become.

A real turn: "I have to go to Chennai on Sunday at 5 AM, my college has an
outpass rule where I should apply for a gate pass 24 hours prior, so manage
it." The planner handled it well and the model did the work -- two calendar
reminders created, both verified by a LIST_CALENDAR_EVENTS receipt. Then the
completion gate refused to let the turn finish, and it burned six more steps
calling WAIT before giving up and telling Himanshu the outcome could not be
completed.

Three faults stacked:

  1. The compiler did not recognise "manage it" and produced one GENERIC
     outcome at confidence 0.4 -- which means "I could not classify this".
  2. Its retrieval query was therefore the literal word "generic", so the
     candidates were noise: WAIT, LEARN_KNOWLEDGE, TRENDING_NEWS, MUSIC_PREVIOUS
     and SHUTDOWN.
  3. Selection bound the outcome to WAIT, the first of those, and the
     completion gate then required WAIT to succeed -- while the two calendar
     events that actually satisfied the request were credited to nothing,
     because neither was the selected action.

So the turn reported failure for work it had done, which is the precise bug
this machinery exists to prevent, reintroduced in a new place. An outcome the
compiler could not classify must not be enforced, must not be bound to
whatever ranking happened to return first, and real completed work must count.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import execution_reliability as reliability
import outcome_compiler
import outcome_planner
import outcomes

#: Genuinely unclassifiable. The original fixture here was the Chennai
#: gate-pass sentence, which now compiles to WEB_WORKFLOW -- the vocabulary
#: for portals and passes was added precisely so it would. The property under
#: test is unchanged: a request VANTA cannot name must not be enforced. It
#: just needs a sentence that is still unnameable.
VAGUE = "sort out the thing from yesterday with the other stuff"


def _noise(query, limit=8):
    """What retrieval really returned for the query "generic"."""
    return ["WAIT", "LEARN_KNOWLEDGE", "TRENDING_NEWS", "MUSIC_PREVIOUS",
            "LIST_ROUTINES", "SHUTDOWN"]


def _reminder_channels(query, limit=8):
    return ["SET_ALARM", "ADD_TASK", "ADD_CALENDAR_EVENT"]


def runtime_for(utterance, retrieve):
    compiled = outcome_compiler.compile(utterance)
    bound = outcome_planner.bind(compiled, retrieve=retrieve)
    chosen = outcome_planner.select(bound, utterance, [])
    return compiled, chosen, reliability.OutcomeRuntime(compiled, chosen)


class AnUnclassifiedRequestIsNotEnforceable(unittest.TestCase):
    def test_a_vague_request_compiles_to_a_generic_outcome(self):
        compiled = outcome_compiler.compile(VAGUE)
        self.assertEqual(compiled.outcomes[0].operation, outcomes.GENERIC)

    def test_a_generic_outcome_is_not_selected(self):
        """Binding noise to an outcome is worse than binding nothing."""
        _c, chosen, _r = runtime_for(VAGUE, _noise)
        self.assertEqual([item.selected_action for item in chosen], [])

    def test_a_generic_outcome_does_not_block_completion(self):
        _c, _s, runtime = runtime_for(VAGUE, _noise)
        self.assertEqual(runtime.completion_problem(), "")

    def test_a_turn_with_only_generic_outcomes_can_finish(self):
        _c, _s, runtime = runtime_for(VAGUE, _noise)
        self.assertTrue(runtime.turn_terminal())

    def test_wait_is_never_selected_as_an_implementation(self):
        for meta in ("WAIT", "WAIT_FOR", "DISCOVER_ACTIONS"):
            with self.subTest(action=meta):
                self.assertIn(meta, outcome_planner.NEVER_SELECTED)

    def test_shutdown_is_not_bound_from_a_meaningless_query(self):
        """It was a candidate. Nothing should be selectable from noise."""
        _c, chosen, _r = runtime_for(VAGUE, _noise)
        self.assertNotIn("SHUTDOWN",
                         [item.selected_action for item in chosen])

    def test_a_generic_query_is_not_the_literal_word_generic(self):
        compiled = outcome_compiler.compile(VAGUE)
        self.assertNotEqual(list(compiled.retrieval_queries()), ["generic"])

    def test_a_generic_query_still_withholds_content(self):
        compiled = outcome_compiler.compile(
            "sort out the thing and remind me to call madhu about the backups")
        joined = " ".join(compiled.retrieval_queries()).lower()
        self.assertNotIn("call madhu about the backups", joined)


class RealWorkCounts(unittest.TestCase):
    """Two calendar events succeeded and were credited to nothing."""

    def test_an_alternative_that_succeeded_satisfies_the_outcome(self):
        utterance = "remind me on saturday at 5am to apply for the gate pass"
        _c, _s, runtime = runtime_for(utterance, _reminder_channels)
        # Whatever was selected, a different channel in the same group doing
        # the job means the outcome is met.
        runtime.apply_result({"type": "ADD_CALENDAR_EVENT", "ok": True,
                              "output": "[Success] Event created: ..."})
        self.assertTrue(runtime.task_successful(), runtime.ledger.summary())
        self.assertEqual(runtime.completion_problem(), "")

    def test_crediting_records_which_action_satisfied_it(self):
        utterance = "remind me at 4:30 to call madhu"
        _c, _s, runtime = runtime_for(utterance, _reminder_channels)
        runtime.apply_result({"type": "ADD_TASK", "ok": True,
                              "output": "[Success] task added"})
        receipts = " ".join(runtime.ledger.get("o1").receipts)
        self.assertIn("task added", receipts)

    def test_an_unrelated_success_does_not_satisfy_an_outcome(self):
        """Crediting must not become "anything that worked counts"."""
        utterance = "remind me at 4:30 to call madhu"
        _c, _s, runtime = runtime_for(utterance, _reminder_channels)
        runtime.apply_result({"type": "TRENDING_NEWS", "ok": True,
                              "output": "[Success] 5 headlines"})
        self.assertFalse(runtime.task_successful())

    def test_a_failed_alternative_does_not_satisfy_the_outcome(self):
        utterance = "remind me at 4:30 to call madhu"
        _c, _s, runtime = runtime_for(utterance, _reminder_channels)
        runtime.apply_result({"type": "ADD_CALENDAR_EVENT", "ok": False,
                              "output": "[Error] calendar refused"})
        self.assertFalse(runtime.task_successful())


class MixedOutcomesStillEnforceTheRealOnes(unittest.TestCase):
    def test_a_recognised_outcome_beside_a_generic_one_is_enforced(self):
        utterance = ("sort this out and remind me on saturday at 5am to "
                     "apply for the gate pass")
        _c, _s, runtime = runtime_for(utterance, _reminder_channels)
        problem = runtime.completion_problem()
        if runtime.ledger.records():
            # The reminder is real and unfinished, so completion is refused.
            self.assertIn("not complete", problem)

    def test_finishing_the_real_outcome_permits_completion(self):
        utterance = ("sort this out and remind me on saturday at 5am to "
                     "apply for the gate pass")
        _c, _s, runtime = runtime_for(utterance, _reminder_channels)
        runtime.apply_result({"type": "ADD_CALENDAR_EVENT", "ok": True,
                              "output": "[Success] Event created"})
        self.assertEqual(runtime.completion_problem(), "")


class VisualSourcesAreNotInterchangeable(unittest.TestCase):
    def test_screenshot_is_refused_for_camera_observation(self):
        _compiled, _selected, runtime = runtime_for(
            "what can you see", lambda _q, _limit=8: [
                "TAKE_SCREENSHOT", "CAPTURE_CAMERA"])
        problem = runtime.validate_proposal(
            '<ACTION type="TAKE_SCREENSHOT" />')
        self.assertIn("wrong source", problem)
        self.assertIn("CAPTURE_CAMERA", problem)

    def test_camera_is_refused_for_screen_observation(self):
        _compiled, _selected, runtime = runtime_for(
            "read the highlighted text", lambda _q, _limit=8: [
                "CAPTURE_CAMERA", "TAKE_SCREENSHOT"])
        problem = runtime.validate_proposal(
            '<ACTION type="CAPTURE_CAMERA" />')
        self.assertIn("wrong source", problem)
        self.assertIn("TAKE_SCREENSHOT", problem)


if __name__ == "__main__":
    unittest.main()
