# -*- coding: utf-8 -*-
"""Showing what VANTA is doing without covering what Himanshu is doing.

Two faults, opposite in size, met in the middle.

The queue has always carried a one-line description of each step --
"Running: npm install", "Searching memory for: '...'" -- and ui.py handled it
with `pass`. So the only places a step appeared were the terminal and the
hidden Activity page. From the front, a turn that ran four actions was a notch
that said PROCESSING for ninety seconds.

What grew up in that gap was the opposite: DISCOVER_ACTIONS was listed as an
action "whose result is itself the requested deliverable", so looking up its
own tool list opened a 680x720 card of twelve capability rows over whatever
was on screen. Nobody asks VANTA to discover actions. It is machinery, and
machinery announcing itself at full size is worse than machinery that says
nothing.

So: the step line goes on the notch, and the card is for results a person
actually asked for.
"""

import ast
import os
import sys
import time
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import actions

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class InternalStepsDoNotOpenAWindow(unittest.TestCase):
    def test_discovering_capabilities_is_not_a_deliverable(self):
        self.assertNotIn("DISCOVER_ACTIONS", actions.VISIBLE_RESULT_ACTIONS,
                         "looking up its own tool list opens a results card "
                         "over whatever is on screen")

    def test_nothing_surfaces_it_by_another_route(self):
        """_surface_action_result is the only door; it must stay shut."""
        sent = []
        surfaced = actions._surface_action_result(
            type("Q", (), {"put": lambda _s, item: sent.append(item)})(),
            "DISCOVER_ACTIONS", "[Success] 12 capability(ies) matching ...",
            "user")
        self.assertFalse(surfaced)
        self.assertEqual(sent, [])

    def test_result_capabilities_do_not_bypass_turn_presentation_policy(self):
        """Being a useful result does not itself authorize a popup."""
        for name in ("SEARCH_WEB", "LIST_CALENDAR_EVENTS", "FIND_FILES"):
            with self.subTest(action=name):
                self.assertIn(name, actions.VISIBLE_RESULT_ACTIONS)
                sent = []
                surfaced = actions._surface_action_result(
                    type("Q", (), {"put": lambda _s, item: sent.append(item)})(),
                    name, "meaningful result", "user")
                self.assertFalse(surfaced)
                self.assertEqual(sent, [])

    def test_it_says_what_it_is_doing_instead(self):
        """Removing the card must not leave the pause unexplained."""
        import action_bus
        branch = action_bus.BUS.handler_source("DISCOVER_ACTIONS")
        self.assertTrue(branch, "DISCOVER_ACTIONS has no handler on the bus")
        self.assertIn('("action"', branch,
                      "no step line, so the turn just pauses with no reason")

    def test_the_step_line_is_short_enough_for_a_notch(self):
        """The model's discovery query runs to a couple of hundred chars."""
        with open(os.path.join(ROOT, "actions.py"), encoding="utf-8") as handle:
            tree = ast.parse(handle.read())
        announced = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if not (isinstance(node.func, ast.Attribute)
                    and node.func.attr == "put"):
                continue
            for argument in node.args:
                if (isinstance(argument, ast.Tuple) and argument.elts
                        and isinstance(argument.elts[0], ast.Constant)
                        and argument.elts[0].value == "action"
                        and len(argument.elts) > 1
                        and isinstance(argument.elts[1], ast.Constant)):
                    announced.append(argument.elts[1].value)
        self.assertTrue(announced, "no literal step lines found to check")
        for line in announced:
            with self.subTest(line=line):
                self.assertLessEqual(len(str(line)), 60)


class TheStepLineIsNotThrownAway(unittest.TestCase):
    """The handler was `pass`. Checked against the source, because building a
    WinterUI needs a display -- narrow assertions, one message each."""

    @classmethod
    def setUpClass(cls):
        with open(os.path.join(ROOT, "ui.py"), encoding="utf-8") as handle:
            cls.ui = handle.read()

    def test_the_action_message_is_handled(self):
        self.assertRegex(self.ui, r'== "action":[\s\S]{0,160}self\._set_step_line')

    def test_it_reaches_the_notch(self):
        self.assertIn("_current_step_line()", self.ui)

    def test_it_is_cleared_when_the_answer_arrives(self):
        self.assertRegex(self.ui,
                         r'== "agent_response":[\s\S]{0,200}_clear_step_line')


class StepLineBehaviour(unittest.TestCase):
    """The decisions, on the mixin's own methods with no widgets."""

    class Notch:
        """Just enough of WinterUI for the step-line methods."""

        def __init__(self):
            self._step_line = ""
            self._step_line_at = 0.0
            self._notch_question = ""
            self.updated = 0

        def _update_notch(self):
            self.updated += 1

    @classmethod
    def setUpClass(cls):
        import ui
        # Borrow the real implementations rather than copying them.
        for name in ("_set_step_line", "_current_step_line",
                     "_clear_step_line"):
            setattr(cls.Notch, name, getattr(ui.WinterUI, name))
        # It is a staticmethod on WinterUI; fetching it off the class yields
        # the bare function, which would then take `self` as its first
        # argument. Re-wrap it so the borrowed methods call it as they do on
        # the real class.
        cls.Notch._notch_clean_text = staticmethod(
            ui.WinterUI.__dict__["_notch_clean_text"].__func__)
        cls.Notch._show_steps = staticmethod(
            ui.WinterUI.__dict__["_show_steps"].__func__)
        cls.Notch.STEP_LINE_TTL = cls.ttl = ui.WinterUI.STEP_LINE_TTL

    def setUp(self):
        self.notch = self.Notch()
        # Step narration is off by default; these tests are about the mechanism
        # when it is switched on.
        patcher = unittest.mock.patch.dict("os.environ", {"VANTA_SHOW_STEPS": "1"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_steps_are_not_narrated_unless_asked_for(self):
        with unittest.mock.patch.dict("os.environ", {"VANTA_SHOW_STEPS": "0"}):
            self.notch._set_step_line("Running: npm install")
        self.assertEqual(self.notch._current_step_line(), "")
        self.assertEqual(self.notch.updated, 0)

    def test_a_step_is_remembered_and_shown(self):
        self.notch._set_step_line("Running: npm install")
        self.assertEqual(self.notch._current_step_line(),
                         "Running: npm install")
        self.assertEqual(self.notch.updated, 1, "the notch was not repainted")

    def test_a_long_command_is_cut_to_fit(self):
        self.notch._set_step_line("Running: " + "x" * 200)
        self.assertLessEqual(len(self.notch._current_step_line()), 48)

    def test_an_empty_message_changes_nothing(self):
        self.notch._set_step_line("")
        self.assertEqual(self.notch._current_step_line(), "")
        self.assertEqual(self.notch.updated, 0)

    def test_a_stale_step_stops_being_claimed(self):
        """Still showing "Running: pip install" ten minutes later is a lie
        about what is happening now."""
        self.notch._set_step_line("Running: pip install numpy")
        self.notch._step_line_at = time.monotonic() - (self.ttl + 1)
        self.assertEqual(self.notch._current_step_line(), "")

    def test_a_step_just_inside_the_window_still_counts(self):
        self.notch._set_step_line("Searching memory")
        self.notch._step_line_at = time.monotonic() - (self.ttl - 5)
        self.assertEqual(self.notch._current_step_line(), "Searching memory")

    def test_clearing_it_is_immediate(self):
        self.notch._set_step_line("Running: dir")
        self.notch._clear_step_line()
        self.assertEqual(self.notch._current_step_line(), "")

    def test_the_newest_step_replaces_the_previous_one(self):
        self.notch._set_step_line("Running: git fetch")
        self.notch._set_step_line("Running: git merge")
        self.assertEqual(self.notch._current_step_line(), "Running: git merge")


if __name__ == "__main__":
    unittest.main()
