# -*- coding: utf-8 -*-
"""Outcomes belong to what Himanshu asked for, and budgets must fit the work.

A startup briefing arrived, VANTA produced a correct REPORT, and then:

    [Outcomes] Rejected premature completion: The task is not complete:
    o2 (pending). o2 needs the created event is retrievable with its time.

There was no event to create. The outcome compiler had been run on the
*briefing instruction* -- VANTA's own prompt text, not a request -- and that
text lists "sections among TODAY, EMAIL, TASKS, WEATHER, and NEWS". The word
EMAIL compiles to SEND_EMAIL; a different phrasing compiled to a calendar
event. Either way a phantom outcome was enforced against a turn that was only
ever going to write a paragraph.

The cost was not just noise. The turn spent its whole four-step budget running
LIST_CALENDAR_EVENTS to satisfy the phantom, and when Himanshu then asked
"Will it rain today from 7-9??" the budget was already gone, so his actual
question was never answered.

Two fixes, and the order matters: an outcome is a thing the *user* asked for,
so proactive turns get none; and the budgets were too tight for a turn that
has to act, observe a receipt and then say what happened.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import outcome_compiler
import outcomes
from planner import Plan

STARTUP_TEXT = (
    "Synthesize the current sources into one polished executive introduction "
    "rather than treating them as unrelated alerts. Output a compact "
    "<REPORT> for the dashboard with only relevant sections among TODAY, "
    "EMAIL, TASKS, WEATHER, and NEWS; preserve concrete names and times and "
    "never fabricate missing data."
)


class BriefingTextIsNotARequest(unittest.TestCase):
    """VANTA's own instruction text must not become an enforced outcome."""

    def test_the_startup_text_does_compile_to_something(self):
        """Showing the hazard is real, not hypothetical."""
        compiled = outcome_compiler.compile(STARTUP_TEXT)
        self.assertTrue(compiled.outcomes,
                        "if this ever stops compiling, the guard below is "
                        "still the thing that makes it safe")

    def test_proactive_turns_are_not_enforced(self):
        self.assertFalse(outcome_compiler.should_compile("proactive"))

    def test_user_turns_are_enforced(self):
        self.assertTrue(outcome_compiler.should_compile("user"))

    def test_an_unknown_origin_is_not_enforced(self):
        """Default to not inventing obligations."""
        for origin in ("", None, "system", "startup"):
            with self.subTest(origin=origin):
                self.assertFalse(outcome_compiler.should_compile(origin))

    def test_an_explicitly_authored_routine_is_enforced(self):
        """A stored routine is delegated work, not unsolicited prose."""
        self.assertTrue(outcome_compiler.should_compile("routine"))

    def test_the_email_word_in_a_section_list_is_the_trap(self):
        """Documented so the next reader knows why this guard exists."""
        compiled = outcome_compiler.compile(
            "Output sections among TODAY, EMAIL, TASKS and NEWS for today")
        operations = {outcome.operation for outcome in compiled.outcomes}
        self.assertTrue(
            operations & {outcomes.SEND_EMAIL, outcomes.CREATE_SCHEDULED_EVENT,
                          outcomes.READ_SCHEDULE, outcomes.GENERIC},
            f"the trap changed shape: {operations}")


class BudgetsFitTheWork(unittest.TestCase):
    """Four steps could not act, observe and report; it died mid-task."""

    def _budget(self, depth, steps=0, trivial=False):
        plan = Plan({"goal": "g", "steps": ["s"] * steps,
                     "trivial": trivial, "deliverable": "d"})
        return plan.execution_budget(depth)

    def test_a_trivial_turn_can_act_observe_and_report(self):
        """Four was exactly the number that ran out mid-verification."""
        self.assertGreaterEqual(self._budget("direct", trivial=True), 8)

    def test_direct_depth_has_room_for_a_correction(self):
        self.assertGreaterEqual(self._budget("direct", steps=1), 8)

    def test_standard_depth_cap_grew(self):
        """Enough planned steps to reach the ceiling, which is what rose.

        The allowance stays len(steps) * 3 + 4; only the cap changed, so a
        four-step plan still gets sixteen. That proportionality is the part
        that was right all along.
        """
        self.assertGreaterEqual(self._budget("standard", steps=40), 32)

    def test_deep_depth_cap_grew(self):
        self.assertGreaterEqual(self._budget("deep", steps=100), 60)

    def test_a_small_plan_is_still_small(self):
        """Depth sets a ceiling, not an allowance.

        This used to assert exactly 16, which was the trivial floor rather
        than anything about proportionality -- so it broke the moment the
        floor moved, while saying nothing about whether a small plan had
        stayed small. The property is that a four-step plan gets a small
        fraction of what its depth permits.
        """
        import planner
        small = self._budget("standard", steps=4)
        self.assertLess(small, planner.STEP_BUDGETS["standard"] / 2)
        self.assertLess(small, self._budget("deep", steps=4))

    def test_budgets_still_scale_with_the_plan(self):
        """Proportional budgets were right; only the floor was wrong."""
        small = self._budget("standard", steps=1)
        large = self._budget("standard", steps=8)
        self.assertLess(small, large)

    def test_budgets_remain_bounded(self):
        """A raised ceiling must not become an unbounded loop."""
        import agent
        self.assertLessEqual(self._budget("deep", steps=500),
                             agent.MAX_AGENT_STEPS)

    def test_the_budget_is_configurable_without_a_code_change(self):
        import importlib
        import planner
        os.environ["VANTA_STEP_BUDGET_STANDARD"] = "99"
        try:
            importlib.reload(planner)
            plan = planner.Plan({"goal": "g", "steps": ["s"] * 40})
            self.assertEqual(plan.execution_budget("standard"), 99)
        finally:
            os.environ.pop("VANTA_STEP_BUDGET_STANDARD", None)
            importlib.reload(planner)


if __name__ == "__main__":
    unittest.main()
