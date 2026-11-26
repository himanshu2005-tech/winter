# -*- coding: utf-8 -*-
"""
An attribute value containing '>' must not delete the whole action.

Found by driving the blackboard on a real screen. BOARD_DIAGRAM with
edges="Input->Hidden" produced no receipt at all -- not an error, not a
refusal, nothing. The action simply vanished between the model and the
dispatcher, and the turn carried on as though nothing had been asked.

The cause is the scan that finds self-closing tags:

    re.findall(r'<\\s*ACTION\\s+([^>]+?)/>', scan_text)

`[^>]` cannot cross a '>', so a '>' inside a quoted value ends the match early
and the tag never matches at all. That is not a blackboard problem. It is
every action whose arguments can legitimately contain '>':

    <ACTION type="RUN_COMMAND" cmd="dir > out.txt" />
    <ACTION type="BOARD_MATH" latex="x > 5" />
    <ACTION type="SEND_EMAIL" ...>quoted reply with > markers</ACTION>

Shell redirects and inequalities are ordinary things to ask for, and all of
them were being dropped in silence -- the worst possible failure, because the
model is told nothing and will either repeat itself or report success it never
earned.
"""

import os
import queue
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import actions

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def receipts_for(tag):
    """Dispatch one tag and return whatever receipts it produced."""
    actions.execute_actions._results = []
    actions.execute_actions(tag, {}, queue.Queue(), BASE_DIR, origin="user",
                            turn_id="scan-test")
    return list(getattr(actions.execute_actions, "_results", []))


class EveryActionIsSeen(unittest.TestCase):
    """The dispatcher must never silently lose a tag."""

    def test_angle_bracket_in_a_value_still_dispatches(self):
        receipts = receipts_for(
            '<ACTION type="BOARD_DIAGRAM" kind="tree" nodes="A,B" '
            'edges="A-&gt;B" />')
        self.assertTrue(receipts, "the action vanished")

    def test_raw_arrow_in_a_value_still_dispatches(self):
        receipts = receipts_for(
            '<ACTION type="BOARD_DIAGRAM" kind="flow" steps="A,B" '
            'label="A->B" />')
        self.assertTrue(receipts, "a raw '>' deleted the whole action")

    def test_math_inequality_still_dispatches(self):
        receipts = receipts_for('<ACTION type="BOARD_MATH" latex="x > 5" />')
        self.assertTrue(receipts, "an inequality deleted the whole action")

    def test_unknown_attribute_is_reported_not_swallowed(self):
        """A wrong attribute deserves the schema error, not silence."""
        receipts = receipts_for(
            '<ACTION type="BOARD_DIAGRAM" nodes="A,B" edges="A->B" />')
        self.assertTrue(receipts, "no receipt at all")
        self.assertFalse(receipts[0]["ok"])
        self.assertIn("edges", receipts[0]["output"])

    def test_single_quoted_value_with_an_arrow(self):
        receipts = receipts_for(
            "<ACTION type='BOARD_MATH' latex='a > b' />")
        self.assertTrue(receipts)

    def test_a_plain_action_still_works(self):
        self.assertTrue(receipts_for('<ACTION type="BOARD_PAGES" />'))

    def test_two_actions_on_one_line_are_both_seen(self):
        """The quote-aware scan must not merge adjacent tags into one."""
        receipts = receipts_for(
            '<ACTION type="BOARD_PAGES" /><ACTION type="BOARD_CLEAR" />')
        kinds = {item["type"] for item in receipts}
        self.assertEqual(kinds, {"BOARD_PAGES", "BOARD_CLEAR"})

    def test_an_arrow_does_not_swallow_a_following_action(self):
        receipts = receipts_for(
            '<ACTION type="BOARD_MATH" latex="x > 5" />'
            '<ACTION type="BOARD_PAGES" />')
        kinds = {item["type"] for item in receipts}
        self.assertIn("BOARD_PAGES", kinds, kinds)
        self.assertIn("BOARD_MATH", kinds, kinds)

    def test_prose_around_the_tag_is_ignored(self):
        receipts = receipts_for(
            'Let me show you. <ACTION type="BOARD_PAGES" /> Done.')
        self.assertTrue(receipts)


class DrawnShapesAreHonest(unittest.TestCase):
    """An unrecognised shape drew nothing and reported success.

    Found the same way: a rect from (300,300) to (900,650) never appeared on
    screen, but its label did, and the receipt said the annotation was drawn.
    The renderer knows box, circle, arrow, line, highlight, underline, number
    and callout; "rect" matched no branch and fell through to the label.
    """

    def test_a_documented_shape_is_accepted(self):
        for shape in ("box", "circle", "arrow", "highlight", "underline",
                      "callout", "number"):
            with self.subTest(shape=shape):
                receipts = receipts_for(
                    f'<ACTION type="DRAW_ON_SCREEN" x1="10" y1="10" '
                    f'x2="100" y2="100" shape="{shape}" />')
                self.assertTrue(receipts)
                self.assertTrue(receipts[0]["ok"], receipts[0]["output"])

    def test_a_common_synonym_is_accepted(self):
        """The model will reach for "rect" and "rectangle"."""
        for shape in ("rect", "rectangle", "square", "oval"):
            with self.subTest(shape=shape):
                receipts = receipts_for(
                    f'<ACTION type="DRAW_ON_SCREEN" x1="10" y1="10" '
                    f'x2="100" y2="100" shape="{shape}" />')
                self.assertTrue(receipts[0]["ok"], receipts[0]["output"])

    def test_an_unknown_shape_is_refused_not_silently_blank(self):
        receipts = receipts_for(
            '<ACTION type="DRAW_ON_SCREEN" x1="10" y1="10" x2="100" '
            'y2="100" shape="dodecahedron" />')
        self.assertTrue(receipts)
        self.assertFalse(receipts[0]["ok"],
                         "an undrawable shape reported success")
        self.assertIn("dodecahedron", receipts[0]["output"])

    def test_the_refusal_lists_what_is_available(self):
        receipts = receipts_for(
            '<ACTION type="DRAW_ON_SCREEN" x1="10" y1="10" x2="100" '
            'y2="100" shape="squiggle" />')
        self.assertIn("box", receipts[0]["output"])


if __name__ == "__main__":
    unittest.main()
