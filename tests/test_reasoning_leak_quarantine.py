# -*- coding: utf-8 -*-
"""Leaked model reasoning must not be fed back into the next prompt.

A real turn, and the sequence that destroyed it:

  1. The model emitted its own deliberation instead of an action --
     "< AWAIT? > no. We need only action output, already emitted commentary?
     In protocol action should be in final maybe. ... Could use
     CODING_TASK_START as activation?"
  2. The repair path appended that text to the conversation verbatim as an
     assistant turn, then asked for a clean action.
  3. Every later call in the turn therefore re-sent it, and it accumulated.
  4. The provider refused the request outright:
     "Invalid prompt: your prompt was flagged as potentially violating our
     usage policy".
  5. The fallback provider hit a connection error, and the turn was lost --
     after CREATE_PROJECT had already succeeded.

Himanshu asked for a project and an environment; he got a created project, a
dead turn, and no explanation.

The leaked text had no value to keep. The model does not need to re-read its
own malformed output to produce a good one; it needs to be told what to
produce. Keeping it achieved nothing and poisoned every subsequent request in
the turn, so it is summarised rather than echoed.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import conversation_output

#: Verbatim from the transcript, which is the only fixture worth trusting.
LEAKS = (
    "< ACTION? > can't in final? Need emit exact XML in commentary likely. "
    "Let's do.",
    "< ACTION type...> cannot in final? Need action channel likely "
    "commentary. We need output action not final. Let's emit.",
    "< AWAIT? > no. We need only action output, already emitted commentary? "
    "In protocol action should be in final maybe. But now need continue "
    "after discovery unavailable. Since no receipt yet, cannot. Could use "
    "CODING_TASK_START as activation?",
    "< S A Y >Nice, sir. Enjoy the dinner.< / S A Y >",
    "<action? Need XML exact. Discover first because missing capability. "
    "Must emit action only.",
)

#: Things that are not leakage and must keep working.
CLEAN = (
    '<ACTION type="CREATE_PROJECT" name="Mavo" />',
    "<SAY>Done, sir. The alarm is set for 4:30.</SAY><DONE />",
    "<REPORT>\nTODAY\n- one thing\n</REPORT>",
    "Yes, sir - rain is likely from 7 to 9 this evening.",
    "<ASK>Which event should I move?</ASK>",
    "<DONE />",
)


class LeakageIsRecognised(unittest.TestCase):
    def test_every_observed_leak_is_detected(self):
        missed = [text for text in LEAKS
                  if not conversation_output.is_reasoning_leak(text)]
        self.assertEqual(missed, [], f"undetected leakage: {missed}")

    def test_valid_protocol_is_not_mistaken_for_leakage(self):
        wrong = [text for text in CLEAN
                 if conversation_output.is_reasoning_leak(text)]
        self.assertEqual(wrong, [], f"valid output flagged: {wrong}")

    def test_ordinary_prose_about_actions_is_not_leakage(self):
        """Talking about an action is not the same as failing to emit one."""
        for text in ("I will set an alarm for you now.",
                     "That needs an action I do not have.",
                     "The calendar action succeeded."):
            with self.subTest(text=text):
                self.assertFalse(conversation_output.is_reasoning_leak(text))

    def test_empty_is_not_leakage(self):
        self.assertFalse(conversation_output.is_reasoning_leak(""))
        self.assertFalse(conversation_output.is_reasoning_leak(None))


class LeakageIsQuarantinedNotEchoed(unittest.TestCase):
    """The part that actually broke the turn."""

    def test_the_placeholder_carries_none_of_the_original(self):
        for text in LEAKS:
            with self.subTest(text=text[:40]):
                placeholder = conversation_output.quarantine_leak(text)
                self.assertTrue(placeholder)
                # No distinctive fragment of the leak may survive, because
                # what survived is what the provider refused to accept.
                for fragment in ("CODING_TASK_START", "commentary",
                                 "Let's emit", "exact XML", "final"):
                    self.assertNotIn(fragment, placeholder)

    def test_the_placeholder_is_short(self):
        """A long placeholder is just the same problem more politely."""
        for text in LEAKS:
            with self.subTest(text=text[:40]):
                self.assertLess(len(conversation_output.quarantine_leak(text)),
                                200)

    def test_the_placeholder_says_what_happened(self):
        placeholder = conversation_output.quarantine_leak(LEAKS[0])
        lowered = placeholder.lower()
        self.assertTrue("not" in lowered or "no " in lowered, placeholder)

    def test_the_repair_path_appends_the_placeholder_not_the_leak(self):
        """Asserted against the agent's own source: the append is the bug."""
        import inspect
        import agent
        source = inspect.getsource(agent._inner_run_agent_loop)
        start = source.index("syntax_meta")
        window = source[start:start + 1800]
        self.assertIn("quarantine_leak", window,
                      "the raw response is still being echoed back")


class TheConversationStaysClean(unittest.TestCase):
    def test_a_quarantined_turn_leaves_no_scratch_markup(self):
        """What goes into the history must be protocol or plain prose."""
        for text in LEAKS:
            with self.subTest(text=text[:40]):
                placeholder = conversation_output.quarantine_leak(text)
                self.assertFalse(
                    conversation_output.is_reasoning_leak(placeholder),
                    "the placeholder would itself be detected as leakage")


if __name__ == "__main__":
    unittest.main()
