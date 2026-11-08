# -*- coding: utf-8 -*-
"""
Tests for the three things that made a turn slower or dumber than it needed to be.

All three were found by measuring the live setup rather than reading it, and one
widely-assumed fix was measured and rejected:

* Context gathering ran to completion BEFORE the routing call, though nothing in
  it depends on the route -- it reads the calendar, telemetry, ambient state and
  alarms unconditionally. Sequential: 7.31s. Overlapped: 3.19s. Four seconds
  back on every turn.

* A direct-route turn was given a two-step budget. Two steps is one action and
  one reply, so a turn that had to act, read the receipt and then say what
  happened was cut off at exactly the wrong moment -- it created four calendar
  events and then stopped before confirming them.

* The nightly inference pass borrowed the critic's 1200-token budget and spent
  all of it reasoning: "output truncated after 4707 characters", then "no usable
  JSON returned", every boot. A whole call paid for and discarded.

Measured and rejected: per-stage model selection. Every model on the account
answers a trivial call in 2.4-2.8s, so the latency is round-trip overhead rather
than model size, and choosing a smaller model for the router would buy about two
tenths of a second. Not worth the mechanism.
"""

import os
import sys
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class ConcurrentContextTests(unittest.TestCase):
    """Routing selects evidence first; selected providers then run together."""

    def _loop_source(self):
        import inspect
        import agent
        return inspect.getsource(agent._inner_run_agent_loop)

    def test_context_is_gathered_on_a_thread(self):
        import inspect
        import resource_broker
        source = inspect.getsource(resource_broker.collect)
        self.assertIn("ThreadPoolExecutor", source)
        self.assertIn('thread_name_prefix="vanta-resource"', source)

    def test_the_router_selects_before_context_is_collected(self):
        source = self._loop_source()
        route = source.index("route_turn(")
        collect = source.index("_collect_context(cognitive_route)")
        self.assertLess(route, collect,
                        "private providers must not run before model selection")

    def test_it_is_collected_before_anything_reads_it(self):
        source = self._loop_source()
        collect = source.index("context_str = _collect_context()")
        route = source.index("route_turn(")
        self.assertLess(route, collect)

    def test_a_routing_failure_does_not_leave_the_turn_blind(self):
        """Failure retains a safe baseline without scraping unrelated state."""
        import agent
        source = self._loop_source()
        except_at = source.index("except Exception as route_error:")
        following = source[except_at:except_at + 220]
        self.assertIn("_collect_context()", following)
        context = agent.gather_context(resources=())
        self.assertIn("Current Time:", context)
        for private_block in ("CALENDAR", "OPEN TASKS", "ACTIVE ALARMS",
                              "LOCATION EVIDENCE", "USER-SELECTED"):
            self.assertNotIn(private_block, context)

    def test_unselected_providers_are_never_invoked(self):
        import resource_broker
        import resource_pool
        called = []
        bundle = resource_broker.collect(
            (resource_pool.ResourceRequest("web"),),
            {"web": lambda: "public",
             "calendar": lambda: called.append("calendar") or "private"})
        self.assertEqual(called, [])
        self.assertEqual([item.name for item in bundle.items], ["web"])

    def test_the_join_is_bounded(self):
        import resource_broker
        import resource_pool

        def slow():
            time.sleep(.35)
            return "late"

        requests = (resource_pool.ResourceRequest("calendar"),
                    resource_pool.ResourceRequest("tasks"))
        started = time.monotonic()
        bundle = resource_broker.collect(
            requests, {"calendar": slow, "tasks": slow}, timeout_sec=.05)
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, .20)
        self.assertEqual([item.ok for item in bundle.items], [False, False])

    def test_overlapping_is_actually_faster(self):
        """Completion order must not make prompt ordering nondeterministic."""
        import resource_broker
        import resource_pool

        def value_after(delay, value):
            time.sleep(delay)
            return value

        bundle = resource_broker.collect(
            (resource_pool.ResourceRequest("calendar"),
             resource_pool.ResourceRequest("environment")),
            {"calendar": lambda: value_after(.04, "calendar first"),
             "environment": lambda: value_after(.005, "weather second")},
            timeout_sec=1.0)
        self.assertLess(bundle.render().index("calendar first"),
                        bundle.render().index("weather second"))


class ExecutionBudgetTests(unittest.TestCase):
    """A direct turn still has to act, verify, and answer."""

    def test_a_trivial_plan_gets_room_to_finish(self):
        import planner
        plan = planner.Plan({"trivial": True, "steps": []})
        budget = plan.execution_budget("direct")
        self.assertGreaterEqual(budget, 4,
                                "two steps cannot act, observe and report")

    def test_budgets_are_still_proportional(self):
        """The floor was wrong; the principle was right.

        A flat large budget is what let a task with unobservable success
        criteria spin until it died.
        """
        import planner
        trivial = planner.Plan({"trivial": True, "steps": []})
        shallow = planner.Plan({"steps": ["one"], "trivial": False})
        deep = planner.Plan({"steps": ["a", "b", "c", "d", "e"], "trivial": False})
        self.assertLess(trivial.execution_budget("direct"),
                        deep.execution_budget("deep"))
        # Against the configured ceiling rather than a copy of its value: the
        # budgets are tunable, and pinning the number here turned a deliberate
        # increase into four unrelated test failures.
        self.assertLessEqual(shallow.execution_budget("direct"),
                             planner.STEP_BUDGETS["direct"])

    def test_a_deep_plan_is_still_capped(self):
        import planner
        huge = planner.Plan({"steps": ["s"] * 100, "trivial": False})
        self.assertLessEqual(huge.execution_budget("deep"),
                             planner.STEP_BUDGETS["deep"])
        # Bounded is the property worth holding, whatever the ceiling is.
        import agent
        self.assertLessEqual(huge.execution_budget("deep"),
                             agent.MAX_AGENT_STEPS)


class InferenceBudgetTests(unittest.TestCase):
    """A call that always truncates is a call always wasted."""

    def test_inference_has_its_own_budget(self):
        import inspect
        import llm_router
        source = inspect.getsource(llm_router.call_llm)
        self.assertIn('"inference"', source)

    def test_it_is_larger_than_the_critic_budget_it_borrowed(self):
        import inspect
        import llm_router
        source = inspect.getsource(llm_router.call_llm)
        table = source[source.index("_DEFAULT_BUDGETS"):]
        table = table[:table.index("}")]
        critic = int(table.split('"critic":')[1].split(",")[0])
        inference = int(table.split('"inference":')[1].split(",")[0])
        self.assertGreater(inference, critic,
                           "it truncated mid-reasoning on the critic's budget")

    def test_the_pass_asks_for_its_own_stage(self):
        import inspect
        import inference
        source = inspect.getsource(inference.run_inference_pass)
        self.assertIn('task_level="inference"', source)
        self.assertNotIn('task_level="critic"', source)

    def test_it_has_a_timeout_of_its_own(self):
        import llm_router
        self.assertGreaterEqual(llm_router.timeout_for("inference"), 60)

    def test_every_budgeted_stage_is_tunable(self):
        # A model change should be a .env edit, not a code edit.
        import inspect
        import llm_router
        self.assertIn("WINTER_BUDGET_", inspect.getsource(llm_router.call_llm))


class ModelChoiceTests(unittest.TestCase):
    """The measurement that stopped a feature being built.

    Per-stage model selection on the ChatGPT path looked like the obvious
    latency fix -- the router emits eleven characters of JSON and was paying
    for a frontier model. Measured across the account's models on a trivial
    call: 2.6s, 2.8s and 2.4s. The cost is the round trip, not the model, so
    the mechanism would have bought two tenths of a second and added a
    configuration surface for nothing.
    """

    def test_the_router_stage_exists_for_providers_that_can_use_it(self):
        import llm_router
        self.assertIn("cognitive_router", llm_router._STAGE_ENV)

    def test_the_default_model_fallback_is_not_a_slow_one(self):
        """The hardcoded fallback runs when .env fails to load.

        It was alibaba/qwen3.8-flash, measured at 266s per call in this
        project: a missing .env would have made VANTA appear hung rather than
        misconfigured.
        """
        import inspect
        import llm_router
        source = inspect.getsource(llm_router)
        # Inspect the assignment itself rather than assuming no bootstrap
        # helper can appear above it. Router-local .env loading now makes
        # provider choice independent of import order.
        assignment = next(
            line.strip() for line in source.splitlines()
            if line.strip().startswith("winter_model ="))
        self.assertNotIn("qwen3.8-flash", assignment,
                         "the silent fallback must not be the slow model")
        # And it must still have A default, or a missing .env is a crash.
        self.assertIn('os.getenv("WINTER_MODEL"', assignment)


if __name__ == "__main__":
    unittest.main()
