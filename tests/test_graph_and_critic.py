# -*- coding: utf-8 -*-
"""
Tests for two bugs that both hid real failures behind confident output.

1. The consolidator wrote a fact, read it back with a case-sensitive string
   compare, and called a good write a failure. An entity keeps the name it was
   first stored under, so the second mention of "Transformer models" reads back
   as "transformer models". Worse, the mismatch raised inside the loop over the
   batch, so every remaining fact in that extraction was discarded -- and
   task_done() was skipped with it.

2. The critic returns "unverifiable" both when nothing could settle a claim and
   when the evidence a claim needed is simply absent. The agent treated both as
   acceptable, so a login that never happened was delivered as a list of job
   openings that did not exist.
"""

import os
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class CanonicalReadBackTests(unittest.TestCase):
    """The comparison must match how the store keys things."""

    def setUp(self):
        from memory.neo4j_store import _slug, canonical_predicate
        self.slug = _slug
        self.canon = canonical_predicate

    def compare(self, wrote, stored):
        """True if the new read-back accepts `stored` as proof of `wrote`."""
        want = (self.slug(wrote[0]), self.canon(wrote[1]), self.slug(wrote[2]))
        got = (self.slug(stored[0]), self.canon(stored[1]), self.slug(stored[2]))
        return want == got

    def test_the_exact_triple_from_the_research_log(self):
        # Written as "Transformer models", stored as "transformer models".
        self.assertTrue(self.compare(
            ("Transformer models", "USES", "attention mechanisms"),
            ("transformer models", "USES", "attention mechanisms")))

    def test_case_differs_on_the_object_too(self):
        self.assertTrue(self.compare(
            ("Transformer models", "RELATED_TO", "Computational Efficiency"),
            ("transformer models", "RELATED_TO", "computational efficiency")))

    def test_surrounding_whitespace_is_not_a_difference(self):
        self.assertTrue(self.compare(
            ("  Transformer  models ", "USES", "attention mechanisms"),
            ("transformer models", "USES", "attention mechanisms")))

    def test_a_genuinely_different_fact_is_still_rejected(self):
        # The check must not be so loose that it verifies anything.
        self.assertFalse(self.compare(
            ("Transformer models", "USES", "attention mechanisms"),
            ("transformer models", "USES", "convolution")))
        self.assertFalse(self.compare(
            ("Transformer models", "USES", "attention"),
            ("recurrent networks", "USES", "attention")))

    def test_predicate_aliases_are_resolved_on_both_sides(self):
        from memory.neo4j_store import PREDICATE_ALIASES
        if not PREDICATE_ALIASES:
            self.skipTest("no predicate aliases configured")
        alias, target = next(iter(PREDICATE_ALIASES.items()))
        self.assertTrue(self.compare(
            ("Thing", alias, "Other"), ("thing", target, "other")))


class ConsolidatorSourceTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        with open(os.path.join(ROOT, "memory", "consolidator.py"),
                  encoding="utf-8") as handle:
            cls.source = handle.read()

    def test_read_back_is_canonical(self):
        self.assertIn("canonical_predicate(edge_p)", self.source)
        self.assertIn("_slug(edge_s)", self.source)

    def test_a_miss_no_longer_discards_the_batch(self):
        self.assertNotIn("raise RuntimeError(f\"Neo4j read-back failed",
                         self.source)
        self.assertIn("unverified.append", self.source)

    def test_task_done_always_runs(self):
        # It used to sit after the loop, so an exception skipped it and any
        # queue.join() would wait forever on a task that had already ended.
        tail = self.source[self.source.find("except Exception as e:"):]
        self.assertIn("finally:", tail)
        self.assertIn("_consolidation_queue.task_done()", tail)
        self.assertEqual(self.source.count("_consolidation_queue.task_done()"), 1)

    def test_unverified_facts_are_still_reported(self):
        self.assertIn("could not be", self.source)

    def test_language_has_a_real_predicate_instead_of_becoming_a_contact(self):
        from memory.consolidator import normalize_personal_triple
        self.assertEqual(
            normalize_personal_triple(
                "I speak Telugu so I call my sister akka",
                "Himanshu", "HAS_CONTACT", "Telugu"),
            ("Himanshu", "SPEAKS_LANGUAGE", "Telugu"),
        )

    def test_language_normalisation_is_not_tied_to_one_language(self):
        from memory.consolidator import normalize_personal_triple
        self.assertEqual(
            normalize_personal_triple(
                "My native language is Spanish.",
                "Person", "RELATED_TO", "Spanish"),
            ("Person", "SPEAKS_LANGUAGE", "Spanish"),
        )


class FactProvenanceSourceTests(unittest.TestCase):
    def test_dialogue_sources_resolve_the_schema_that_is_actually_written(self):
        source = (Path(ROOT) / "memory" / "neo4j_store.py").read_text(
            encoding="utf-8")
        self.assertIn("t.turn_id=$id", source)
        self.assertIn("coalesce(t.content,t.user_text,t.text,'')", source)
        self.assertIn("t.role='user'", source)


class CriticDeliveryTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        with open(os.path.join(ROOT, "agent.py"), encoding="utf-8") as handle:
            cls.source = handle.read()

    def test_missing_evidence_triggers_a_correction_round(self):
        self.assertIn('verdict.verdict == "unverifiable" and verdict.missing',
                      self.source)

    def test_the_correction_uses_the_existing_bounded_loop(self):
        block = self.source[self.source.find('verdict.verdict == "unverifiable" and verdict.missing'):]
        block = block[:1800]
        self.assertIn("review_rounds < critic.MAX_REVIEWS", block)
        self.assertIn("critic.correction_prompt(verdict, plan)", block)

    def test_a_claim_with_nothing_missing_is_still_accepted(self):
        # A spoken answer has no receipt and never will; blocking those would
        # break ordinary conversation.
        self.assertIn('elif verdict.verdict == "unverifiable" and verdict.reason',
                      self.source)

    def test_the_final_note_names_what_is_unsupported(self):
        self.assertIn("No receipt supports:", self.source)

    def test_the_critic_still_distinguishes_three_verdicts(self):
        with open(os.path.join(ROOT, "critic.py"), encoding="utf-8") as handle:
            critic_source = handle.read()
        for verdict in ("pass", "fail", "unverifiable"):
            self.assertIn(f'"{verdict}"', critic_source)


if __name__ == "__main__":
    unittest.main()


class MatureDeliveryPipelineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(os.path.join(ROOT, "agent.py"), encoding="utf-8") as handle:
            cls.agent = handle.read()
        with open(os.path.join(ROOT, "cognitive_pipeline.py"), encoding="utf-8") as handle:
            cls.router = handle.read()

    def test_nontrivial_completion_is_deferred_until_after_review(self):
        deferred = self.agent.index(
            "defer_completion_delivery = completion_candidate and not plan.trivial")
        review = self.agent.index("verdict = critic.review(", deferred)
        report_bound = self.agent.index(
            "verified_report = (display_report if wants_report_surface",
            review)
        delivered = self.agent.index(
            "_deliver_verified_completion(", report_bound)
        self.assertLess(deferred, review)
        self.assertLess(review, report_bound)
        self.assertLess(review, delivered)
        self.assertIn("report_text=verified_report",
                      self.agent[delivered:delivered + 400])

    def test_action_steps_are_silent_until_final_result(self):
        self.assertIn('spoken_text = ""', self.agent)
        self.assertIn("Action responses are protocol, not conversation", self.agent)
        self.assertNotIn('spoken_text = f"On it, {_address()}."', self.agent)

    def test_router_demands_proportional_effort_and_selective_context(self):
        self.assertIn("Use planning_depth=direct for a single reversible action",
                      self.router)
        self.assertIn("each must change the answer to this turn", self.router)

    def test_verified_delivery_does_not_directly_export_dashboard_to_telegram(self):
        start = self.agent.index("def _deliver_verified_completion")
        end = self.agent.index("def _handle_social_acknowledgement", start)
        helper = self.agent[start:end]
        self.assertNotIn("send_telegram_message", helper)
