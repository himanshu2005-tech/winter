# -*- coding: utf-8 -*-
"""When VANTA may think on its own, and how much.

Three background jobs -- reading the web, reasoning over the graph, joining
his silos -- spend model calls, and the first spends network and battery. None
is wanted while he is working, and none may ever be the reason the machine is
slow when he comes back. So one place decides, and every job asks it.

The tests are about the two directions of that decision. Starting needs the
machine to have been idle for a while and unused by a turn; stopping needs a
single keystroke. Asymmetry on purpose: a job that starts too eagerly is an
annoyance, and a job that is slow to stop is a machine that is slow when you
sit down at it.
"""

import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import idle_gate


class ActivityProbeTests(unittest.TestCase):
    def tearDown(self):
        idle_gate.set_activity_probe(None)

    def test_busy_state_comes_from_registered_probe(self):
        idle_gate.set_activity_probe(lambda: True)
        self.assertTrue(idle_gate.agent_busy())
        idle_gate.set_activity_probe(lambda: False)
        self.assertFalse(idle_gate.agent_busy())

    def test_busy_check_never_imports_the_agent_runtime(self):
        idle_gate.set_activity_probe(None)
        with patch("builtins.__import__", side_effect=AssertionError(
                "agent runtime must not be imported")):
            self.assertFalse(idle_gate.agent_busy())

    def test_broken_probe_fails_closed(self):
        def broken():
            raise RuntimeError("probe failed")
        idle_gate.set_activity_probe(broken)
        self.assertTrue(idle_gate.agent_busy())


class Quiet(unittest.TestCase):
    """Every external signal pinned, so a test never depends on the machine."""

    def setUp(self):
        self.patches = [
            patch.object(idle_gate, "_idle_seconds", return_value=0.0),
            patch.object(idle_gate, "agent_busy", return_value=False),
            patch.object(idle_gate, "battery_ok", return_value=True),
        ]
        for patcher in self.patches:
            patcher.start()
            self.addCleanup(patcher.stop)


class WhenItMayStart(Quiet):
    def state(self, resting=False, standby=False, sleeping=False,
              idle=0.0):
        import screen_rest
        import runtime_power
        import mode_manager
        mode = (mode_manager.WinterMode.SLEEP if sleeping
                else mode_manager.WinterMode.ASSISTANT)
        with patch.object(screen_rest, "is_resting", return_value=resting), \
                patch.object(runtime_power, "is_standby", return_value=standby), \
                patch.object(mode_manager, "current_mode", return_value=mode), \
                patch.object(idle_gate, "_idle_seconds", return_value=idle):
            return idle_gate.current_state()

    def test_a_working_machine_is_active(self):
        self.assertEqual(self.state(idle=5.0), idle_gate.ACTIVE)

    def test_a_dark_screen_is_resting(self):
        self.assertEqual(self.state(resting=True), idle_gate.RESTING)

    def test_standby_is_standby(self):
        self.assertEqual(self.state(standby=True), idle_gate.STANDBY)

    def test_sleep_mode_is_sleeping(self):
        self.assertEqual(self.state(sleeping=True), idle_gate.SLEEPING)

    def test_a_long_absence_is_away(self):
        self.assertEqual(self.state(idle=idle_gate.AWAY_MINUTES * 60 + 1),
                         idle_gate.AWAY)

    def test_a_short_pause_is_not_away(self):
        """Long enough to rule out reading a page."""
        self.assertEqual(self.state(idle=120.0), idle_gate.ACTIVE)

    def test_the_hour_is_not_evidence(self):
        """Someone awake at two in the morning is not asleep. The state comes
        from what says so, never from the clock."""
        import inspect
        source = inspect.getsource(idle_gate.current_state)
        for word in ("datetime", "hour", "time.localtime", "strftime"):
            self.assertNotIn(word, source)

    def test_an_active_machine_may_not_start(self):
        with patch.object(idle_gate, "current_state",
                          return_value=idle_gate.ACTIVE):
            ok, why = idle_gate.allowed()
        self.assertFalse(ok)
        self.assertIn("active", why)

    def test_a_resting_machine_may(self):
        with patch.object(idle_gate, "current_state",
                          return_value=idle_gate.RESTING), \
                patch.object(idle_gate, "user_returned", return_value=False):
            ok, _why = idle_gate.allowed()
        self.assertTrue(ok)

    def test_it_will_not_start_while_he_is_typing(self):
        with patch.object(idle_gate, "current_state",
                          return_value=idle_gate.STANDBY), \
                patch.object(idle_gate, "user_returned", return_value=True):
            ok, why = idle_gate.allowed()
        self.assertFalse(ok)
        self.assertIn("using it", why)

    def test_it_will_not_start_during_a_turn(self):
        with patch.object(idle_gate, "current_state",
                          return_value=idle_gate.STANDBY), \
                patch.object(idle_gate, "agent_busy", return_value=True):
            ok, why = idle_gate.allowed()
        self.assertFalse(ok)

    def test_it_will_not_start_on_a_low_battery(self):
        with patch.object(idle_gate, "current_state",
                          return_value=idle_gate.STANDBY), \
                patch.object(idle_gate, "user_returned", return_value=False), \
                patch.object(idle_gate, "battery_ok", return_value=False):
            ok, why = idle_gate.allowed()
        self.assertFalse(ok)
        self.assertIn("battery", why)


class WhenItMustStop(Quiet):
    def test_a_keystroke_stops_it(self):
        with patch.object(idle_gate, "_idle_seconds", return_value=1.0):
            self.assertTrue(idle_gate.interrupted())

    def test_a_running_turn_stops_it(self):
        with patch.object(idle_gate, "agent_busy", return_value=True):
            self.assertTrue(idle_gate.interrupted())

    def test_stopping_is_stricter_than_starting(self):
        """Starting needs a long idle; stopping needs one keystroke."""
        with patch.object(idle_gate, "_idle_seconds", return_value=10.0):
            self.assertTrue(idle_gate.interrupted())
        with patch.object(idle_gate, "_idle_seconds",
                          return_value=idle_gate.RETURNED_SECONDS + 60):
            self.assertFalse(idle_gate.interrupted())

    def test_an_unreadable_idle_time_does_not_stop_it(self):
        """No signal is not evidence he is back; off Windows this is None."""
        with patch.object(idle_gate, "_idle_seconds", return_value=None):
            self.assertFalse(idle_gate.user_returned())


class Allowances(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.base = self._dir.name
        for target in ("interrupted",):
            patcher = patch.object(idle_gate, target, return_value=False)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_spending_is_counted(self):
        idle_gate.spend(self.base, "job", "calls", 3)
        idle_gate.spend(self.base, "job", "calls", 2)
        self.assertEqual(idle_gate.spent_today(self.base, "job", "calls"), 5)

    def test_jobs_have_separate_allowances(self):
        idle_gate.spend(self.base, "a", "calls", 4)
        self.assertEqual(idle_gate.spent_today(self.base, "b", "calls"), 0)

    def test_the_daily_limit_holds(self):
        idle_gate.spend(self.base, "job", "calls", 10)
        self.assertFalse(idle_gate.within_daily(self.base, "job", "calls", 10))
        self.assertTrue(idle_gate.within_daily(self.base, "job", "calls", 11))

    def test_the_allowance_survives_a_restart(self):
        """In memory, a crash loop would spend it again every boot."""
        idle_gate.spend(self.base, "job", "calls", 7)
        self.assertEqual(idle_gate.spent_today(self.base, "job", "calls"), 7)
        # a fresh read straight from disk, as a new process would make
        self.assertEqual(idle_gate._load(self.base)[idle_gate._today()]
                         ["job"]["calls"], 7)

    def test_old_days_are_dropped(self):
        import json
        path = idle_gate._path(self.base)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump({"1999-01-01": {"job": {"calls": 99}}}, handle)
        idle_gate.spend(self.base, "job", "calls", 1)
        self.assertNotIn("1999-01-01", idle_gate._load(self.base))
        self.assertEqual(idle_gate.spent_today(self.base, "job", "calls"), 1)

    def test_a_corrupt_file_is_not_fatal(self):
        with open(idle_gate._path(self.base), "w", encoding="utf-8") as handle:
            handle.write("{ not json")
        self.assertEqual(idle_gate.spent_today(self.base, "job", "calls"), 0)

    def test_the_write_is_atomic(self):
        import inspect
        self.assertIn("os.replace", inspect.getsource(idle_gate._save))


class ARun(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.base = self._dir.name
        patcher = patch.object(idle_gate, "interrupted", return_value=False)
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_for(self, **kwargs):
        defaults = dict(max_calls=3, max_seconds=60.0, daily_calls=None)
        defaults.update(kwargs)
        return idle_gate.Run(self.base, "job", **defaults)

    def test_it_may_continue_at_first(self):
        self.assertTrue(self.run_for().may_continue())

    def test_it_stops_at_its_call_limit(self):
        run = self.run_for()
        run.charge(3)
        self.assertFalse(run.may_continue())
        self.assertIn("3 calls", run.stopped_because)

    def test_it_stops_at_its_time_limit(self):
        run = self.run_for(max_seconds=0.0)
        self.assertFalse(run.may_continue())
        self.assertIn("seconds", run.stopped_because)

    def test_it_stops_when_he_comes_back(self):
        run = self.run_for()
        with patch.object(idle_gate, "interrupted", return_value=True):
            self.assertFalse(run.may_continue())
        self.assertIn("came back", run.stopped_because)

    def test_it_stops_at_the_daily_limit(self):
        idle_gate.spend(self.base, "job", "calls", 5)
        run = self.run_for(daily_calls=5)
        self.assertFalse(run.may_continue())
        self.assertIn("today", run.stopped_because)

    def test_charging_counts_against_both_allowances(self):
        run = self.run_for()
        run.charge(2)
        self.assertEqual(run.calls, 2)
        self.assertEqual(idle_gate.spent_today(self.base, "job", "calls"), 2)


if __name__ == "__main__":
    unittest.main()
