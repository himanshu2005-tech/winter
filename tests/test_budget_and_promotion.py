# -*- coding: utf-8 -*-
"""Room to work, and beliefs worth holding without being asked.

TWO SMALL NUMBERS DOING TWO JOBS
--------------------------------
The step ceilings were 24/40/70 and they were bounding two different things
at once: how much a turn may cost, and whether a turn has started going in
circles. They were bad at the first -- a gate-pass application ran out one
step from the end, having filled and submitted the form correctly -- and only
accidentally good at the second.

Raising the ceilings alone would have changed nothing, because the allowance
is `len(steps) * N + 4` and a research plan has four or five steps in it: a
deep turn got nineteen steps whether its ceiling was 70 or 700. The ceiling
was never what confined it.

So a planned step converts to executor steps at a rate that depends on the
depth -- "survey the current approaches" is one line of plan and a dozen
searches -- and the loop is caught by watching for progress instead.

CONFIDENCE IS NOT EVIDENCE
--------------------------
The second half is auto-promotion, and the obvious version of it is wrong.
The confidence number arrives in the same JSON as the claim it describes, and
the engine attaches 0.99 to restatements of its own input:

    PATTERN 0.99  Himanshu -TRACKS_DAILY_HEALTH_METRICS-> steps and exercise
                  minutes
      "He has recorded daily steps of 9050.0 and daily exercise minutes of
       34.3"

A threshold on that number promotes the vacuous with the useful. Corroboration
across independent passes is evidence the model cannot award itself, so that
is the second condition.
"""

import os
import sys
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import inference
import planner

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class FakePlan:
    trivial = False

    def __init__(self, steps):
        self.steps = ["step"] * steps


def budget(steps, depth):
    return planner.Plan.execution_budget(FakePlan(steps), depth)


class ResearchIsNotConfined(unittest.TestCase):
    def test_a_deep_plan_gets_hundreds_of_steps(self):
        self.assertGreaterEqual(budget(5, "deep"), 200)

    def test_the_deep_ceiling_is_what_was_asked_for(self):
        self.assertGreaterEqual(planner.STEP_BUDGETS["deep"], 600)

    def test_the_hard_ceiling_does_not_undercut_the_deep_budget(self):
        """200 was below what a deep plan may now ask for, so it would have
        silently capped the thing the budgets were raised to permit."""
        import agent
        self.assertGreaterEqual(agent.MAX_AGENT_STEPS,
                                planner.STEP_BUDGETS["deep"])

    def test_the_allowance_is_what_actually_confined_it(self):
        """With the old flat rate of 3, a 700 ceiling would be unreachable."""
        self.assertGreater(planner.STEP_ALLOWANCE["deep"],
                           planner.STEP_ALLOWANCE["standard"])
        self.assertGreater(planner.STEP_ALLOWANCE["standard"],
                           planner.STEP_ALLOWANCE["direct"])

    def test_a_small_task_stays_small(self):
        """Depth sets a ceiling, not an allowance. A one-step direct turn must
        not suddenly be allowed sixty."""
        self.assertLessEqual(budget(1, "direct"), 60)
        self.assertLessEqual(budget(1, "standard"), 60)

    def test_the_budget_grows_with_the_plan(self):
        self.assertLess(budget(2, "deep"), budget(8, "deep"))

    def test_nothing_exceeds_its_ceiling(self):
        for depth, cap in planner.STEP_BUDGETS.items():
            with self.subTest(depth=depth):
                self.assertLessEqual(budget(500, depth), cap)

    def test_the_floor_covers_doing_one_thing_properly(self):
        """16 was one action, an observation, a correction and a reply, with
        five of a browser turn's steps spent merely looking."""
        self.assertGreaterEqual(planner.TRIVIAL_STEP_BUDGET, 24)

    def test_every_budget_is_overridable_without_a_code_change(self):
        """These are the numbers that decide whether a turn can finish."""
        import inspect
        source = inspect.getsource(planner)
        for name in ("VANTA_STEP_BUDGET_", "_budget("):
            self.assertIn(name, source)


class ALoopIsStoppedByProgressNotByCost(unittest.TestCase):
    """What the small ceilings were really protecting against."""

    def test_there_is_a_stall_limit(self):
        self.assertGreater(planner.STALL_LIMIT, 0)

    def test_it_allows_room_to_recover(self):
        """Recovery genuinely takes a few turns: ask what went wrong, look,
        try again. Three would cut that off."""
        self.assertGreaterEqual(planner.STALL_LIMIT, 5)

    def test_it_is_far_below_the_deep_budget(self):
        """Otherwise a loop still costs seven hundred model calls."""
        self.assertLess(planner.STALL_LIMIT * 10,
                        planner.STEP_BUDGETS["deep"])

    def test_the_loop_counts_receipts_not_turns(self):
        with open(os.path.join(ROOT, "agent.py"), encoding="utf-8") as handle:
            source = handle.read()
        self.assertIn("stalled_steps", source)
        self.assertIn("STALL_LIMIT", source)
        # A receipt resets it; the absence of one advances it.
        start = source.index("if len(task_receipts) > receipts_seen:")
        block = source[start:start + 700]
        self.assertIn("stalled_steps = 0", block)
        self.assertIn("stalled_steps += 1", block)


class PromotionNeedsMoreThanTheModelSayingSo(unittest.TestCase):
    def test_the_confidence_threshold_is_what_he_asked_for(self):
        self.assertGreaterEqual(inference.AUTO_PROMOTE_CONFIDENCE, 0.90)

    def test_confidence_alone_is_not_enough(self):
        """The number arrives in the same JSON as the claim it describes."""
        self.assertGreaterEqual(inference.AUTO_PROMOTE_SIGHTINGS, 2)

    def test_promotion_runs_after_a_pass(self):
        import inspect
        source = inspect.getsource(inference.run_inference_pass)
        self.assertIn("auto_promote()", source)

    def test_a_broken_graph_does_not_break_the_pass(self):
        with unittest.mock.patch.object(
                inference.store, "auto_promote_inferences",
                side_effect=RuntimeError("graph down")):
            self.assertEqual(inference.auto_promote(), [])

    def test_it_reports_what_it_decided(self):
        """Believing something without asking has to be visible."""
        import inspect
        self.assertIn("Believed without asking",
                      inspect.getsource(inference.auto_promote))


class PromotionKeepsTheSafetyProperties(unittest.TestCase):
    """What promotion must not change, each for a specific reason."""

    @classmethod
    def setUpClass(cls):
        import inspect
        from memory import neo4j_store
        source = inspect.getsource(
            neo4j_store.GraphStore.auto_promote_inferences)
        # The Cypher only. The docstring explains which properties are
        # deliberately left alone, so searching the whole function for their
        # names finds the explanation rather than an assignment.
        start = source.index("MATCH (s:Entity)")
        cls.cypher = source[start:source.index('"""', start)]

    def test_it_stays_a_derived_inference(self):
        """The engine reads only explicitly stated facts. If promotion changed
        fact_class, it would start reading its own output -- and after a few
        passes be certain of things that were always guesses."""
        self.assertNotIn("fact_class=", self.cypher)

    def test_it_cannot_authorize_anything(self):
        self.assertNotIn("autonomy_eligible", self.cypher)
        self.assertNotIn("confirmation_required", self.cypher)

    def test_it_ranks_below_a_human_confirmation(self):
        """A human CONFIRM_INSIGHT sets authority 70; anything Himanshu
        states outright is higher still."""
        self.assertIn("r.authority=50", self.cypher)

    def test_it_is_distinguishable_from_what_he_agreed_to(self):
        self.assertIn("'auto_accepted'", self.cypher)

    def test_only_pending_proposals_are_promoted(self):
        """Not something already rejected."""
        self.assertIn("'pending_confirmation'", self.cypher)

    def test_a_rejected_proposal_is_not_resurrected(self):
        self.assertIn("r.valid_to IS NULL", self.cypher)

    def test_what_it_decided_can_be_listed(self):
        from memory import neo4j_store
        self.assertTrue(hasattr(neo4j_store.GraphStore,
                                "auto_accepted_inferences"))

    def test_what_it_decided_can_be_undone(self):
        """REJECT_INSIGHT matches on fact_class alone, so it reaches an
        auto-accepted row as well as a pending one."""
        import inspect
        from memory import neo4j_store
        source = inspect.getsource(
            neo4j_store.GraphStore.set_inference_status)
        self.assertNotIn("review_status,'')='pending_confirmation'", source)


class CorroborationIsCounted(unittest.TestCase):
    def test_a_repeat_proposal_increments_the_count(self):
        """Without this, mention_count stays 1 forever and nothing is ever
        corroborated -- the gate would be unreachable rather than strict."""
        import inspect
        from memory import neo4j_store
        source = inspect.getsource(neo4j_store.GraphStore.propose_inference)
        self.assertIn("ON MATCH SET", source)
        self.assertIn("mention_count", source)

    def test_only_an_inference_is_counted(self):
        """A proposal landing on a real stated fact added nothing, and must
        not inflate a counter as though it had."""
        import inspect
        from memory import neo4j_store
        source = inspect.getsource(neo4j_store.GraphStore.propose_inference)
        start = source.index("ON MATCH SET")
        self.assertIn("derived_inference", source[start:start + 400])


if __name__ == "__main__":
    import unittest.mock
    unittest.main()
