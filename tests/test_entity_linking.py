# -*- coding: utf-8 -*-
"""Joining what his separate worlds say about the same thing.

Run against the real data, the first version of this produced four defects
nobody would have written a fixture for:

  * a calendar entry about the city of Chennai was linked to a contact who is
    literally named "Chennai"
  * "Himanshu" matched most of the inbox -- he is every email's recipient
  * a project was recorded as referring to itself
  * the contact sync, which feeds all of it, kept only contacts with a phone
    number and asked for no emails at all

So these tests are mostly about what a link must NOT be. The principle behind
the fixes: an unambiguous match (a full name, a project's name) is an
observation and may be written; an ambiguous one (a single word) is a
question, and goes to the model. And nothing here may alter an entity that
already exists -- `upsert_entity` overwrites a node's type and aliases, and a
contact reset by a linking pass would be corruption nobody noticed for weeks.

The graph, the web and the model are all replaced with fakes.
"""

import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import entity_linking as L
import idle_gate


def item(silo, ident, title, body="", **kw):
    return L.Item(silo, ident, title, body, **kw)


def filler(count=8):
    """Unrelated mail, so an item pool is the size a real one is.

    Rarity is measured against the pool: in a pool of two, every shared word
    is "rare" and the measure means nothing. The real pool is thirty or more.
    """
    return [item(L.SILO_EMAIL, f"filler{n}",
                 f"Newsletter number {n} about topic{n} and subject{n}")
            for n in range(count)]


def names(*values, kind="person"):
    return L.build_targets(**{{"person": "persons", "concept": "concepts",
                               "project": "projects"}[kind]: values})


class WhatCountsAsAName(unittest.TestCase):
    def test_a_real_name_is_usable(self):
        self.assertTrue(L.usable_name("Srimaan Gajjelli"))
        self.assertTrue(L.usable_name("Mavo"))

    def test_too_short_is_not(self):
        for name in ("", "ab", "I", "me"):
            self.assertFalse(L.usable_name(name), name)

    def test_a_value_is_not_a_name(self):
        for name in ("a@b.com", "+919000000000", "https://example.org"):
            self.assertFalse(L.usable_name(name), name)

    def test_a_name_of_only_stop_words_is_not(self):
        self.assertFalse(L.usable_name("the and for"))

    def test_duplicates_are_collapsed(self):
        targets = L.build_targets(persons=["Rahul Verma", "rahul  verma"])
        self.assertEqual(len(targets), 1)


class WhetherAMatchIsEnoughToWrite(unittest.TestCase):
    """Strong matches are observations; weak ones are questions."""

    def kinds(self, name, kind):
        return L.Target(name, kind).strong()

    def test_a_project_name_is_strong(self):
        self.assertTrue(self.kinds("Mavo", "project"))

    def test_a_full_name_is_strong(self):
        self.assertTrue(self.kinds("Srimaan Gajjelli", "person"))

    def test_a_single_name_is_weak(self):
        """The real address book has a contact called "Chennai"."""
        self.assertFalse(self.kinds("Chennai", "person"))
        self.assertFalse(self.kinds("Rahul", "person"))

    def test_a_short_single_word_concept_is_weak(self):
        self.assertFalse(self.kinds("Graph", "concept"))

    def test_a_long_distinctive_concept_is_strong(self):
        self.assertTrue(self.kinds("backpropagation", "concept"))

    def test_a_multiword_concept_is_strong(self):
        self.assertTrue(self.kinds("Self-attention", "concept")
                        or self.kinds("multi head attention", "concept"))


class Mentions(unittest.TestCase):
    def test_a_name_in_a_subject_is_found(self):
        items = [item(L.SILO_EMAIL, "1", "Congratulate Srimaan Gajjelli on ...")]
        hits = L.find_mentions(items, names("Srimaan Gajjelli"))
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0][2].lower(), "srimaan gajjelli")

    def test_whole_words_only(self):
        """"Mavo" must not match "Mavolution"."""
        items = [item(L.SILO_EMAIL, "1", "The Mavolution begins")]
        self.assertEqual(L.find_mentions(items, names("Mavo", kind="project")), [])

    def test_case_and_separators_do_not_matter(self):
        items = [item(L.SILO_TASK, "1", "check FINAL-PDF paper")]
        self.assertEqual(len(L.find_mentions(
            items, names("final pdf", kind="concept"))), 1)

    def test_the_account_owner_is_not_a_target(self):
        """He is every email's recipient; matching him meant nothing."""
        queries = []

        class Store:
            def query(self, cypher, **params):
                queries.append((cypher, params))
                return [{"name": "madhu", "aliases": []}]
        L.load_persons(Store())
        cypher, params = queries[0]
        self.assertIn("degree <= $hub", cypher)
        self.assertEqual(params["hub"], L.HUB_DEGREE)

    def test_a_project_does_not_refer_to_itself(self):
        items = [item(L.SILO_PROJECT, "Mavo", "Mavo", "a personal project")]
        self.assertEqual(L.find_mentions(
            items, names("Mavo", kind="project")), [])

    def test_a_person_in_a_projects_readme_is_ignored(self):
        """Author names in a file are not people he is dealing with."""
        items = [item(L.SILO_PROJECT, "Mavo", "Mavo",
                      "written by Srimaan Gajjelli")]
        self.assertEqual(L.find_mentions(
            items, names("Srimaan Gajjelli")), [])

    def test_a_name_on_almost_everything_is_a_word_not_an_identity(self):
        """Judged against the items actually present, so it adapts to what he
        has rather than to a list of words someone thought of."""
        items = [item(L.SILO_EMAIL, str(n), f"Graph weekly digest {n}")
                 for n in range(10)]
        items.append(item(L.SILO_EMAIL, "x", "Graph Neural Networks reading"))
        hits = L.find_mentions(items, names("Graph", kind="concept"))
        self.assertEqual(hits, [])

    def test_a_distinctive_name_survives_the_generic_filter(self):
        items = [item(L.SILO_EMAIL, str(n), f"Weekly digest {n}")
                 for n in range(10)]
        items.append(item(L.SILO_EMAIL, "x", "Overleaf project shared"))
        self.assertEqual(len(L.find_mentions(
            items, names("Overleaf", kind="project"))), 1)

    def test_a_small_pool_is_not_filtered(self):
        """With three items, a name on two of them is not "generic"."""
        items = [item(L.SILO_EMAIL, "1", "Mavo update"),
                 item(L.SILO_EVENT, "2", "Mavo demo"),
                 item(L.SILO_TASK, "3", "buy milk")]
        self.assertEqual(len(L.find_mentions(
            items, names("Mavo", kind="project"))), 2)


class CandidatePairs(unittest.TestCase):
    def test_two_items_about_the_same_thing_pair_up(self):
        items = [item(L.SILO_EVENT, "1", "Apply for Chennai gate pass"),
                 item(L.SILO_TASK, "2", "Chennai gate pass application"),
                 item(L.SILO_EMAIL, "3", "Your weekly digest"),
                 item(L.SILO_EMAIL, "4", "Offers on shoes")]
        pairs = L.candidate_pairs(items)
        self.assertEqual(len(pairs), 1)
        _w, first, second, shared = pairs[0]
        self.assertEqual({first.silo, second.silo},
                         {L.SILO_EVENT, L.SILO_TASK})
        self.assertIn("chennai", shared)

    def test_a_pool_of_two_pairs_nothing(self):
        """Conservative by construction: with two items every shared word is
        'rare', which is no information. A missed link costs nothing."""
        items = [item(L.SILO_EVENT, "1", "Apply for Chennai gate pass"),
                 item(L.SILO_TASK, "2", "Chennai gate pass application")]
        self.assertEqual(L.candidate_pairs(items), [])

    def test_never_within_one_silo(self):
        """Two emails about one sender are a thread, not a link between
        worlds."""
        items = [item(L.SILO_EMAIL, "1", "Deloitte interview schedule"),
                 item(L.SILO_EMAIL, "2", "Deloitte interview reminder")]
        self.assertEqual(L.candidate_pairs(items), [])

    def test_one_common_word_is_not_enough(self):
        items = [item(L.SILO_EVENT, "1", "Weekly report review"),
                 item(L.SILO_TASK, "2", "Send the report"),
                 item(L.SILO_EMAIL, "3", "Monthly report"),
                 item(L.SILO_PROJECT, "4", "Annual report")]
        self.assertEqual(L.candidate_pairs(items), [])

    def test_rare_shared_words_outweigh_common_ones(self):
        common = [item(L.SILO_EMAIL, str(n), f"Update number {n} report")
                  for n in range(8)]
        pair = [item(L.SILO_EVENT, "e", "Hyperverge interview report"),
                item(L.SILO_TASK, "t", "Hyperverge interview prep")]
        pairs = L.candidate_pairs(common + pair)
        self.assertEqual(len(pairs), 1)
        self.assertIn("hyperverge", pairs[0][3])

    def test_the_list_is_bounded(self):
        items = []
        for n in range(40):
            items += [item(L.SILO_EVENT, f"e{n}", f"unique{n} subject{n} alpha"),
                      item(L.SILO_TASK, f"t{n}", f"unique{n} subject{n} beta")]
        self.assertLessEqual(len(L.candidate_pairs(items)),
                             L.MAX_PAIRS_ASKED)


class Verdicts(unittest.TestCase):
    def setUp(self):
        a = item(L.SILO_EVENT, "1", "Apply for gate pass")
        b = item(L.SILO_TASK, "2", "Gate pass application")
        self.questions = L.questions_from([(5.0, a, b, ["gate", "pass"])], [])

    def parse(self, **fields):
        base = {"pair": 1, "verdict": "same", "reason": "same obligation",
                "confidence": 0.9}
        base.update(fields)
        return L.parse_verdicts(json.dumps({"pairs": [base]}), self.questions)

    def test_a_same_verdict_becomes_same_concept_as(self):
        self.assertEqual(self.parse()[0]["predicate"], "SAME_CONCEPT_AS")

    def test_a_related_verdict_becomes_related_to(self):
        self.assertEqual(self.parse(verdict="related")[0]["predicate"],
                         "RELATED_TO")

    def test_unrelated_is_dropped(self):
        self.assertEqual(self.parse(verdict="unrelated"), [])

    def test_an_unknown_verdict_is_dropped(self):
        for bad in ("maybe", "", "yes", "definitely the same"):
            with self.subTest(verdict=bad):
                self.assertEqual(self.parse(verdict=bad), [])

    def test_the_verdict_is_read_case_and_whitespace_insensitively(self):
        self.assertEqual(len(self.parse(verdict=" SAME ")), 1)

    def test_confidence_is_capped_below_auto_promotion(self):
        import inference
        self.assertLess(self.parse(confidence=1.0)[0]["confidence"],
                        inference.AUTO_PROMOTE_CONFIDENCE)

    def test_a_missing_confidence_is_survived(self):
        self.assertTrue(0.3 <= self.parse(confidence="high")[0]["confidence"]
                        <= L.VERDICT_CAP)

    def test_a_pair_number_out_of_range_is_ignored(self):
        self.assertEqual(self.parse(pair=9), [])
        self.assertEqual(self.parse(pair="x"), [])

    def test_garbage_is_survived(self):
        for raw in ("", "nope", '{"pairs": "none"}', "null"):
            self.assertEqual(L.parse_verdicts(raw, self.questions), [])

    def test_an_item_that_is_a_person_refers_to_them(self):
        """"Same concept as" about an email and a human would be nonsense."""
        email = item(L.SILO_EMAIL, "1", "Project ownership transfer",
                     extra={})
        target = L.Target("Rahul", "person")
        questions = L.questions_from([], [(email, target, "RAHUL")])
        verdict = L.parse_verdicts(json.dumps({"pairs": [
            {"pair": 1, "verdict": "same", "reason": "r",
             "confidence": 0.8}]}), questions)[0]
        self.assertEqual(verdict["predicate"], "REFERS_TO")


class FakeStore:
    """Records every write, and refuses the one that would corrupt a node."""

    def __init__(self, persons=(), concepts=()):
        self.persons = list(persons)
        self.concepts = list(concepts)
        self.facts, self.proposed, self.queries = [], [], []

    def query(self, cypher, **params):
        self.queries.append((cypher, params))
        if "entity_type IN ['Person'" in cypher:
            return [{"name": n, "aliases": []} for n in self.persons]
        if "degree >= 2" in cypher:
            return [{"name": n} for n in self.concepts]
        return []

    def upsert_entity(self, *a, **k):
        raise AssertionError(
            "upsert_entity overwrites an existing node's type and aliases")

    def add_fact(self, subject, predicate, obj, **meta):
        self.facts.append({"s": subject, "p": predicate, "o": obj, **meta})
        return f"Added fact: {subject} -{predicate}-> {obj}"

    def propose_inference(self, subject, predicate, obj, rationale,
                          confidence=0.5, source_id=""):
        self.proposed.append({"s": subject, "p": predicate, "o": obj,
                              "why": rationale, "c": confidence})
        return f"{subject} -{predicate}-> {obj}"


class APass(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.base = self._dir.name
        patcher = patch.object(idle_gate, "interrupted", return_value=False)
        patcher.start()
        self.addCleanup(patcher.stop)

    def llm(self, reply="{}"):
        calls = []

        def call(messages, task_level=None):
            calls.append(messages)
            return reply
        call.calls = calls
        return call

    def run_(self, store, items, targets=None, llm=None):
        if targets is None:
            targets = L.build_targets(
                persons=store.persons, concepts=store.concepts,
                projects=[i.title for i in items if i.silo == L.SILO_PROJECT])
        return L.run_pass(self.base, llm, store=store, items=items,
                          targets=targets)

    def test_a_full_name_mention_is_recorded_as_an_observation(self):
        store = FakeStore(persons=["Srimaan Gajjelli"])
        items = [item(L.SILO_EMAIL, "m1", "Congratulate Srimaan Gajjelli")]
        out = self.run_(store, items)
        self.assertEqual(out["mentions"], 1)
        fact = store.facts[0]
        self.assertEqual(fact["p"], "REFERS_TO")
        self.assertEqual(fact["o"], "Srimaan Gajjelli")
        self.assertEqual(fact["fact_class"], "external_observation")
        self.assertIn("srimaan gajjelli", fact["evidence_quote"].lower())

    def test_an_observation_is_not_a_belief_about_him(self):
        store = FakeStore(persons=["Srimaan Gajjelli"])
        self.run_(store, [item(L.SILO_EMAIL, "m1", "Srimaan Gajjelli posted")])
        self.assertLess(store.facts[0]["authority"], 70)
        self.assertFalse(store.facts[0].get("autonomy_eligible", False))

    def test_a_single_name_is_not_written_but_asked_about(self):
        """The contact called "Chennai" and the city of Chennai."""
        store = FakeStore(persons=["Chennai"])
        items = [item(L.SILO_EVENT, "e1", "Apply for Chennai gate pass")]
        llm = self.llm(json.dumps({"pairs": [{
            "pair": 1, "verdict": "unrelated", "confidence": 0.9,
            "reason": "the city, not a contact"}]}))
        out = self.run_(store, items, llm=llm)
        self.assertEqual(out["mentions"], 0)
        self.assertEqual(store.facts, [])
        self.assertEqual(len(llm.calls), 1)
        self.assertIn("Chennai", llm.calls[0][1]["content"])

    def test_and_the_model_can_confirm_one(self):
        store = FakeStore(persons=["Rahul"])
        items = [item(L.SILO_EMAIL, "m1", "Project ownership transfer",
                      "Rahul has transferred the project to you")]
        llm = self.llm(json.dumps({"pairs": [{
            "pair": 1, "verdict": "same", "confidence": 0.8,
            "reason": "the email is from the contact"}]}))
        out = self.run_(store, items, llm=llm)
        self.assertEqual(out["proposed"], 1)
        self.assertEqual(store.proposed[0]["p"], "REFERS_TO")
        self.assertEqual(store.proposed[0]["o"], "Rahul")

    def test_a_judgment_is_a_pending_proposal_with_a_reason(self):
        store = FakeStore(persons=["Rahul"])
        items = [item(L.SILO_EMAIL, "m1", "Rahul wrote")]
        llm = self.llm(json.dumps({"pairs": [{
            "pair": 1, "verdict": "same", "confidence": 0.8,
            "reason": "the sender is that contact"}]}))
        self.run_(store, items, llm=llm)
        self.assertIn("the sender is that contact", store.proposed[0]["why"])
        self.assertIn("Cross-silo", store.proposed[0]["why"])

    def test_a_project_is_not_recorded_as_referring_to_itself(self):
        store = FakeStore()
        out = self.run_(store, [item(L.SILO_PROJECT, "Mavo", "Mavo")])
        self.assertEqual(out["mentions"], 0)

    def test_no_model_call_when_there_is_nothing_to_ask(self):
        store = FakeStore(persons=["Srimaan Gajjelli"])
        llm = self.llm()
        self.run_(store, [item(L.SILO_EMAIL, "m1", "Srimaan Gajjelli posted")],
                  llm=llm)
        self.assertEqual(llm.calls, [])

    def test_two_items_about_one_thing_are_put_to_the_model(self):
        store = FakeStore()
        items = [item(L.SILO_EVENT, "e1", "Apply for Chennai gate pass"),
                 item(L.SILO_TASK, "t1", "Chennai gate pass application")
                 ] + filler()
        llm = self.llm(json.dumps({"pairs": [{
            "pair": 1, "verdict": "same", "confidence": 0.9,
            "reason": "the same obligation"}]}))
        out = self.run_(store, items, llm=llm)
        self.assertEqual(out["proposed"], 1)
        self.assertEqual(store.proposed[0]["p"], "SAME_CONCEPT_AS")

    def test_it_never_calls_upsert_entity(self):
        """FakeStore raises if it does. That method SETs name, type and
        aliases, so on a node that already exists it would quietly reset a
        contact."""
        store = FakeStore()
        items = [item(L.SILO_EVENT, "e1", "Apply for Chennai gate pass"),
                 item(L.SILO_TASK, "t1", "Chennai gate pass application")
                 ] + filler()
        llm = self.llm(json.dumps({"pairs": [{
            "pair": 1, "verdict": "same", "confidence": 0.9, "reason": "r"}]}))
        self.run_(store, items, llm=llm)          # would raise

    def test_a_new_item_node_is_created_only_if_absent(self):
        store = FakeStore()
        items = [item(L.SILO_EVENT, "e1", "Apply for Chennai gate pass"),
                 item(L.SILO_TASK, "t1", "Chennai gate pass application")
                 ] + filler()
        llm = self.llm(json.dumps({"pairs": [{
            "pair": 1, "verdict": "same", "confidence": 0.9, "reason": "r"}]}))
        self.run_(store, items, llm=llm)
        merges = [c for c, _p in store.queries if "MERGE" in c]
        self.assertTrue(merges)
        for cypher in merges:
            self.assertIn("ON CREATE SET", cypher)
            self.assertNotIn("\n        SET e.", cypher.replace(
                "ON CREATE SET", ""))

    def test_item_nodes_cannot_collide_with_a_person(self):
        self.assertEqual(
            item(L.SILO_TASK, "1", "Check final PDF paper").node_name(),
            "Task: Check final PDF paper")
        self.assertEqual(item(L.SILO_EVENT, "1", "Mavo").node_name(),
                         "Event: Mavo")

    def test_an_unchanged_item_is_not_reprocessed(self):
        store = FakeStore(persons=["Srimaan Gajjelli"])
        items = [item(L.SILO_EMAIL, "m1", "Srimaan Gajjelli posted")]
        self.run_(store, items)
        again = FakeStore(persons=["Srimaan Gajjelli"])
        out = self.run_(again, items)
        self.assertEqual(out, {"mentions": 0, "proposed": 0})
        self.assertEqual(again.facts, [])

    def test_a_changed_item_is_reprocessed(self):
        store = FakeStore(persons=["Srimaan Gajjelli"])
        self.run_(store, [item(L.SILO_EMAIL, "m1", "Srimaan Gajjelli posted")])
        again = FakeStore(persons=["Srimaan Gajjelli"])
        out = self.run_(again, [item(L.SILO_EMAIL, "m1",
                                     "Srimaan Gajjelli posted again")])
        self.assertEqual(out["mentions"], 1)

    def test_the_number_of_new_links_is_bounded(self):
        people = [f"Person{n} Surname{n}" for n in range(80)]
        store = FakeStore(persons=people)
        items = [item(L.SILO_EMAIL, str(n), f"note about {name}")
                 for n, name in enumerate(people)]
        out = self.run_(store, items)
        self.assertLessEqual(out["mentions"], L.MAX_NEW_LINKS)

    def test_a_dead_graph_is_survived(self):
        class Dead(FakeStore):
            def add_fact(self, *a, **k):
                raise ConnectionError("neo4j down")

            def query(self, *a, **k):
                raise ConnectionError("neo4j down")
        store = Dead(persons=["Srimaan Gajjelli"])
        out = self.run_(store, [item(L.SILO_EMAIL, "m1",
                                     "Srimaan Gajjelli posted")])
        self.assertEqual(out["mentions"], 0)

    def test_a_failing_model_does_not_lose_the_direct_mentions(self):
        store = FakeStore(persons=["Srimaan Gajjelli", "Rahul"])
        items = [item(L.SILO_EMAIL, "m1", "Srimaan Gajjelli posted"),
                 item(L.SILO_EMAIL, "m2", "Rahul wrote")]

        def broken(messages, task_level=None):
            raise RuntimeError("down")
        out = self.run_(store, items, llm=broken)
        self.assertEqual(out["mentions"], 1)

    def test_he_coming_back_stops_the_model_call(self):
        store = FakeStore(persons=["Rahul"])
        llm = self.llm()
        with patch.object(idle_gate, "interrupted", return_value=True):
            self.run_(store, [item(L.SILO_EMAIL, "m1", "Rahul wrote")], llm=llm)
        self.assertEqual(llm.calls, [])


class TheSourcesAreOptionalAndBounded(unittest.TestCase):
    def test_an_unavailable_source_is_survived(self):
        class Dead:
            def query(self, *a, **k):
                raise ConnectionError("down")
        self.assertEqual(L.load_emails(Dead()), [])
        self.assertEqual(L.load_persons(Dead()), [])
        self.assertEqual(L.load_concepts(Dead()), [])

    def test_concepts_exclude_hubs_people_and_values(self):
        seen = []

        class Store:
            def query(self, cypher, **params):
                seen.append((cypher, params))
                return []
        L.load_concepts(Store())
        cypher, params = seen[0]
        self.assertIn("degree <= $hub", cypher)
        for excluded in ("Person", "ProfileValue", "Email"):
            self.assertIn(excluded, cypher)

    def test_items_are_capped_overall(self):
        class Store:
            def query(self, *a, **k):
                return [{"id": str(n), "subject": f"s{n}", "sender": "x",
                         "snippet": "", "sent": ""} for n in range(500)]
        with patch.object(L, "load_projects", return_value=[]), \
                patch.object(L, "load_calendar", return_value=[]), \
                patch.object(L, "load_tasks", return_value=[]):
            self.assertLessEqual(len(L.gather(Store(), ".")), L.MAX_ITEMS)

    def test_a_missing_calendar_does_not_break_gathering(self):
        class Store:
            def query(self, *a, **k):
                return []
        with patch.object(L, "load_calendar", side_effect=RuntimeError("x")), \
                patch.object(L, "load_projects", return_value=[]), \
                patch.object(L, "load_tasks", return_value=[]):
            self.assertEqual(L.gather(Store(), "."), [])


class TheContactSyncFeedsIt(unittest.TestCase):
    """The address book had nothing to link on: 2 emails in 167 contacts and
    two junk "organizations". And one contact -- email, no phone -- was
    dropped without a word."""

    def sync(self, people):
        from providers import google_api
        calls = []
        added = google_api._sync_contact_details(
            people, lambda *a, **k: calls.append(a[:3]))
        return added, calls

    def test_emails_are_synced(self):
        _n, calls = self.sync([{"names": [{"displayName": "Rahul Verma"}],
                                "emailAddresses": [{"value": "R@D.com"}]}])
        self.assertIn(("rahul verma", "HAS_EMAIL", "r@d.com"), calls)

    def test_a_contact_without_a_phone_is_no_longer_dropped(self):
        _n, calls = self.sync([{"names": [{"displayName": "No Phone"}],
                                "emailAddresses": [{"value": "x@y.io"}]}])
        self.assertEqual(len(calls), 1)

    def test_a_malformed_address_is_not_stored(self):
        _n, calls = self.sync([{"names": [{"displayName": "A B"}],
                                "emailAddresses": [{"value": "not an email"}]}])
        self.assertEqual(calls, [])

    def test_junk_organizations_are_not_stored(self):
        """96 contacts have an "organization"; there are two distinct values,
        one of them the letter "a"."""
        _n, calls = self.sync([{"names": [{"displayName": "A B"}],
                                "organizations": [{"name": "a"}, {"name": "-"},
                                                  {"name": "N/A"}]}])
        self.assertEqual(calls, [])

    def test_a_real_organization_is_stored(self):
        _n, calls = self.sync([{"names": [{"displayName": "A B"}],
                                "organizations": [{"name": "Deloitte"}]}])
        self.assertEqual(calls, [("a b", "AFFILIATED_WITH", "Deloitte")])

    def test_a_nameless_entry_is_skipped(self):
        _n, calls = self.sync([{"emailAddresses": [{"value": "x@y.io"}]}])
        self.assertEqual(calls, [])

    def test_the_sync_now_asks_for_them(self):
        import inspect
        from providers import google_api
        source = inspect.getsource(google_api.sync_contacts)
        self.assertIn("emailAddresses", source)
        self.assertIn("organizations", source)

    def test_a_failure_here_never_costs_the_phone_sync(self):
        import inspect
        from providers import google_api
        source = inspect.getsource(google_api.sync_contacts)
        start = source.index("_sync_contact_details")
        self.assertIn("except Exception", source[start:start + 700])


class WiredIn(unittest.TestCase):
    def test_it_rides_on_the_hourly_worker(self):
        import inspect
        import inference
        self.assertIn("entity_linking", inspect.getsource(inference.start_worker))

    def test_each_job_has_its_own_failure_handling(self):
        """A failure in one must not cost the others their pass."""
        import inspect
        import inference
        source = inspect.getsource(inference.start_worker)
        self.assertGreaterEqual(source.count("except Exception"), 2)


if __name__ == "__main__":
    unittest.main()
