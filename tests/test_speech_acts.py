# -*- coding: utf-8 -*-
"""Telling someone you did something is not asking them to do it.

The transcript:

    "its completed, delete the event"        (the gate pass was done)
    -> "I can't remove the calendar entry from here because calendar-
        management actions aren't currently available."

    "nO U R REMINDING ME ABOUT THE GATE PASS STUFF, I DID APPLY FR GATE PASS"
    -> opened a browser, hunted for a form, and asked "What URL should I open
       for the form you want submitted?" -- to which he answered "I DID NOT
       TELL U ANYTHING ABOUT THE FORM".

Nothing was wrong with the model. It was handed the wrong tools, three
separate ways.

*A report was compiled as an order.* The word "gate pass" alone matched the
web-workflow pattern, so "I did apply for the gate pass" became an instruction
to apply for it, and even "delete the gate pass event" went looking for a form.
A noun is a topic, not an operation.

*A phantom outcome replaced the real one in retrieval.* Each compiled outcome
is searched separately, so the invented "apply for the pass" displaced the real
intent. Asked to delete the entry, the model was shown browser actions only --
which is why it honestly reported that calendar actions "aren't available".

*The vocabulary matched on the topic.* "gate pass outpass homepass hostel" sat
on BROWSER_GOTO and BROWSER_FILL_FORM, so every mention of a pass, including
"stop reminding me about it", pulled the whole browser bundle.

None of the fixes says what "it's completed" should lead to. The filter is
about tense and stance. What follows a finished thing -- that its reminder is
no longer wanted -- is the model's to work out, and it can only do that once it
is given the calendar tools it was previously denied.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import outcome_compiler as compiler
import outcomes

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def operations(text):
    return [item.operation for item in compiler.compile(text).outcomes]


class AReportIsNotARequest(unittest.TestCase):
    def test_i_did_it_asks_for_nothing(self):
        for text in ("i did apply for gate pass",
                     "I already applied for the gate pass",
                     "i have applied",
                     "we sent it yesterday",
                     "it's done",
                     "its completed",
                     "that was submitted ages ago"):
            with self.subTest(text=text):
                self.assertEqual(operations(text), [],
                                 f"{text!r} was compiled as an order")

    def test_a_complaint_about_what_it_did_asks_for_nothing(self):
        for text in ("you are reminding me about it",
                     "u r reminding me again",
                     "you keep opening the browser",
                     "you didn't do what i said",
                     "why are you opening the browser"):
            with self.subTest(text=text):
                self.assertEqual(operations(text), [])

    def test_the_real_sentence_from_the_log(self):
        text = ("nO U R REMINDING ME ABOUT THE GATE PASS STUFF, "
                "I DID APPLY FR GATE PASS")
        self.assertEqual(operations(text), [],
                         "it opened a browser to apply for a pass that had "
                         "already been applied for")

    def test_a_report_followed_by_a_request_keeps_only_the_request(self):
        """The shape of the first sentence: what happened, then what to do."""
        self.assertEqual(operations("its completed, delete the event"),
                         [outcomes.GENERIC])
        self.assertEqual(
            operations("i already applied so delete the reminder"),
            [outcomes.GENERIC])

    def test_the_request_half_is_what_is_kept(self):
        self.assertEqual(
            compiler.requested_text("its completed, delete the event"),
            "delete the event")


class AnIntentionIsStillARequest(unittest.TestCase):
    """The filter must not eat real requests that happen to sound similar."""

    def test_have_to_is_an_intention_not_a_report(self):
        for text in ("i have to apply for my gate pass on the portal",
                     "i had to apply for the gate pass on the portal",
                     "i need to apply for the gate pass on the portal"):
            with self.subTest(text=text):
                self.assertEqual(operations(text), [outcomes.WEB_WORKFLOW])

    def test_i_just_want_is_not_a_report(self):
        self.assertEqual(
            operations("i just want you to set an alarm for 7am"),
            [outcomes.CREATE_ALARM])

    def test_ordinary_requests_are_unchanged(self):
        self.assertEqual(operations("remind me tomorrow at 5 to call madhu"),
                         [outcomes.CREATE_REMINDER])
        self.assertEqual(operations("apply for my gate pass on the portal"),
                         [outcomes.WEB_WORKFLOW])

    def test_stop_is_a_request_not_a_complaint(self):
        """"stop reminding me" asks for something; "you are reminding me"
        states something. Only the second is dropped."""
        self.assertFalse(compiler.is_report("stop reminding me"))
        self.assertTrue(compiler.is_report("you are reminding me"))


class ANounIsNotAnOperation(unittest.TestCase):
    def test_a_pass_alone_is_not_a_web_workflow(self):
        for text in ("delete the gate pass event",
                     "stop reminding me about the gate pass",
                     "what is the gate pass status",
                     "when is my gate pass"):
            with self.subTest(text=text):
                self.assertNotIn(outcomes.WEB_WORKFLOW, operations(text),
                                 f"{text!r} went looking for a form")

    def test_applying_for_one_still_is(self):
        for text in ("apply for my gate pass",
                     "get me an out pass for sunday",
                     "submit the home pass form",
                     "request a hostel pass"):
            with self.subTest(text=text):
                self.assertIn(outcomes.WEB_WORKFLOW, operations(text))

    def test_the_vocabulary_has_no_topic_nouns_on_the_browser(self):
        """It carried "gate pass outpass homepass hostel", which matched on
        every mention of a pass whatever was being done to it."""
        import capability_vocab
        for name in ("BROWSER_GOTO", "BROWSER_FILL_FORM"):
            with self.subTest(action=name):
                words = capability_vocab.OPERATION_VOCAB[name].lower()
                for noun in ("gate pass", "outpass", "homepass", "hostel"):
                    self.assertNotIn(noun, words)


class WhatTheModelIsActuallyGiven(unittest.TestCase):
    """The result that mattered: the right tools reach the model."""

    @classmethod
    def setUpClass(cls):
        import action_registry
        import agent
        import capabilities
        catalogs = dict(agent.ACTION_CATALOGS)
        catalogs.update(capabilities.catalog_overlay())
        action_registry.configure(catalogs)
        action_registry.warm_semantic_index(background=False)
        cls.registry = action_registry

    def shortlist(self, text):
        import protected_spans
        plan = compiler.compile(text)
        queries = list(plan.retrieval_queries()) or [
            protected_spans.command_envelope(
                compiler.retrieval_text(text),
                protected_spans.extract(text))]
        return [row["name"] for row in self.registry.search_outcomes(
            ROOT, queries, None, 12, "user")]

    def test_deleting_an_entry_is_reachable_after_a_report(self):
        names = self.shortlist("its completed, delete the event")
        self.assertIn("DELETE_CALENDAR_EVENT", names)
        self.assertIn("LIST_CALENDAR_EVENTS", names,
                      "it needs the event's id before it can delete it")

    def test_no_browser_is_offered_for_that(self):
        names = self.shortlist("its completed, delete the event")
        self.assertFalse([n for n in names if n.startswith("BROWSER")])

    def test_a_complaint_about_reminders_reaches_the_calendar(self):
        names = self.shortlist(
            "nO U R REMINDING ME ABOUT THE GATE PASS STUFF, "
            "I DID APPLY FR GATE PASS")
        self.assertIn("DELETE_CALENDAR_EVENT", names,
                      "the model would again be told calendar actions "
                      "are unavailable")
        self.assertFalse([n for n in names if n.startswith("BROWSER")],
                         "the report's verbs pulled the browser back in")

    def test_applying_still_reaches_the_browser(self):
        names = self.shortlist("apply for my gate pass on the portal")
        self.assertTrue([n for n in names if n.startswith("BROWSER")])


class WhenNothingWasAsked(unittest.TestCase):
    def test_the_complaint_is_what_is_searched(self):
        """The report describes the past; the complaint is the problem."""
        self.assertEqual(
            compiler.retrieval_text(
                "nO U R REMINDING ME ABOUT THE GATE PASS STUFF, "
                "I DID APPLY FR GATE PASS"),
            "nO U R REMINDING ME ABOUT THE GATE PASS STUFF")

    def test_without_a_complaint_the_whole_utterance_is_kept(self):
        """Dropping words on a guess would be worse than keeping them."""
        self.assertEqual(compiler.retrieval_text("i did apply for gate pass"),
                         "i did apply for gate pass")

    def test_the_agent_uses_it(self):
        with open(os.path.join(ROOT, "agent.py"), encoding="utf-8") as handle:
            source = handle.read()
        self.assertIn("outcome_compiler.retrieval_text", source)


class ProtectedContentIsLeftAlone(unittest.TestCase):
    """Commas inside quoted text are not fragment boundaries."""

    def test_a_quoted_body_is_not_split(self):
        text = 'text madhu "I did it, and I already told you, sorry"'
        plan = compiler.compile(text)
        self.assertTrue(plan.outcomes or plan.clarification,
                        "the filter mangled a message body")

    def test_the_filter_is_skipped_when_content_is_protected(self):
        import inspect
        source = inspect.getsource(compiler.compile)
        self.assertIn("if not spans:", source)


if __name__ == "__main__":
    unittest.main()
