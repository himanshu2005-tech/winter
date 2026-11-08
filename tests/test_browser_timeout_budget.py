# -*- coding: utf-8 -*-
"""One slow page must not take the whole turn down with it.

From a real attempt at the hostel gate-pass portal. VANTA navigated, clicked
through to the pass form and filled it -- and then reported:

    the browser control service timed out during filling, saving, and
    verification. No successful submission receipt exists

Three operations in a row, all timing out. The portal was slow, not broken,
and the work had actually been getting somewhere.

The cause is arithmetic. The browser runs on one serial worker thread. A
caller waits 40 seconds for its reply, while Playwright's own per-action
timeout is 30 -- so a fill of six fields can legitimately take six times 30
seconds. The caller gives up at 40, the worker carries on, and every
subsequent request is queued behind the one still running. Each of those then
waits out its own 40 seconds against a backlog it cannot see. One slow
operation therefore poisons the rest of the turn, and the model, reading three
consecutive timeouts, reasonably concludes the service is dead.

So the caller's patience has to be derived from the work being asked for,
rather than being one number that happens to be smaller than the sum of the
parts.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import browser


class TimeoutsFitTheWork(unittest.TestCase):
    def test_a_fill_waits_longer_than_a_single_click(self):
        click = browser.call_timeout("click", {})
        fill = browser.call_timeout("fill_form", {"fields": {f"e{n}": {}
                                                             for n in range(6)}})
        self.assertGreater(fill, click)

    def test_a_fill_allows_for_every_field(self):
        """Six fields at up to 30s each cannot fit in 40 seconds."""
        fields = {f"e{n}": {} for n in range(6)}
        allowed = browser.call_timeout("fill_form", {"fields": fields})
        per_action = browser.DEFAULT_TIMEOUT_MS / 1000
        self.assertGreaterEqual(allowed, 6 * per_action)

    def test_the_caller_always_outlasts_one_playwright_action(self):
        """Otherwise the very first slow element reports a false failure."""
        per_action = browser.DEFAULT_TIMEOUT_MS / 1000
        for operation in ("click", "goto", "snapshot", "type", "press",
                          "upload", "select", "check", "start", "close"):
            with self.subTest(operation=operation):
                self.assertGreater(browser.call_timeout(operation, {}),
                                   per_action)

    def test_an_unknown_operation_still_gets_a_sane_wait(self):
        self.assertGreater(browser.call_timeout("something_new", {}), 0)

    def test_timeouts_stay_bounded(self):
        """A generous wait must not become an unbounded hang."""
        enormous = {"fields": {f"e{n}": {} for n in range(500)}}
        self.assertLessEqual(browser.call_timeout("fill_form", enormous),
                             browser.MAX_CALL_TIMEOUT)

    def test_the_ceiling_is_long_enough_to_be_useful(self):
        self.assertGreaterEqual(browser.MAX_CALL_TIMEOUT, 120)

    def test_a_timeout_says_the_page_was_slow_not_that_it_failed(self):
        """The wording decides what the model does next.

        "Control service timed out" reads as a broken subsystem, and the model
        stopped and declared the portal unusable. It was slow.
        """
        message = browser.timeout_message("fill_form", 180)
        self.assertIn("180", message)
        self.assertIn("fill_form", message)
        lowered = message.lower()
        self.assertTrue("slow" in lowered or "still" in lowered, message)
        self.assertIn("snapshot", lowered,
                      "the model needs to be told how to find out what "
                      "actually happened")


class ConfigurableWithoutACodeChange(unittest.TestCase):
    def test_the_ceiling_can_be_raised_from_the_environment(self):
        import importlib
        os.environ["WINTER_BROWSER_MAX_WAIT"] = "321"
        try:
            importlib.reload(browser)
            self.assertEqual(browser.MAX_CALL_TIMEOUT, 321)
        finally:
            os.environ.pop("WINTER_BROWSER_MAX_WAIT", None)
            importlib.reload(browser)


if __name__ == "__main__":
    unittest.main()
