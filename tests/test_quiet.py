# -*- coding: utf-8 -*-
"""
Tests for "stay quiet", and for two things that behaved unintelligently near it.

Himanshu said he was napping for ten minutes. VANTA answered "I'll stay quiet
until then", set a timer, and meant it -- then a proactive event fired and read
him an email while he slept. The promise was real; there was nothing behind it.
Silence existed in one sentence of one reply, and the ambient watcher, the
commitment monitor and the briefing scheduler had never heard of it.

The other two came from the same session:

* SET_ALARM was set successfully and reported as failed, twice, so he was told
  his backup alarm did not exist when it did.
* A bare "Approve" with nothing pending was read as a fresh instruction and cost
  five model calls planning a wake-up nobody had asked for.
"""

import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import quiet


class QuietWindowTests(unittest.TestCase):

    def setUp(self):
        quiet.clear()

    def tearDown(self):
        quiet.clear()

    def test_nothing_is_quiet_by_default(self):
        self.assertFalse(quiet.active())
        self.assertEqual(quiet.describe(), "")

    def test_a_window_can_be_opened(self):
        self.assertIsNotNone(quiet.begin(10, "napping"))
        self.assertTrue(quiet.active())
        self.assertEqual(quiet.reason(), "napping")

    def test_it_expires(self):
        """There is no indefinite quiet.

        A mode that outlives the situation it was set for stops being a
        preference and becomes a bug -- which is what happened to every sticky
        mode this project used to have.
        """
        quiet.begin(0.004)          # ~0.24s
        time.sleep(0.45)
        self.assertFalse(quiet.active())
        self.assertEqual(quiet.describe(), "")

    def test_it_is_capped(self):
        quiet.begin(99999)
        self.assertLessEqual(quiet.remaining_seconds(),
                             quiet.MAX_MINUTES * 60 + 5)

    def test_rubbish_opens_no_window(self):
        for bad in (None, "", "soon", 0, -5):
            self.assertIsNone(quiet.begin(bad), repr(bad))
            self.assertFalse(quiet.active())

    def test_the_model_is_told_it_should_be_quiet(self):
        # Suppression happens before a model is called, so this is for the turns
        # that DO happen: he asks something mid-nap and the answer should not
        # arrive with a briefing attached.
        quiet.begin(10, "napping")
        text = quiet.describe()
        self.assertIn("quiet", text.lower())
        self.assertIn("napping", text)
        self.assertIn("Alarms and timers", text)


class QuietScopeTests(unittest.TestCase):
    """What silence covers, and what it must never cover."""

    def setUp(self):
        quiet.clear()

    def tearDown(self):
        quiet.clear()

    def test_proactive_speech_is_held(self):
        """Asserted over the whole function, not a window into it.

        This sliced 700 characters after the first `origin == "proactive"` it
        found. A second such block was later added above the gate, so the window
        stopped reaching the check and the test failed while the behaviour was
        perfectly correct.
        """
        import inspect
        import agent
        source = inspect.getsource(agent.run_agent_loop)
        self.assertIn("quiet.active()", source)
        # The gate must RETURN, not merely notice.
        held = source.index("Held back a proactive event")
        following = source[held:held + 260]
        self.assertIn("return", following)

    def test_contextual_quiet_holds_noncritical_local_alerts(self):
        from pathlib import Path
        source = Path("agent.py").read_text(encoding="utf-8")
        start = source.index("def _deliver_local_alert")
        end = source.index("def _run_out_of_band_fast_command", start)
        source = source[start:end]
        self.assertIn('quiet.active() and priority != "critical"', source)
        self.assertIn("_defer_proactive(text)", source)

    def test_his_own_words_end_it(self):
        """Staying silent at someone who is talking to you is not discretion."""
        import inspect
        import agent
        source = inspect.getsource(agent.run_agent_loop)
        self.assertIn("quiet.clear()", source)
        clear_at = source.index("quiet.clear()")
        self.assertIn('origin == "user"', source[max(0, clear_at - 220):clear_at])

    def test_alarms_are_not_silenced(self):
        """The nap timer must still fire.

        Silencing the thing he asked to be woken by would obey the letter of
        "be quiet" and betray the whole point of it. Alarms run through the
        scheduler, not the proactive path, and nothing here touches them.
        """
        import inspect
        source = inspect.getsource(quiet)
        self.assertNotIn("alarm", source.lower().split("NOT suppressed")[0]
                         .replace("alarms and timers", ""))
        # Stated in the contract the model reads, too.
        quiet.begin(5)
        self.assertIn("still fire", quiet.describe())

    def test_the_action_exists_in_every_layer(self):
        """A promise the model cannot keep is worse than no promise.

        Four layers have to agree or the action is invisible: dispatcher,
        registry, catalogue, schema.
        """
        import action_schema
        import actions
        import agent
        import autonomy
        self.assertIn("QUIET_FOR", autonomy.KNOWN_ACTIONS)
        self.assertIn("QUIET_FOR", action_schema.ACTION_SPECS)
        self.assertTrue(any("QUIET_FOR" in text
                            for text in agent.ACTION_CATALOGS.values()))
        import action_bus
        self.assertTrue(action_bus.BUS.handles("QUIET_FOR"),
                        "nothing would run for it; the dispatcher is a table "
                        "and QUIET_FOR is not in it")

    def test_the_schema_accepts_minutes(self):
        # Written as "required" first, which the validator ignores -- so the
        # action refused the one attribute it needs.
        import action_schema
        spec = action_schema.ACTION_SPECS["QUIET_FOR"]
        self.assertIn("minutes", spec.get("requires", []))


class AlarmReceiptTests(unittest.TestCase):
    """An action that worked must not report failure."""

    def test_recurrence_is_accepted(self):
        """The catalogue advertises recurrence; the parser rejected it.

        The old pattern demanded exactly time= then label= then />, so the
        documented recurrence= made the tag match nothing, and the receipt told
        the model to check spelling that was already correct.
        """
        import action_attrs
        parsed = action_attrs.parse(
            'type="SET_ALARM" label="x" time="07:15" recurrence="weekdays"')
        self.assertEqual(parsed["time"], "07:15")
        self.assertEqual(parsed["recurrence"], "weekdays")

    def test_the_parser_is_order_independent(self):
        """recurrence= before time= must parse, not vanish.

        Asserted by parsing rather than by looking for a regex literal in the
        source: the old check searched execute_actions for the pattern text,
        so it broke the moment the pattern changed -- even when the change was
        to accept MORE spellings -- while telling nobody whether a tag
        actually parses.
        """
        import actions
        reply = ('<ACTION type="SET_ALARM" recurrence="daily" '
                 'label="standup" time="09:15" />')
        found = actions.parse_alarm_tags(reply)
        self.assertEqual(len(found), 1, found)
        self.assertEqual(found[0]["time"], "09:15")
        self.assertEqual(found[0]["label"], "standup")
        self.assertEqual(found[0]["recurrence"], "daily")

    def test_both_spellings_of_the_alarm_action_parse(self):
        """ADD_ALARM was authorized for a long time with no implementation."""
        import actions
        for name in ("SET_ALARM", "ADD_ALARM"):
            with self.subTest(action=name):
                found = actions.parse_alarm_tags(
                    f'<ACTION type="{name}" time="07:15" label="gym" />')
                self.assertEqual(len(found), 1, f"{name} parsed to nothing")

    def test_post_loop_actions_are_not_failed_before_they_run(self):
        """SET_ALARM has no branch in the main loop.

        So the loop reached its "the handler never wrote a receipt" check and
        reported failure for an action that ran perfectly a few lines later.
        Himanshu was told his 07:15 alarm had failed twice while it existed.
        """
        import actions
        self.assertIn("SET_ALARM", actions.POST_LOOP_ACTIONS)
        import inspect
        source = inspect.getsource(actions.execute_actions)
        self.assertIn("POST_LOOP_ACTIONS", source)

    def test_the_alarm_handler_records_its_own_receipt(self):
        import inspect
        import actions
        source = inspect.getsource(actions.execute_actions)
        self.assertIn('_record("SET_ALARM"', source)


class BareApprovalTests(unittest.TestCase):
    """"Approve" with nothing pending is not a new instruction."""

    def test_unambiguous_approval_is_answered_not_planned(self):
        import inspect
        import agent
        source = inspect.getsource(agent.run_agent_loop)
        self.assertIn("UNAMBIGUOUS_APPROVAL", source)
        self.assertIn("Nothing is waiting for your approval", source)

    def test_a_conversational_yes_still_falls_through(self):
        """"yes" and "go ahead" are conversation as often as consent.

        Asserted against the values and the behaviour rather than against the
        source text. The previous version searched inspect.getsource for a set
        literal, so moving the sets to module level -- which is what let the
        during-a-task path share them instead of keeping a second copy -- broke
        a test whose actual property was unchanged.
        """
        import agent
        for word in ("yes", "go ahead", "do it", "confirm"):
            with self.subTest(word=word):
                self.assertNotIn(word, agent.UNAMBIGUOUS_APPROVAL,
                                 f"'{word}' is too ambiguous to be unambiguous")
                # They must still reach the pending lookup...
                self.assertIn(word, agent.CONTEXTUAL_APPROVAL)
                # ...and count only when something is actually waiting.
                self.assertEqual(agent.approval_verdict(word, True), "approve")
                self.assertEqual(agent.approval_verdict(word, False), "")

    def test_an_unambiguous_approval_is_recognised_either_way(self):
        import agent
        for word in sorted(agent.UNAMBIGUOUS_APPROVAL):
            with self.subTest(word=word):
                self.assertEqual(agent.approval_verdict(word, True), "approve")
                self.assertEqual(agent.approval_verdict(word, False), "approve")


if __name__ == "__main__":
    unittest.main()
