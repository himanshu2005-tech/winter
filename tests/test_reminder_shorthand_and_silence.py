# -*- coding: utf-8 -*-
"""Two failures from one exchange: a lost day, and a turn that said nothing.

VANTA asked "How did starting your new project go?" and Himanshu replied,
asking to be reminded tomorrow. Two things went wrong.

The day was lost. He wrote "tmr", which the day-reference pattern did not
recognise, so the reminder had no date and went to SET_ALARM -- a channel that
holds a clock time and nothing else. Worse, the selection gate then blocked
ADD_CALENDAR_EVENT, which was the action that could actually have held the
date. The guard was right about not running two reminders and wrong about
which one to keep, purely because three letters were missing from a regex.

Then the turn went silent. SET_ALARM succeeded and the next step was a bare
<DONE /> with no <SAY>, so nothing was spoken and nothing appeared. From where
he was sitting the assistant simply stopped answering. The system prompt asks
for a closing line after verified completion, but asking is not enforcing, and
a turn that did real work must say so.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import outcome_compiler
import outcome_planner
import outcomes


def _channels(query, limit=8):
    return ["SET_ALARM", "ADD_TASK", "ADD_CALENDAR_EVENT"]


def selected_for(utterance):
    compiled = outcome_compiler.compile(utterance)
    bound = outcome_planner.bind(compiled, retrieve=_channels)
    chosen = outcome_planner.select(bound, utterance, [])
    return compiled, [item.selected_action for item in chosen]


class ShorthandDaysAreStillDays(unittest.TestCase):
    """He writes "tmr". That is a date, and an alarm cannot hold it."""

    def test_tmr_is_tomorrow(self):
        compiled, selected = selected_for("just remind me tmr")
        self.assertEqual(
            compiled.outcomes[0].arguments.get("date_reference"), "tmr")
        self.assertNotIn("SET_ALARM", selected)

    def test_every_shorthand_form_is_recognised(self):
        for word in ("tmr", "tmrw", "tomo", "2moro", "tonite"):
            with self.subTest(word=word):
                compiled = outcome_compiler.compile(f"remind me {word} at 5")
                self.assertTrue(
                    compiled.outcomes[0].arguments.get("date_reference"),
                    f"{word!r} was not read as a day")

    def test_tonite_is_still_tonight_and_may_use_an_alarm(self):
        """Tonight is today; an alarm represents it perfectly well."""
        _compiled, selected = selected_for("remind me tonite at 10")
        self.assertIn("SET_ALARM", selected)

    def test_shorthand_tomorrow_needs_a_dated_channel(self):
        _compiled, selected = selected_for("remind me tmrw at 9 to call them")
        self.assertTrue({"ADD_CALENDAR_EVENT", "ADD_TASK"} & set(selected),
                        selected)

    def test_a_weekday_shorthand_is_recognised(self):
        for word in ("sat", "sun", "mon", "tues", "thurs"):
            with self.subTest(word=word):
                compiled = outcome_compiler.compile(
                    f"remind me on {word} at 5 to pack")
                self.assertTrue(
                    compiled.outcomes[0].arguments.get("date_reference"),
                    f"{word!r} was not read as a day")

    def test_an_undated_reminder_still_uses_the_configured_channel(self):
        import settings
        _compiled, selected = selected_for("remind me at 4:30 to call madhu")
        self.assertIn(settings.reminder_channel(), selected)


class AFinishedTurnSaysSomething(unittest.TestCase):
    """A bare <DONE /> after real work read as the assistant giving up."""

    def test_a_receipt_becomes_a_spoken_confirmation(self):
        import agent
        spoken = agent.confirmation_from_receipts([
            {"type": "SET_ALARM", "ok": True,
             "output": "Alarm set for 4:30 PM: Start my new project"}])
        self.assertTrue(spoken)
        self.assertIn("4:30", spoken)

    def test_it_reports_what_happened_not_a_platitude(self):
        import agent
        spoken = agent.confirmation_from_receipts([
            {"type": "ADD_CALENDAR_EVENT", "ok": True,
             "output": "Event created: Apply for pass at 2026-10-17T05:00"}])
        self.assertIn("2026-10-17", spoken)

    def test_a_failure_is_not_dressed_up_as_success(self):
        import agent
        spoken = agent.confirmation_from_receipts([
            {"type": "SET_ALARM", "ok": False,
             "output": "[Error] the alarm store refused"}])
        lowered = spoken.lower()
        self.assertTrue("not" in lowered or "fail" in lowered
                        or "could" in lowered, spoken)
        self.assertNotIn("done", lowered.split()[:1])

    def test_nothing_to_report_yields_nothing(self):
        """Receipt synthesis itself must not invent work."""
        import agent
        self.assertEqual(agent.confirmation_from_receipts([]), "")

    def test_direct_bare_done_can_never_be_silent(self):
        import agent
        reply = agent.direct_turn_completion_fallback("user", "", "")
        self.assertIn("I heard you", reply)
        self.assertIn("Nothing was changed", reply)

    def test_proactive_bare_done_remains_intentionally_quiet(self):
        import agent
        self.assertEqual(
            agent.direct_turn_completion_fallback("proactive", "", ""), "")

    def test_real_response_is_never_replaced(self):
        import agent
        self.assertEqual(agent.direct_turn_completion_fallback(
            "user", "Done already.", ""), "")

    def test_helper_steps_alone_do_not_warrant_a_claim(self):
        import agent
        spoken = agent.confirmation_from_receipts([
            {"type": "DISCOVER_ACTIONS", "ok": True, "output": "12 found"}])
        self.assertEqual(spoken, "")

    def test_several_receipts_are_summarised_once(self):
        import agent
        spoken = agent.confirmation_from_receipts([
            {"type": "UPDATE_CALENDAR_EVENT", "ok": True,
             "output": "[Success] moved to 13:00"},
            {"type": "SET_ALARM", "ok": True,
             "output": "Alarm set for 4:30 PM"}])
        self.assertIn("13:00", spoken)
        self.assertIn("4:30", spoken)
        self.assertLessEqual(spoken.count("."), 3)


if __name__ == "__main__":
    unittest.main()
