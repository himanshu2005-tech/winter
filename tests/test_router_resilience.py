# -*- coding: utf-8 -*-
"""
Tests for surviving a dropped connection on a single-provider setup.

The bug these exist for: max_retries was pinned to 0 on every call, because
the router was designed to fall through to a different provider instead of
retrying. Himanshu runs AIRouter alone, so there was nothing to fall through
to, and a single transient connection error on a phone hotspot ended the turn
with "[API Error] airouter failed and no fallback is configured".

Measured at the time: AIRouter answered every stage in about two seconds
against timeouts of 30-90 seconds. Latency was never the problem; giving up
after one attempt was.
"""

import inspect
import io
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import llm_router

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class RetryBudgetTests(unittest.TestCase):

    def setUp(self):
        self.saved = os.environ.pop("WINTER_LLM_RETRIES", None)

    def tearDown(self):
        os.environ.pop("WINTER_LLM_RETRIES", None)
        if self.saved is not None:
            os.environ["WINTER_LLM_RETRIES"] = self.saved

    def test_retries_are_on_by_default(self):
        self.assertEqual(llm_router.connection_retries(), 2)

    def test_the_old_behaviour_is_still_reachable(self):
        os.environ["WINTER_LLM_RETRIES"] = "0"
        self.assertEqual(llm_router.connection_retries(), 0)

    def test_an_absurd_value_is_clamped(self):
        os.environ["WINTER_LLM_RETRIES"] = "99"
        self.assertEqual(llm_router.connection_retries(), 5)
        os.environ["WINTER_LLM_RETRIES"] = "-4"
        self.assertEqual(llm_router.connection_retries(), 0)

    def test_garbage_falls_back_to_the_default(self):
        os.environ["WINTER_LLM_RETRIES"] = "soon"
        self.assertEqual(llm_router.connection_retries(), 2)


class RetryPlacementTests(unittest.TestCase):
    """Only the last candidate retries; earlier ones switch instead."""

    @classmethod
    def setUpClass(cls):
        with open(os.path.join(ROOT, "llm_router.py"), encoding="utf-8") as handle:
            cls.source = handle.read()

    def test_max_retries_is_no_longer_pinned_to_zero(self):
        self.assertNotIn("client.with_options(max_retries=0)", self.source,
                         "every call is giving up after one attempt again")

    def test_the_budget_depends_on_being_the_last_candidate(self):
        self.assertIn("is_last = index == len(candidates) - 1", self.source)
        self.assertIn("attempts = connection_retries() if is_last else 0",
                      self.source)

    def test_a_single_provider_setup_is_always_the_last_candidate(self):
        # With AIRouter alone there is one candidate, index 0, which is last --
        # so it gets the retry budget. That is the whole point.
        saved = os.environ.pop("WINTER_FALLBACK_PROVIDERS", None)
        try:
            self.assertEqual(llm_router.fallback_providers(), [])
        finally:
            if saved is not None:
                os.environ["WINTER_FALLBACK_PROVIDERS"] = saved

    def test_timeouts_still_leave_room_for_the_measured_latency(self):
        # Every stage answered in roughly 2s when this was written. A budget
        # below ~10s would start failing on ordinary variance.
        for stage in ("cognitive_router", "routine", "planner", "vision"):
            self.assertGreaterEqual(
                llm_router.timeout_for(stage), 10.0,
                f"{stage} timeout is too tight for the measured latency")


class TokenAccountingTests(unittest.TestCase):
    """Nothing counted tokens, so cost could only be guessed at from the bill.

    A turn is not one completion -- routers, planner, critic and one call per
    iteration of the execution loop, each re-sending the whole tool catalogue.
    Without per-stage attribution, "it burnt 0.7 dollars in an hour" and "that
    model drinks tokens" are both unactionable.
    """

    def setUp(self):
        llm_router.reset_usage()

    def tearDown(self):
        llm_router.reset_usage()

    def test_nothing_recorded_means_no_noise(self):
        self.assertEqual(llm_router.usage_summary(), "")
        self.assertEqual(llm_router.usage_since(llm_router.usage_totals()), "")

    def test_usage_is_attributed_per_stage(self):
        llm_router.record_usage("planner", 6800, 900, exact=True)
        llm_router.record_usage("routine", 9100, 300, exact=True)
        llm_router.record_usage("routine", 9600, 250, exact=True)
        totals = llm_router.usage_totals()
        self.assertEqual(totals["routine"]["calls"], 2)
        self.assertEqual(totals["routine"]["prompt"], 18700)
        self.assertEqual(totals["planner"]["calls"], 1)

    def test_a_turn_reports_only_its_own_cost(self):
        llm_router.record_usage("planner", 5000, 100, exact=True)
        snapshot = llm_router.usage_totals()
        llm_router.record_usage("routine", 7000, 200, exact=True)
        line = llm_router.usage_since(snapshot)
        self.assertIn("7,000 in", line)
        self.assertNotIn("12,000", line)
        self.assertIn("routine", line)
        self.assertNotIn("planner", line)

    def test_estimates_are_declared_as_estimates(self):
        llm_router.record_usage("routine", 1000, 100, exact=False)
        self.assertIn("estimated", llm_router.usage_summary())
        llm_router.reset_usage()
        llm_router.record_usage("routine", 1000, 100, exact=True)
        self.assertNotIn("estimated", llm_router.usage_summary())

    def test_text_is_measured_and_images_are_counted_separately(self):
        messages = [{"role": "system", "content": "x" * 4000},
                    {"role": "user", "content": [
                        {"type": "text", "text": "y" * 400},
                        {"type": "image_url", "image_url": {"url": "..."}},
                        {"type": "image_url", "image_url": {"url": "..."}}]}]
        tokens, images = llm_router.estimate_tokens(messages)
        self.assertEqual(tokens, 1100)
        # Images dominate a vision turn's cost and are not measurable in
        # characters, so they are reported rather than folded into the estimate.
        self.assertEqual(images, 2)

    def test_estimating_survives_odd_message_shapes(self):
        self.assertEqual(llm_router.estimate_tokens([]), (0, 0))
        self.assertEqual(llm_router.estimate_tokens(None), (0, 0))
        tokens, images = llm_router.estimate_tokens(
            [{"role": "user"}, {"role": "user", "content": None}])
        self.assertEqual((tokens, images), (0, 0))

    def test_a_provider_that_refuses_usage_is_asked_only_once(self):
        # Losing the call to count its tokens would be a poor trade.
        self.assertIn("stream_options", inspect.getsource(llm_router._stream))
        self.assertIn("_USAGE_UNSUPPORTED", inspect.getsource(llm_router._stream))


class ShadowedImportTests(unittest.TestCase):
    """A nested import makes that name local to the WHOLE function.

    run_agent_loop has an "import os" inside a conditional branch, so os is a
    local name throughout it. Reading os.getenv in the token-reporting block at
    the end therefore raised UnboundLocalError on every turn where that branch
    had not run -- seen on a real boot as:

        [Tokens] Could not report usage: cannot access local variable 'os'

    The reporting itself worked; only the optional breakdown check broke. Which
    is why it survived the test suite: the failure was inside the try/except
    that exists to stop accounting ever taking down a turn.
    """

    def test_the_reporting_block_does_not_use_a_shadowed_name(self):
        import inspect
        import agent
        source = inspect.getsource(agent.run_agent_loop)
        start = source.index("usage_since")
        block = source[start:start + 700]
        # Comments discuss the bug by name, so only real code is examined --
        # this test failed on its own explanation the first time.
        code = "\n".join(line.split("#", 1)[0]
                         for line in block.splitlines())
        self.assertNotIn("os.getenv", code,
                         "os is local to this function; use identity.env_flag")
        self.assertIn("identity.env_flag", code)

    def test_functions_with_a_nested_os_import_do_not_read_os_earlier(self):
        """The general form of the bug, checked across the module.

        Any function that imports os partway through and also reads os before
        that point is broken on some path, whether or not it has been hit yet.
        """
        import ast
        import os as _os
        root = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
        with io.open(_os.path.join(root, "agent.py"), encoding="utf-8") as handle:
            tree = ast.parse(handle.read())

        offenders = []
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            import_lines = [
                child.lineno for child in ast.walk(node)
                if isinstance(child, ast.Import)
                and any(alias.name == "os" for alias in child.names)]
            if not import_lines:
                continue
            first_import = min(import_lines)
            for child in ast.walk(node):
                if (isinstance(child, ast.Name) and child.id == "os"
                        and isinstance(child.ctx, ast.Load)
                        and child.lineno < first_import):
                    offenders.append(f"{node.name} reads os at line "
                                     f"{child.lineno}, imports it at "
                                     f"{first_import}")
                    break
        self.assertEqual(offenders, [], "; ".join(offenders))


if __name__ == "__main__":
    unittest.main()
