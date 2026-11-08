# -*- coding: utf-8 -*-
"""Conclusions that follow from facts nobody put side by side.

The graph is a star around Himanshu: in the real one, 647 of the two-hop paths
pass through him, and every one of them says only "two things are both his". A
chain is only worth reasoning over when it turns on something that is not
everyone's neighbour, nor a literal -- and what follows from it is the model's
to judge, with code checking the judgment is grounded.

Three properties carry the weight, and most tests here are about them:

  1. It never pivots on a hub or a value.
  2. A conclusion has to rest on the evidence it cites: at least two edges,
     an endpoint the chain actually contains, a shape that is a relation.
  3. It never promotes its own output, and never reasons over its own output.

The model is replaced by a fake throughout, and so is the graph.
"""

import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import deduction
import idle_gate


def row(s, p, o, c=1.0, fc="explicit_user_fact", st="Entity", ot="Entity"):
    return {"s": s, "sk": s.lower(), "p": p, "o": o, "ok": o.lower(),
            "c": c, "fc": fc, "src": "t", "st": st, "ot": ot}


def graph_of(*rows, **kwargs):
    return deduction.Graph(list(rows), **kwargs)


def hubbed(extra=()):
    """A graph with a hub: one node connected to thirty others."""
    rows = [row("Himanshu", "HAS_PREFERENCE", f"thing {n}") for n in range(30)]
    return graph_of(*rows, *extra)


class TheGraph(unittest.TestCase):
    def test_a_duplicate_edge_is_kept_once(self):
        g = graph_of(row("a", "R", "b"), row("a", "R", "b"))
        self.assertEqual(len(g.edges), 1)

    def test_a_self_loop_is_dropped(self):
        self.assertEqual(len(graph_of(row("a", "R", "a")).edges), 0)

    def test_degree_counts_both_directions(self):
        g = graph_of(row("a", "R", "b"), row("c", "R", "a"))
        self.assertEqual(g.degree("a"), 2)

    def test_a_busy_node_is_a_hub(self):
        g = hubbed()
        self.assertTrue(g.is_hub("himanshu"))
        self.assertFalse(g.is_hub("thing 1"))

    def test_literals_are_recognised_by_type_and_by_shape(self):
        g = graph_of(row("x", "HAS_PHONE", "+919000000000", ot="ProfileValue"),
                     row("x", "HAS_EMAIL", "a@b.com"),
                     row("x", "HAS_SITE", "https://example.org"))
        self.assertTrue(g.is_literal("+919000000000"))
        self.assertTrue(g.is_literal("a@b.com"))
        self.assertTrue(g.is_literal("https://example.org"))
        self.assertFalse(g.is_literal("x"))


class ChainsTurnOnOrdinaryNodes(unittest.TestCase):
    def pairs(self, g, **kw):
        return {(p.nodes[0], p.nodes[-1]) for p in
                deduction.enumerate_paths(g, **kw)}

    def test_a_simple_chain_is_found(self):
        g = graph_of(row("a", "R", "b"), row("b", "S", "c"))
        self.assertEqual(len(deduction.enumerate_paths(g)), 1)

    def test_a_chain_is_found_whichever_way_the_edges_point(self):
        """Two things converging on a node is as informative as a line."""
        g = graph_of(row("a", "R", "b"), row("c", "S", "b"))
        self.assertEqual(len(deduction.enumerate_paths(g)), 1)

    def test_it_never_pivots_on_a_hub(self):
        """Every one of those says only that two things are both his."""
        g = hubbed()
        self.assertEqual(deduction.enumerate_paths(g), [])

    def test_a_hub_may_still_be_an_endpoint(self):
        """He -> paper <- task is exactly the useful shape: the pivot is the
        paper, not him."""
        g = hubbed([row("Himanshu", "IS_WORKING_ON", "research paper"),
                    row("final pdf task", "RELATES_TO", "research paper")])
        found = deduction.enumerate_paths(g)
        self.assertTrue(any(p.pivots() == ("research paper",) and
                            "himanshu" in p.nodes for p in found))

    def test_it_never_pivots_on_a_value(self):
        g = graph_of(row("a", "HAS_PHONE", "+919000000000", ot="ProfileValue"),
                     row("b", "HAS_PHONE", "+919000000000", ot="ProfileValue"))
        self.assertEqual(deduction.enumerate_paths(g), [])

    def test_a_value_is_never_an_endpoint_either(self):
        g = graph_of(row("a", "R", "b"),
                     row("b", "HAS_PHONE", "+919000000000", ot="ProfileValue"))
        self.assertEqual(deduction.enumerate_paths(g), [])

    def test_the_same_chain_is_not_reported_twice(self):
        g = graph_of(row("a", "R", "b"), row("b", "S", "c"))
        keys = [p.key() for p in deduction.enumerate_paths(g)]
        self.assertEqual(len(keys), len(set(keys)))

    def test_a_chain_does_not_revisit_a_node(self):
        g = graph_of(row("a", "R", "b"), row("b", "S", "a"))
        self.assertEqual(deduction.enumerate_paths(g), [])

    def test_three_edge_chains_are_found(self):
        g = graph_of(row("a", "R", "b"), row("b", "S", "c"), row("c", "T", "d"))
        lengths = {len(p.edges) for p in deduction.enumerate_paths(g)}
        self.assertEqual(lengths, {2, 3})

    def test_no_chain_is_longer_than_the_limit(self):
        edges = [row(f"n{i}", "R", f"n{i + 1}") for i in range(10)]
        for path in deduction.enumerate_paths(graph_of(*edges)):
            self.assertLessEqual(len(path.edges), deduction.MAX_HOPS)

    def test_the_traversal_is_bounded_whatever_the_graph_holds(self):
        """A graph ten times larger must not turn a pass into an hour."""
        edges = [row(f"n{i}", "R", f"n{(i * 7 + 3) % 400}") for i in range(400)]
        deduction.enumerate_paths(graph_of(*edges), max_expansions=500)


class WhichChainsAreWorthAsking(unittest.TestCase):
    def graph_and_path(self, *rows):
        g = graph_of(*rows)
        return g, deduction.enumerate_paths(g)[0]

    def test_chains_of_nothing_but_related_to_score_nothing(self):
        g, path = self.graph_and_path(row("a", "RELATED_TO", "b"),
                                      row("b", "RELATED_TO", "c"))
        self.assertEqual(deduction.score(path, g), 0.0)

    def test_varied_relations_score_higher(self):
        g1, p1 = self.graph_and_path(row("a", "IS_A", "b"), row("b", "IS_A", "c"))
        g2, p2 = self.graph_and_path(row("a", "IS_A", "b"), row("b", "USES", "c"))
        self.assertGreater(deduction.score(p2, g2), deduction.score(p1, g1))

    def test_what_he_said_outweighs_what_was_observed(self):
        g1, p1 = self.graph_and_path(
            row("a", "X", "b", fc="external_observation"),
            row("b", "Y", "c", fc="external_observation"))
        g2, p2 = self.graph_and_path(row("a", "X", "b"), row("b", "Y", "c"))
        self.assertGreater(deduction.score(p2, g2), deduction.score(p1, g1))

    def test_two_things_already_linked_gain_little(self):
        g_linked = graph_of(row("a", "X", "b"), row("b", "Y", "c"),
                            row("a", "Z", "c"))
        g_open = graph_of(row("a", "X", "b"), row("b", "Y", "c"))
        linked = next(p for p in deduction.enumerate_paths(g_linked)
                      if set(p.nodes) == {"a", "b", "c"} and len(p.edges) == 2
                      and {e.predicate for e in p.edges} == {"X", "Y"})
        opened = deduction.enumerate_paths(g_open)[0]
        self.assertLess(deduction.score(linked, g_linked),
                        deduction.score(opened, g_open))

    def test_a_longer_chain_is_weaker_evidence(self):
        g = graph_of(row("a", "X", "b"), row("b", "Y", "c"), row("c", "Z", "d"))
        paths = deduction.enumerate_paths(g)
        two = max((p for p in paths if len(p.edges) == 2),
                  key=lambda p: deduction.score(p, g))
        three = next(p for p in paths if len(p.edges) == 3)
        self.assertGreater(deduction.score(two, g), deduction.score(three, g))

    def test_no_single_pivot_dominates_a_pass(self):
        edges = [row(f"leaf {n}", f"P{n % 3}", "centre") for n in range(8)]
        g = graph_of(*edges)
        chosen = deduction.pick(deduction.enumerate_paths(g), g, {})
        self.assertLessEqual(len(chosen), deduction.MAX_PER_PIVOT)

    def test_a_pass_is_bounded(self):
        edges = []
        for n in range(40):
            edges += [row(f"x{n}", "IS_A", f"mid{n}"),
                      row(f"mid{n}", "USES", f"y{n}")]
        g = graph_of(*edges)
        self.assertLessEqual(len(deduction.pick(
            deduction.enumerate_paths(g), g, {})), deduction.MAX_PATHS)


class WhatHasAlreadyBeenAsked(unittest.TestCase):
    def setUp(self):
        g = graph_of(row("a", "X", "b"), row("b", "Y", "c"))
        self.path = deduction.enumerate_paths(g)[0]

    def test_a_new_chain_is_allowed(self):
        self.assertTrue(deduction.ledger_allows({}, self.path))

    def test_a_chain_just_asked_about_waits(self):
        ledger = {}
        deduction.note_checked(ledger, self.path, False)
        self.assertFalse(deduction.ledger_allows(ledger, self.path))

    def test_it_may_be_asked_again_after_the_cooldown(self):
        import datetime
        ledger = {}
        long_ago = (datetime.datetime.now()
                    - datetime.timedelta(hours=deduction.COOLDOWN_HOURS + 1))
        deduction.note_checked(ledger, self.path, True, now=long_ago)
        self.assertTrue(deduction.ledger_allows(ledger, self.path))

    def test_a_chain_that_never_yields_anything_is_given_up_on(self):
        import datetime
        ledger = {}
        for hours in range(deduction.MAX_CHECKS):
            deduction.note_checked(
                ledger, self.path, False,
                now=datetime.datetime.now() - datetime.timedelta(days=30))
        self.assertFalse(deduction.ledger_allows(ledger, self.path))

    def test_a_chain_that_did_conclude_is_kept_for_corroboration(self):
        import datetime
        ledger = {}
        for _ in range(deduction.MAX_CHECKS):
            deduction.note_checked(
                ledger, self.path, True,
                now=datetime.datetime.now() - datetime.timedelta(days=30))
        self.assertTrue(deduction.ledger_allows(ledger, self.path))

    def test_the_key_is_independent_of_direction(self):
        g1 = graph_of(row("a", "X", "b"), row("b", "Y", "c"))
        g2 = graph_of(row("b", "Y", "c"), row("a", "X", "b"))
        self.assertEqual(deduction.enumerate_paths(g1)[0].key(),
                         deduction.enumerate_paths(g2)[0].key())


def concluding(chain, edges, subject, predicate, obj, statement="a statement",
               confidence=0.8):
    return {"chain": chain, "edges": edges, "subject": subject,
            "predicate": predicate, "object": obj, "statement": statement,
            "confidence": confidence}


class AConclusionHasToBeGrounded(unittest.TestCase):
    def setUp(self):
        g = graph_of(
            row("Nikhila", "HAS_ALIAS", "Akka"),
            row("nikhila usa", "POSSIBLY_SAME_PERSON_AS", "Akka"))
        self.paths = [deduction.enumerate_paths(g)[0]]

    def parse(self, *items):
        return deduction.parse(json.dumps({"conclusions": list(items)}),
                               self.paths)

    def test_a_sound_conclusion_is_kept(self):
        kept = self.parse(concluding(1, [1, 2], "Nikhila",
                                     "POSSIBLY_SAME_PERSON_AS", "nikhila usa"))
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0]["subject"], "Nikhila")

    def test_one_edge_is_not_enough(self):
        """That is just the edge restated."""
        self.assertEqual(self.parse(concluding(
            1, [1], "Nikhila", "KNOWN_AS", "Akka")), [])

    def test_the_same_edge_cited_twice_is_still_one(self):
        self.assertEqual(self.parse(concluding(
            1, [1, 1], "Nikhila", "KNOWN_AS", "Akka")), [])

    def test_citing_edges_that_do_not_exist_is_not_citing_them(self):
        self.assertEqual(self.parse(concluding(
            1, [1, 7], "Nikhila", "KNOWN_AS", "Akka")), [])

    def test_an_unknown_chain_is_ignored(self):
        self.assertEqual(self.parse(concluding(
            9, [1, 2], "Nikhila", "X_Y_Z", "nikhila usa")), [])

    def test_a_conclusion_about_things_not_in_the_chain_is_dropped(self):
        """A deduction that mentions things the evidence does not is a guess
        dressed as logic."""
        self.assertEqual(self.parse(concluding(
            1, [1, 2], "Elvis", "IS_ALIVE_AS", "a rumour")), [])

    def test_one_grounded_endpoint_is_enough(self):
        kept = self.parse(concluding(1, [1, 2], "Nikhila", "ALSO_LISTED_AS",
                                     "a duplicate contact entry"))
        self.assertEqual(len(kept), 1)

    def test_restating_an_existing_edge_is_dropped(self):
        self.assertEqual(self.parse(concluding(
            1, [1, 2], "Nikhila", "KNOWN_AS", "Akka")), [])

    def test_a_predicate_has_to_look_like_a_relation(self):
        for bad in ("is probably the same person", "x", "DROP TABLE; --",
                    "", "a-b-c", "REL" + "_X" * 30):
            with self.subTest(predicate=bad):
                self.assertEqual(self.parse(concluding(
                    1, [1, 2], "Nikhila", bad, "nikhila usa")), [])

    def test_a_relation_is_a_name_not_a_sentence(self):
        """"IS_PROBABLY_THE_SAME_PERSON" is a claim wearing a predicate."""
        self.assertEqual(self.parse(concluding(
            1, [1, 2], "Nikhila", "IS_PROBABLY_THE_SAME_PERSON",
            "nikhila usa")), [])
        self.assertEqual(len(self.parse(concluding(
            1, [1, 2], "Nikhila", "POSSIBLY_SAME_PERSON_AS",
            "nikhila usa"))), 1)

    def test_lowercase_is_normalised_not_refused(self):
        kept = self.parse(concluding(1, [1, 2], "Nikhila", "listed_as",
                                     "nikhila usa"))
        self.assertEqual(kept[0]["predicate"], "LISTED_AS")

    def test_a_spaced_predicate_is_normalised(self):
        kept = self.parse(concluding(1, [1, 2], "Nikhila", "same as",
                                     "nikhila usa"))
        self.assertEqual(kept[0]["predicate"], "SAME_AS")

    def test_an_empty_statement_is_dropped(self):
        self.assertEqual(self.parse(concluding(
            1, [1, 2], "Nikhila", "SAME_AS", "nikhila usa", statement="")), [])

    def test_the_same_conclusion_from_two_chains_is_one(self):
        """The first dry run produced one twice, which would have counted as
        corroboration of itself."""
        g = graph_of(row("a", "X", "b"), row("b", "Y", "c"),
                     row("a", "X2", "d"), row("d", "Y2", "c"))
        paths = deduction.enumerate_paths(g)[:2]
        raw = json.dumps({"conclusions": [
            concluding(1, [1, 2], "a", "LINKED_TO", "c"),
            concluding(2, [1, 2], "a", "LINKED_TO", "c")]})
        self.assertEqual(len(deduction.parse(raw, paths)), 1)

    def test_garbage_from_the_model_is_survived(self):
        for raw in ("", "no json here", '{"conclusions": "none"}',
                    '{"conclusions": [1, 2, 3]}', "null"):
            with self.subTest(raw=raw):
                self.assertEqual(deduction.parse(raw, self.paths), [])

    def test_the_number_kept_is_bounded(self):
        g = graph_of(*[row(f"s{n}", "X", f"m{n}") for n in range(20)],
                     *[row(f"m{n}", "Y", f"e{n}") for n in range(20)])
        paths = deduction.enumerate_paths(g)[:20]
        raw = json.dumps({"conclusions": [
            concluding(i + 1, [1, 2], paths[i].nodes[0], f"REL_{i}_X",
                       f"obj {i}") for i in range(len(paths))]})
        self.assertLessEqual(len(deduction.parse(raw, paths)),
                             deduction.MAX_CONCLUSIONS)


class ConfidenceIsCapped(unittest.TestCase):
    def conclusion(self, rows, stated=1.0):
        g = graph_of(*rows)
        path = deduction.enumerate_paths(g)[0]
        raw = json.dumps({"conclusions": [concluding(
            1, [1, 2], path.nodes[0], "LINKED_TO", path.nodes[-1],
            confidence=stated)]})
        return deduction.parse(raw, [path])[0]["confidence"]

    def test_never_above_the_ceiling(self):
        self.assertLessEqual(
            self.conclusion([row("a", "X", "b"), row("b", "Y", "c")]),
            deduction.CONFIDENCE_CAP)

    def test_the_ceiling_is_below_auto_promotion(self):
        """A deduction is always something to confirm, never something simply
        believed."""
        import inference
        self.assertLess(deduction.CONFIDENCE_CAP,
                        inference.AUTO_PROMOTE_CONFIDENCE)

    def test_never_above_the_weakest_edge(self):
        self.assertEqual(self.conclusion(
            [row("a", "X", "b", c=0.4), row("b", "Y", "c", c=0.9)]), 0.4)

    def test_never_above_the_models_own_doubt(self):
        self.assertEqual(self.conclusion(
            [row("a", "X", "b"), row("b", "Y", "c")], stated=0.3), 0.3)

    def test_observation_only_chains_are_capped_lower(self):
        """Nothing in it was said by him."""
        self.assertLessEqual(self.conclusion(
            [row("a", "X", "b", fc="external_observation"),
             row("b", "Y", "c", fc="external_observation")]),
            deduction.OBSERVED_ONLY_CAP)

    def test_a_missing_or_absurd_confidence_is_survived(self):
        g = graph_of(row("a", "X", "b"), row("b", "Y", "c"))
        path = deduction.enumerate_paths(g)[0]
        for stated in ("high", None, -4, 99):
            raw = json.dumps({"conclusions": [{
                "chain": 1, "edges": [1, 2], "subject": "a",
                "predicate": "LINKED_TO", "object": "c",
                "statement": "s", "confidence": stated}]})
            kept = deduction.parse(raw, [path])
            self.assertTrue(0.0 < kept[0]["confidence"] <= 1.0, stated)


class ItReadsOnlyWhatHeSaid(unittest.TestCase):
    def test_derived_inferences_are_not_a_source(self):
        """An engine that reasons over its own conclusions becomes certain of
        things nobody said."""
        self.assertNotIn("derived_inference", deduction.SOURCE_CLASSES)

    def test_the_query_asks_only_for_the_allowed_classes(self):
        seen = {}

        class Store:
            def query(self, cypher, **params):
                seen.update(params)
                return []
        deduction.load_graph(Store())
        self.assertEqual(set(seen["classes"]), set(deduction.SOURCE_CLASSES))

    def test_what_a_web_page_said_is_allowed_but_marked_lower(self):
        self.assertIn("external_observation", deduction.SOURCE_CLASSES)
        self.assertNotIn("external_observation", deduction.STATED_CLASSES)


class FakeStore:
    def __init__(self, rows):
        self.rows = rows
        self.proposed = []

    def query(self, cypher, **params):
        return self.rows

    def propose_inference(self, subject, predicate, obj, rationale,
                          confidence=0.5, source_id=""):
        self.proposed.append({"s": subject, "p": predicate, "o": obj,
                              "why": rationale, "c": confidence,
                              "src": source_id})
        return f"{subject} -{predicate}-> {obj}"


class APass(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.base = self._dir.name
        patcher = patch.object(idle_gate, "interrupted", return_value=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.rows = [row("Nikhila", "HAS_ALIAS", "Akka"),
                     row("nikhila usa", "POSSIBLY_SAME_PERSON_AS", "Akka")]

    def llm(self, reply):
        calls = []

        def call(messages, task_level=None):
            calls.append(messages)
            return reply
        call.calls = calls
        return call

    def test_a_sound_pass_proposes_with_its_reasoning(self):
        store = FakeStore(self.rows)
        reply = json.dumps({"conclusions": [concluding(
            1, [1, 2], "Nikhila", "SAME_AS", "nikhila usa",
            statement="both entries are the person called Akka")]})
        out = deduction.run_pass(self.base, self.llm(reply), store=store)
        self.assertEqual(len(out), 1)
        proposal = store.proposed[0]
        self.assertIn("Deduced", proposal["why"])
        self.assertIn("HAS_ALIAS", proposal["why"])
        self.assertLessEqual(proposal["c"], deduction.CONFIDENCE_CAP)
        self.assertTrue(proposal["src"].startswith("deduction:"))

    def test_nothing_to_reason_over_costs_no_model_call(self):
        llm = self.llm("{}")
        out = deduction.run_pass(self.base, llm, store=FakeStore([]))
        self.assertEqual(out, [])
        self.assertEqual(llm.calls, [])

    def test_one_call_per_pass(self):
        llm = self.llm('{"conclusions": []}')
        deduction.run_pass(self.base, llm, store=FakeStore(self.rows))
        self.assertEqual(len(llm.calls), 1)

    def test_the_chains_are_what_the_model_sees(self):
        llm = self.llm('{"conclusions": []}')
        deduction.run_pass(self.base, llm, store=FakeStore(self.rows))
        prompt = llm.calls[0][1]["content"]
        self.assertIn("HAS_ALIAS", prompt)
        self.assertIn("he said", prompt)

    def test_the_model_is_told_most_chains_conclude_nothing(self):
        self.assertIn("empty list is a good answer", deduction._PROMPT)

    def test_it_is_told_not_to_swap_a_name_for_its_alias(self):
        self.assertIn("alias", deduction._PROMPT)

    def test_it_is_told_not_to_infer_sensitive_traits(self):
        self.assertIn("sensitive trait", deduction._PROMPT)

    def test_asked_chains_are_remembered(self):
        deduction.run_pass(self.base, self.llm('{"conclusions": []}'),
                           store=FakeStore(self.rows))
        self.assertEqual(len(deduction.load_ledger(self.base)), 1)

    def test_a_chain_just_asked_about_is_not_asked_again_at_once(self):
        deduction.run_pass(self.base, self.llm('{"conclusions": []}'),
                           store=FakeStore(self.rows))
        llm = self.llm('{"conclusions": []}')
        deduction.run_pass(self.base, llm, store=FakeStore(self.rows))
        self.assertEqual(llm.calls, [])

    def test_a_failing_model_costs_nothing_and_raises_nothing(self):
        def broken(messages, task_level=None):
            raise RuntimeError("provider down")
        self.assertEqual(deduction.run_pass(
            self.base, broken, store=FakeStore(self.rows)), [])

    def test_a_failing_graph_costs_nothing_and_raises_nothing(self):
        class Dead:
            def query(self, *a, **k):
                raise ConnectionError("neo4j down")
        self.assertEqual(deduction.run_pass(
            self.base, self.llm("{}"), store=Dead()), [])

    def test_he_coming_back_stops_it_before_the_call(self):
        llm = self.llm("{}")
        with patch.object(idle_gate, "interrupted", return_value=True):
            self.assertEqual(deduction.run_pass(
                self.base, llm, store=FakeStore(self.rows)), [])
        self.assertEqual(llm.calls, [])

    def test_it_rides_on_the_hourly_worker(self):
        import inspect
        import inference
        source = inspect.getsource(inference.start_worker)
        self.assertIn("deduction", source)


if __name__ == "__main__":
    unittest.main()
