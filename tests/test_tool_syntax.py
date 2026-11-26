# -*- coding: utf-8 -*-
"""
Tests for a model that reaches for its own tool syntax instead of VANTA's.

The bug these exist for: Himanshu asked for a calendar event on Sunday. VANTA
did not create one -- it sent him the markup as a message. Pasting that same
text back in worked, because by then it arrived as a fresh instruction.

The model had written its native function-call form:

    <function_calls>
      <invoke name="ADD_CALENDAR_EVENT">
        <parameter name="summary">Exam</parameter>
      </invoke>
    </function_calls>

The detector only matched `<ACTION ` and `<tool_call>`, so response_has_action
was False, the turn was treated as ordinary conversation, and the tags went to
the dashboard and on to Telegram.

Refusing the syntax would be the wrong fix -- the model is asking for a real
action by a real name. It is translated instead, and the dispatcher still
validates the name against the registry, so an invented one is refused on its
own merits.
"""

import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from conversation_output import normalize_tool_syntax

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# The same test the agent uses to decide whether a turn acts.
DETECTS = re.compile(r'<ACTION\s+', re.IGNORECASE)


class NormaliseTests(unittest.TestCase):

    def test_the_shape_from_the_log(self):
        text = normalize_tool_syntax(
            "I'll fetch your unread emails.\n"
            "<function_calls>\n"
            '<invoke name="READ_EMAILS">\n'
            '<parameter name="query">is:unread</parameter>\n'
            "</invoke>\n"
            "</function_calls>")
        self.assertIn('<ACTION type="READ_EMAILS"', text)
        self.assertIn('query="is:unread"', text)
        self.assertTrue(DETECTS.search(text), "still not seen as an action")

    def test_the_calendar_event_that_was_missed(self):
        text = normalize_tool_syntax(
            '<invoke name="ADD_CALENDAR_EVENT">\n'
            '<parameter name="summary">TQM revision</parameter>\n'
            '<parameter name="date">2026-09-27</parameter>\n'
            '<parameter name="start_time">10:00</parameter>\n'
            "</invoke>")
        self.assertIn('type="ADD_CALENDAR_EVENT"', text)
        for attr in ('summary="TQM revision"', 'date="2026-09-27"',
                     'start_time="10:00"'):
            self.assertIn(attr, text)
        self.assertTrue(DETECTS.search(text))

    def test_the_wrapper_is_removed(self):
        text = normalize_tool_syntax(
            '<function_calls><invoke name="LIST_TASKS"></invoke></function_calls>')
        self.assertNotIn("function_calls", text)

    def test_a_multiline_value_becomes_the_body(self):
        # WRITE_FILE and RUN_PYTHON carry their payload in the tag body, not
        # in an attribute.
        text = normalize_tool_syntax(
            '<invoke name="WRITE_FILE">\n'
            '<parameter name="path">C:/tmp/a.py</parameter>\n'
            '<parameter name="content">line one\nline two</parameter>\n'
            "</invoke>")
        self.assertIn('path="C:/tmp/a.py"', text)
        self.assertIn("line one\nline two", text)
        self.assertNotIn('content="line one', text)
        self.assertTrue(text.rstrip().endswith("</ACTION>"))

    def test_a_quote_in_a_value_is_escaped(self):
        text = normalize_tool_syntax(
            '<invoke name="ADD_TASK">\n'
            '<parameter name="title">read "Dune"</parameter>\n'
            "</invoke>")
        self.assertIn("&quot;Dune&quot;", text)
        # The attribute must not be terminated early by the inner quote.
        self.assertTrue(DETECTS.search(text))

    def test_single_quoted_attribute_names_work(self):
        text = normalize_tool_syntax(
            "<invoke name='LIST_TASKS'></invoke>")
        self.assertIn('type="LIST_TASKS"', text)

    def test_the_name_is_upper_cased(self):
        text = normalize_tool_syntax('<invoke name="read_emails"></invoke>')
        self.assertIn('type="READ_EMAILS"', text)

    def test_correct_syntax_is_left_alone(self):
        original = '<ACTION type="BROWSER_SNAPSHOT" />'
        self.assertEqual(normalize_tool_syntax(original), original)

    def test_whitespace_after_open_bracket_is_repaired_for_known_action(self):
        text = normalize_tool_syntax(
            'Must use exact XML. < ACTION type="READ_EMAILS" query="is:unread" />')
        self.assertIn('<ACTION type="READ_EMAILS" query="is:unread" />', text)
        self.assertTrue(DETECTS.search(text))

    def test_spaced_unknown_action_is_not_promoted(self):
        original = '< ACTION type="BUY_ANYTHING" item="x" />'
        self.assertEqual(normalize_tool_syntax(original), original)

    def test_spaced_content_action_and_closing_tag_are_repaired(self):
        text = normalize_tool_syntax(
            '< ACTION type="SEND_EMAIL" to="x@example.com" subject="Hello">'
            'Body text< /ACTION>')
        self.assertEqual(
            text,
            '<ACTION type="SEND_EMAIL" to="x@example.com" subject="Hello">'
            'Body text</ACTION>')

    def test_registered_action_shorthand_from_music_log_is_normalized(self):
        text = normalize_tool_syntax(
            '<MUSIC_PLAY query="lofi study beats focus playlist" />')
        self.assertEqual(
            text,
            '<ACTION type="MUSIC_PLAY" query="lofi study beats focus playlist" />')
        self.assertTrue(DETECTS.search(text))

    def test_qwen_tool_call_prefix_from_prime_video_log_is_normalized(self):
        text = normalize_tool_syntax(
            '<tool_call>ACTION type="BROWSER_GOTO" '
            'url="https://www.primevideo.com/search?phrase=spider-man" />')
        self.assertEqual(
            text,
            '<ACTION type="BROWSER_GOTO" '
            'url="https://www.primevideo.com/search?phrase=spider-man" />')
        self.assertTrue(DETECTS.search(text))

    def test_tool_call_wrapping_a_normal_action_is_normalized(self):
        text = normalize_tool_syntax(
            '<tool_call><ACTION type="BROWSER_START" /></tool_call>')
        self.assertEqual(text, '<ACTION type="BROWSER_START" />')

    def test_unknown_tool_call_action_is_not_authorized(self):
        original = '<tool_call>ACTION type="BUY_ANYTHING" item="x" />'
        self.assertEqual(normalize_tool_syntax(original), original)

    def test_several_qwen_tool_calls_all_convert(self):
        text = normalize_tool_syntax(
            '<tool_call>ACTION type="BROWSER_START" />\n'
            '<tool_call>ACTION type="BROWSER_GOTO" url="https://example.com" />')
        self.assertEqual(text.count("<ACTION"), 2)
        self.assertNotIn("<tool_call>", text)

    def test_unknown_or_control_shorthand_is_not_authorized(self):
        for original in ('<INVENTED_COMMAND />', '<DONE />', '<ASK />'):
            self.assertEqual(normalize_tool_syntax(original), original)

    def test_prose_is_left_alone(self):
        original = "You have three unread emails, sir."
        self.assertEqual(normalize_tool_syntax(original), original)

    def test_empty_input_is_safe(self):
        self.assertEqual(normalize_tool_syntax(""), "")
        self.assertEqual(normalize_tool_syntax(None), "")

    def test_several_invokes_all_convert(self):
        text = normalize_tool_syntax(
            '<invoke name="BROWSER_START"></invoke>\n'
            '<invoke name="BROWSER_GOTO">'
            '<parameter name="url">https://x.com</parameter></invoke>')
        self.assertEqual(text.count("<ACTION"), 2)


class WiringTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        with open(os.path.join(ROOT, "agent.py"), encoding="utf-8") as handle:
            cls.agent = handle.read()

    def test_user_actions_use_visual_state_not_spoken_filler(self):
        self.assertIn('spoken_text = ""', self.agent)
        self.assertNotIn('spoken_text = f"On it, {_address()}."', self.agent)
        self.assertIn("set_state(STATE_THINKING, self.message)", self.agent)
        self.assertNotIn('speak(self.message, False)', self.agent)

    def test_startup_contract_has_concise_audible_greeting(self):
        self.assertIn("Write exactly one <SAY> block", self.agent)
        self.assertIn("short, natural hello", self.agent)
        self.assertNotIn('Analyze this startup state silently', self.agent)

    def test_normalisation_runs_before_action_detection(self):
        applied = self.agent.find("response = normalize_tool_syntax(response)")
        detected = self.agent.find("response_has_action = bool(re.search(")
        self.assertNotEqual(applied, -1, "the normaliser is not wired in")
        self.assertLess(applied, detected,
                        "translation must happen before the turn is judged")


class TeachingRouteTests(unittest.TestCase):
    """Cutting thirteen modes to two took teaching out of the router's words."""

    @classmethod
    def setUpClass(cls):
        with open(os.path.join(ROOT, "cognitive_pipeline.py"),
                  encoding="utf-8") as handle:
            cls.router = handle.read()
        with open(os.path.join(ROOT, "planner.py"), encoding="utf-8") as handle:
            cls.planner = handle.read()

    def test_the_router_knows_assistant_teaches(self):
        self.assertIn("TEACHING", self.router)
        self.assertIn("rich report", self.router)
        self.assertNotIn("use the blackboard", self.router)

    def test_the_router_is_told_when_the_screen_matters(self):
        import cognitive_pipeline

        with_screen = cognitive_pipeline._validate({
            "mode_decision": "keep",
            "target_mode": "assistant",
            "interaction": "respond",
            "task_kind": "observation",
            "planning_depth": "direct",
            "confidence": 1.0,
            "resources": [{"name": "screen", "purpose": "inspect it"}],
        }, "assistant")
        without_screen = cognitive_pipeline._validate({
            "mode_decision": "keep",
            "target_mode": "assistant",
            "interaction": "respond",
            "task_kind": "conversation",
            "planning_depth": "direct",
            "confidence": 1.0,
            "needs_screen": True,
            "resources": [],
        }, "assistant")

        self.assertTrue(with_screen.needs_screen)
        self.assertFalse(without_screen.needs_screen)

    def test_the_planner_is_told_when_to_ask_for_the_board(self):
        self.assertIn("teach me", self.planner)

    def test_the_teaching_actions_still_exist(self):
        import autonomy
        board = [a for a in autonomy.KNOWN_ACTIONS if a.startswith("BOARD_")]
        self.assertGreater(len(board), 15,
                           "the board actions went missing with the mode")


if __name__ == "__main__":
    unittest.main()
