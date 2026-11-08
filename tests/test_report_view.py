# -*- coding: utf-8 -*-
"""Tests for report_view: the structure that was being thrown away."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import report_view


#: The briefing that prompted "it gives me some panic when i see this text
#: dump UI" -- kept verbatim as the parser's main fixture, because a shape the
#: model really produced is worth more than one invented to pass.
REAL_BRIEFING = """TODAY
- RL review: completed earlier today.
- Evening: dinner at 20:00; wind down and sleep at 22:30.

TASKS
- SDP review: tomorrow, 13 October.
- Amazon ML challenge: submission due 20 October.

EMAIL
- "Get hired by Eurogrip - Grab PPIs and 3L" from Team Unstop.
- Nothing else needing a reply.

NEWS
- Markets closed higher.
"""


class ParseStructure(unittest.TestCase):
    def test_all_caps_headings_become_headings(self):
        blocks = report_view.parse(REAL_BRIEFING)
        self.assertEqual(report_view.outline(blocks),
                         ["TODAY", "TASKS", "EMAIL", "NEWS"])

    def test_bullets_are_not_mistaken_for_headings(self):
        blocks = report_view.parse(REAL_BRIEFING)
        bullets = [b for b in blocks if b["kind"] == "bullet"]
        self.assertEqual(len(bullets), 7)

    def test_bullet_label_is_split_out(self):
        blocks = report_view.parse("TASKS\n- SDP review: tomorrow.\n")
        bullet = [b for b in blocks if b["kind"] == "bullet"][0]
        self.assertEqual(bullet["label"], "SDP review")
        self.assertEqual(bullet["text"], "tomorrow.")

    def test_markdown_and_bold_headings(self):
        blocks = report_view.parse("## Tasks\n- a thing\n**EMAIL**\n- another\n")
        self.assertEqual(report_view.outline(blocks), ["Tasks", "EMAIL"])

    def test_numbered_items_are_bullets(self):
        blocks = report_view.parse("STEPS\n1. First\n2) Second\n")
        self.assertEqual(
            [b["kind"] for b in blocks if b["kind"] != "spacer"],
            ["heading", "bullet", "bullet"])

    def test_blank_line_runs_collapse(self):
        blocks = report_view.parse("A\n\n\n\n- b\n")
        self.assertEqual(sum(1 for b in blocks if b["kind"] == "spacer"), 1)

    def test_no_trailing_spacer(self):
        blocks = report_view.parse("TODAY\n- a thing\n\n\n")
        self.assertNotEqual(blocks[-1]["kind"], "spacer")


class ThingsThatAreNotHeadings(unittest.TestCase):
    """The failure that would look worst: body text promoted to a heading."""

    def test_short_sentence_is_not_a_heading(self):
        blocks = report_view.parse("Chennai is clear at 30C.\nMore text here.\n")
        self.assertEqual(report_view.outline(blocks), [])

    def test_trailing_line_with_nothing_under_it_is_not_a_heading(self):
        blocks = report_view.parse("- a thing\nDONE")
        self.assertEqual(report_view.outline(blocks), [])

    def test_long_capitalised_line_is_not_a_heading(self):
        line = "THIS IS A VERY LONG SHOUTED LINE THAT RUNS WELL PAST THE LIMIT"
        blocks = report_view.parse(line + "\nbody\n")
        self.assertEqual(report_view.outline(blocks), [])

    def test_url_line_is_not_a_heading(self):
        blocks = report_view.parse("https://x.com/a\nbody\n")
        self.assertEqual(report_view.outline(blocks), [])


class LabelledValues(unittest.TestCase):
    def test_time_is_not_read_as_a_label(self):
        blocks = report_view.parse("Dinner at 20:30 tonight.\n")
        self.assertEqual(blocks[0]["kind"], "text")

    def test_url_is_not_read_as_a_label(self):
        blocks = report_view.parse("https://example.com/path\n")
        self.assertEqual(blocks[0]["kind"], "link")

    def test_quantity_is_flagged(self):
        blocks = report_view.parse("Temperature: 30C\n")
        self.assertTrue(blocks[0]["quantity"])

    def test_prose_value_is_not_flagged_as_quantity(self):
        blocks = report_view.parse("Status: looking good\n")
        self.assertFalse(blocks[0]["quantity"])


class PlainProse(unittest.TestCase):
    """A short answer should not be dressed up as a briefing."""

    def test_two_sentences_are_plain(self):
        blocks = report_view.parse("I moved the review. It is now on Monday.")
        self.assertTrue(report_view.is_plain_prose(blocks))

    def test_a_real_briefing_is_not_plain(self):
        blocks = report_view.parse(REAL_BRIEFING)
        self.assertFalse(report_view.is_plain_prose(blocks))

    def test_empty_is_plain(self):
        self.assertTrue(report_view.is_plain_prose(report_view.parse("")))


class RichBlocks(unittest.TestCase):
    def test_markdown_table_is_one_typed_block(self):
        blocks = report_view.parse(
            "## Options\n| Model | Speed |\n|---|---|\n| GLM | Fast |\n| Qwen | Medium |")
        table = next(block for block in blocks if block["kind"] == "table")
        self.assertEqual(table["headers"], ["Model", "Speed"])
        self.assertEqual(table["rows"], [["GLM", "Fast"], ["Qwen", "Medium"]])

    def test_mermaid_fence_becomes_a_diagram(self):
        blocks = report_view.parse(
            "```mermaid\ngraph TD\nA[Question] --> B[Answer]\n```")
        self.assertEqual(blocks, [{
            "kind": "diagram", "language": "mermaid",
            "text": "graph TD\nA[Question] --> B[Answer]",
        }])
        self.assertFalse(report_view.is_plain_prose(blocks))

    def test_other_fence_remains_code(self):
        blocks = report_view.parse("```python\nprint('hello')\n```")
        self.assertEqual(blocks[0]["kind"], "code")
        self.assertEqual(blocks[0]["language"], "python")

    def test_display_latex_is_a_formula_block(self):
        blocks = report_view.parse("## Result\n$$\\frac{a}{b}$$\n")
        self.assertEqual(blocks[1], {"kind": "math", "text": r"\frac{a}{b}"})
        self.assertFalse(report_view.is_plain_prose(blocks))

    def test_latex_fence_is_a_formula_block(self):
        blocks = report_view.parse("```latex\n\\sum_{i=1}^n i\n```")
        self.assertEqual(blocks[0]["kind"], "math")


class Robustness(unittest.TestCase):
    def test_none_does_not_raise(self):
        self.assertEqual(report_view.parse(None), [])

    def test_crlf_handled(self):
        blocks = report_view.parse("TODAY\r\n- a thing\r\n")
        self.assertEqual(report_view.outline(blocks), ["TODAY"])

    def test_every_nonblank_line_is_accounted_for(self):
        """Nothing may be silently dropped -- that is worse than a dump."""
        expected = len([l for l in REAL_BRIEFING.split("\n") if l.strip()])
        blocks = report_view.parse(REAL_BRIEFING)
        self.assertEqual(len([b for b in blocks if b["kind"] != "spacer"]),
                         expected)


if __name__ == "__main__":
    unittest.main()
