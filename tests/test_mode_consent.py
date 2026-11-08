# -*- coding: utf-8 -*-
"""
Tests for who gets to change VANTA's operating mode.

The bug these exist for: "Open my placements portal and search for the latest
jobs" was routed to Research with 0.9 confidence. VANTA switched itself into
Research mode, resumed an autonomous study session on graph theory, and ran it
concurrently with the portal task -- both streaming to the same console, until
one reply came out spliced through the other's JSON mid-word:

    "confidence": 1<SAY>Sir, I need the URL and login credent.0

Himanshu now decides. An explicit request is obeyed and anything else is
offered to him as a question. Nothing switches itself: emergency mode was the
one exception worth taking unasked, and the thirteen modes have since been cut
to two -- assistant, and build for coding -- so there is nothing left urgent
enough to change mode on its own.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cognitive_pipeline import (CognitiveRoute, RetrievalRoute,
                                mode_switch_needs_confirmation,
                                should_apply_mode_switch)
import mode_manager

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def route(**kwargs):
    base = dict(
        mode_decision="switch", target_mode="build", interaction="respond",
        task_kind="conversation", planning_depth="standard", needs_screen=False,
        confidence=0.9, retrieval=RetrievalRoute(),
    )
    base.update(kwargs)
    return CognitiveRoute(**base)


class SwitchAuthorityTests(unittest.TestCase):

    def test_the_router_cannot_switch_on_its_own(self):
        self.assertFalse(should_apply_mode_switch(route()))

    def test_an_explicit_request_is_obeyed(self):
        self.assertTrue(should_apply_mode_switch(route(), explicit=True))

    def test_nothing_switches_itself_any_more(self):
        # Emergency mode used to be the one exception worth taking without
        # asking. It no longer exists: with assistant and build left, there is
        # nothing urgent enough to change mode unasked.
        for target in ("assistant", "study", "research", "build", "health", "sleep"):
            self.assertFalse(should_apply_mode_switch(route(target_mode=target)),
                             f"{target} switched without being asked for")

    def test_keeping_the_mode_is_never_a_switch(self):
        self.assertFalse(should_apply_mode_switch(route(mode_decision="keep")))
        self.assertFalse(mode_switch_needs_confirmation(route(mode_decision="keep")))
        self.assertFalse(should_apply_mode_switch(None))


class ConfirmationScopeTests(unittest.TestCase):
    """Asking is the fallback, but not in front of work he just asked for."""

    def test_the_placements_portal_turn_is_not_even_asked_about(self):
        # The exact route from the log: an action needing the screen.
        self.assertFalse(mode_switch_needs_confirmation(
            route(task_kind="action", needs_screen=True)))

    def test_a_screen_turn_never_changes_mode(self):
        for kind in ("action", "observation", "conversation"):
            self.assertFalse(
                mode_switch_needs_confirmation(
                    route(task_kind=kind, needs_screen=True)),
                f"{kind} with screen should stay in the current mode")

    def test_a_plain_conversation_does_get_the_question(self):
        self.assertTrue(mode_switch_needs_confirmation(route()))

    def test_a_low_confidence_guess_is_not_worth_asking(self):
        self.assertFalse(mode_switch_needs_confirmation(route(confidence=0.5)))


class ProposalTests(unittest.TestCase):

    def setUp(self):
        mode_manager.clear_mode_proposal()

    def tearDown(self):
        mode_manager.clear_mode_proposal()

    def test_a_proposal_is_held_until_answered(self):
        mode_manager.propose_mode("build", "looks like project work")
        self.assertEqual(mode_manager.pending_mode_proposal(),
                         mode_manager.WinterMode.BUILD)

    def test_yes_accepts_it(self):
        mode_manager.propose_mode("build")
        self.assertEqual(mode_manager.answer_to_proposal("yes"),
                         ("accept", mode_manager.WinterMode.BUILD))
        self.assertIsNone(mode_manager.pending_mode_proposal(),
                          "an answered proposal must not linger")

    def test_no_declines_it(self):
        mode_manager.propose_mode("build")
        verdict, mode = mode_manager.answer_to_proposal("no thanks")
        self.assertEqual(verdict, "decline")
        self.assertEqual(mode, mode_manager.WinterMode.BUILD)
        self.assertIsNone(mode_manager.pending_mode_proposal())

    def test_a_new_instruction_is_not_an_answer(self):
        # "sure" buried in a real request must not be read as consent.
        mode_manager.propose_mode("build")
        self.assertIsNone(mode_manager.answer_to_proposal(
            "sure but first open my email and check for anything urgent"))
        self.assertIsNotNone(mode_manager.pending_mode_proposal())

    def test_nothing_pending_means_yes_means_nothing(self):
        self.assertIsNone(mode_manager.answer_to_proposal("yes"))

    def test_a_stale_proposal_expires(self):
        import time
        mode_manager.propose_mode("build")
        mode_manager._proposal["at"] = time.time() - (
            mode_manager.PROPOSAL_TTL_SECONDS + 1)
        self.assertIsNone(mode_manager.pending_mode_proposal())
        self.assertIsNone(mode_manager.answer_to_proposal("yes"))

    def test_an_unknown_mode_is_not_proposed(self):
        self.assertIsNone(mode_manager.propose_mode("teleportation"))


class ConsoleSerialisationTests(unittest.TestCase):
    """Two threads streaming to one console is what corrupted the output."""

    @classmethod
    def setUpClass(cls):
        with open(os.path.join(ROOT, "llm_router.py"), encoding="utf-8") as handle:
            cls.router = handle.read()

    def test_console_writes_go_through_a_lock(self):
        self.assertIn("_PRINT_LOCK", self.router)
        self.assertIn("with _PRINT_LOCK:", self.router)

    def test_background_callers_can_stay_quiet(self):
        """Checked by parameter, not by the whole signature.

        This pinned the exact signature text, so adding on_delta for streaming
        speech broke a test about console quietness -- which tells you nothing
        true and sends you reading the wrong file.
        """
        import inspect
        import llm_router
        params = inspect.signature(llm_router.call_llm).parameters
        self.assertIn("quiet", params)
        self.assertIs(params["quiet"].default, False)
        self.assertIn("quiet=quiet", self.router)

    def test_the_two_background_loops_pass_it(self):
        with open(os.path.join(ROOT, "research.py"), encoding="utf-8") as handle:
            self.assertIn("quiet=True", handle.read(),
                          "the research loop still streams to the console")
        with open(os.path.join(ROOT, "memory", "consolidator.py"),
                  encoding="utf-8") as handle:
            self.assertIn("quiet=True", handle.read(),
                          "fact extraction still streams to the console")


class AgentWiringTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        with open(os.path.join(ROOT, "agent.py"), encoding="utf-8") as handle:
            cls.source = handle.read()

    def test_an_answer_is_checked_before_a_mode_command(self):
        # A bare "yes" is only a mode command while an offer is outstanding.
        idx_answer = self.source.find("answered = answer_to_proposal(text)")
        idx_parse = self.source.find("parsed = parse_mode_command(text)")
        self.assertNotEqual(idx_answer, -1, "proposal answers are not wired in")
        self.assertLess(idx_answer, idx_parse,
                        "the answer check must come first")

    def test_the_router_path_proposes_instead_of_switching(self):
        self.assertIn("elif mode_switch_needs_confirmation(cognitive_route):",
                      self.source)
        self.assertIn("propose_mode(", self.source)

    def test_declining_leaves_the_mode_alone(self):
        self.assertIn('if verdict == "decline":', self.source)


if __name__ == "__main__":
    unittest.main()
