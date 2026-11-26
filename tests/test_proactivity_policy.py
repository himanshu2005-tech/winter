import datetime as dt
import json
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

import autonomy


class ProactivityPolicyTests(unittest.TestCase):
    def _plan(self, **overrides):
        values = {
            "goal": "Triage an ambient event",
            "reasoning": "Nothing changed, so remain quiet.",
            "deliverable": "No spoken output.",
            "steps": [],
            "question": None,
            "success_criteria": [],
            "stop_conditions": [],
        }
        values.update(overrides)
        return SimpleNamespace(**values)

    def test_silent_planner_verdict_is_enforceable(self):
        self.assertTrue(autonomy.proactive_plan_requests_silence(self._plan()))

    def test_real_proactive_work_is_not_suppressed(self):
        plan = self._plan(
            reasoning="A deadline changed and needs attention.",
            deliverable="A concise warning.",
            steps=["Read the calendar event"],
        )
        self.assertFalse(autonomy.proactive_plan_requests_silence(plan))

    def test_question_is_not_mistaken_for_silence(self):
        self.assertFalse(autonomy.proactive_plan_requests_silence(
            self._plan(question="Which deadline matters most?")))

    def test_silent_background_work_does_not_suppress_requested_briefing(self):
        plan = self._plan(
            goal="Perform background checks silently, then give a startup briefing.",
            reasoning="The current day should be summarized.",
            deliverable="A spoken briefing and dashboard report.",
        )
        self.assertFalse(autonomy.proactive_plan_requests_silence(plan))

    def test_daily_plan_is_available_after_morning(self):
        with tempfile.TemporaryDirectory() as base:
            noon = dt.datetime(2026, 10, 19, 14, 0)
            with mock.patch.object(autonomy, "date") as mocked_date:
                mocked_date.today.return_value = noon.date()
                self.assertTrue(autonomy.daily_plan_due(base, noon))

    def test_daily_plan_runs_once_and_preserves_other_state(self):
        with tempfile.TemporaryDirectory() as base:
            path = os.path.join(base, "vault", "system", "proactive_state.json")
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as handle:
                json.dump({"other_worker": "keep-me"}, handle)
            autonomy.mark_daily_plan_run(base)
            self.assertFalse(autonomy.should_run_daily_plan(base))
            with open(path, "r", encoding="utf-8") as handle:
                self.assertEqual(json.load(handle)["other_worker"], "keep-me")

    def test_daily_plan_does_not_interrupt_late_evening(self):
        with tempfile.TemporaryDirectory() as base:
            self.assertFalse(autonomy.daily_plan_due(
                base, dt.datetime(2026, 10, 19, 22, 0)))


if __name__ == "__main__":
    unittest.main()
