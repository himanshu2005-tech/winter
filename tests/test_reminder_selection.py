# -*- coding: utf-8 -*-
"""
Choosing a reminder mechanism that can actually hold the request.

"Remind me on Saturday at 5 AM to apply for the gate pass" produced
SET_ALARM time="05:00" with no date, because the configured default channel
was an alarm and alarms take a clock time only -- alarms.set_alarm defaults to
today, or tomorrow if the time has passed. The reminder was therefore set for
the next morning rather than Saturday, three days early, and reported as done.

Then it got worse. The planner had decided a calendar entry was appropriate
and written its success criteria around a LIST_CALENDAR_EVENTS receipt, so the
critic failed the turn. The model tried ADD_CALENDAR_EVENT, the selection gate
blocked it as an unselected alternative, and that repeated four times across
fourteen model calls before the turn gave up -- after which the reflection
store learned the lesson "never run an alternative, even when verification
demands evidence the selected action cannot produce."

Three faults, all downstream of one: a channel was chosen that could not
represent the request. So selection has to consider what a channel is capable
of, not only what is configured.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import outcome_compiler
import outcome_planner
import settings


def _retrieve(query, limit=8):
    return ["SET_ALARM", "ADD_TASK", "ADD_CALENDAR_EVENT"]


def selected_for(utterance, plan=None):
    compiled = outcome_compiler.compile(utterance, plan=plan)
    bound = outcome_planner.bind(compiled, retrieve=_retrieve)
    chosen = outcome_planner.select(bound, utterance, [], plan=plan)
    return compiled, [item.selected_action for item in chosen]


class ADatedReminderNeedsADatedChannel(unittest.TestCase):
    def test_weekday_reminder_does_not_use_an_alarm(self):
        """An alarm cannot hold Saturday; it would fire tomorrow."""
        _compiled, selected = selected_for(
            "remind me on saturday at 5 am to apply for the gate pass")
        self.assertNotIn("SET_ALARM", selected)
        self.assertTrue(
            {"ADD_CALENDAR_EVENT", "ADD_TASK"} & set(selected), selected)

    def test_named_day_is_captured_as_a_date_reference(self):
        compiled, _selected = selected_for(
            "remind me on saturday at 5 am to apply for the gate pass")
        self.assertEqual(compiled.outcomes[0].arguments.get("date_reference"),
                         "saturday")

    def test_explicit_date_does_not_use_an_alarm(self):
        _compiled, selected = selected_for(
            "remind me on 17 October at 5am to pack")
        self.assertNotIn("SET_ALARM", selected)

    def test_tomorrow_does_not_use_an_alarm(self):
        _compiled, selected = selected_for(
            "remind me tomorrow at 7 to call the bank")
        self.assertNotIn("SET_ALARM", selected)

    def test_a_today_reminder_still_uses_the_configured_channel(self):
        _compiled, selected = selected_for("remind me at 4:30 to call madhu")
        self.assertIn(settings.reminder_channel(), selected)

    def test_tonight_is_today(self):
        _compiled, selected = selected_for(
            "remind me tonight at 10 to take my meds")
        self.assertIn(settings.reminder_channel(), selected)

    def test_an_explicit_alarm_request_is_still_honoured(self):
        """He asked for an alarm. Substituting would override his choice."""
        _compiled, selected = selected_for("set an alarm for 6am")
        self.assertIn("SET_ALARM", selected)


class ThePlannerAndTheCompilerMustNotDisagree(unittest.TestCase):
    """Two components independently choosing a mechanism is a deadlock."""

    def _plan(self, *lines):
        return type("Plan", (), {
            "goal": lines[0], "steps": list(lines),
            "success_criteria": list(lines), "fallback_steps": []})()

    def test_a_plan_naming_the_calendar_selects_the_calendar(self):
        plan = self._plan(
            "Create a calendar reminder for Saturday at 05:00.",
            "Verify it appears in a LIST_CALENDAR_EVENTS receipt.")
        _compiled, selected = selected_for(
            "remind me at 5 to apply for the pass", plan=plan)
        self.assertIn("ADD_CALENDAR_EVENT", selected)

    def test_a_plan_naming_a_task_selects_a_task(self):
        plan = self._plan("Create a Google Task due Saturday.")
        _compiled, selected = selected_for(
            "remind me at 5 to apply for the pass", plan=plan)
        self.assertIn("ADD_TASK", selected)

    def test_a_plan_naming_an_alarm_selects_an_alarm(self):
        plan = self._plan("Set an alarm for 05:00 today.")
        _compiled, selected = selected_for("remind me at 5 to call madhu",
                                           plan=plan)
        self.assertIn("SET_ALARM", selected)

    def test_no_plan_hint_falls_back_to_configuration(self):
        plan = self._plan("Remind him before he leaves.")
        _compiled, selected = selected_for("remind me at 4:30 to call madhu",
                                           plan=plan)
        self.assertIn(settings.reminder_channel(), selected)

    def test_a_dated_plan_hint_still_cannot_choose_an_alarm(self):
        """Capability beats preference. An alarm holds no date either way."""
        plan = self._plan("Set an alarm for Saturday at 05:00.")
        _compiled, selected = selected_for(
            "remind me on saturday at 5 to apply for the pass", plan=plan)
        self.assertNotIn("SET_ALARM", selected)


class TheGateMustNotDeadlock(unittest.TestCase):
    """A refusal with no way forward cost fourteen calls and 68k tokens."""

    def _runtime(self):
        import execution_reliability as reliability
        utterance = "remind me at 4:30 to call madhu"
        compiled = outcome_compiler.compile(utterance)
        bound = outcome_planner.bind(compiled, retrieve=_retrieve)
        chosen = outcome_planner.select(bound, utterance, [])
        return reliability.OutcomeRuntime(compiled, chosen)

    def test_first_proposal_is_blocked(self):
        runtime = self._runtime()
        self.assertIn("did not select", runtime.validate_proposal(
            '<ACTION type="ADD_CALENDAR_EVENT" />'))

    def test_repeated_insistence_is_released_rather_than_looped(self):
        """Persistent insistence means the selection was probably wrong."""
        runtime = self._runtime()
        messages = [runtime.validate_proposal('<ACTION type="ADD_TASK" />')
                    for _ in range(4)]
        self.assertTrue(messages[0])
        self.assertEqual(messages[-1], "",
                         "the gate never released; this is the deadlock")

    def test_release_is_recorded_as_an_override(self):
        runtime = self._runtime()
        for _ in range(4):
            runtime.validate_proposal('<ACTION type="ADD_TASK" />')
        notes = runtime.ledger.get("o1").notes
        self.assertTrue(any("released" in str(note) for note in notes), notes)

    def test_a_satisfied_outcome_tells_the_model_to_stop(self):
        runtime = self._runtime()
        runtime.apply_result({"type": "SET_ALARM", "ok": True,
                              "output": "[Success] alarm set"})
        message = runtime.validate_proposal('<ACTION type="ADD_TASK" />')
        self.assertIn("already", message.lower())
        self.assertIn("report", message.lower())

    def test_release_does_not_apply_to_a_satisfied_outcome(self):
        """Releasing here would create a second reminder."""
        runtime = self._runtime()
        runtime.apply_result({"type": "SET_ALARM", "ok": True,
                              "output": "[Success] alarm set"})
        messages = [runtime.validate_proposal('<ACTION type="ADD_TASK" />')
                    for _ in range(5)]
        self.assertTrue(all(messages), "a duplicate side effect was allowed")


if __name__ == "__main__":
    unittest.main()
