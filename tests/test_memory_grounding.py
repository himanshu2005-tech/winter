import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from memory.neo4j_store import (
    GraphStore,
    canonical_predicate,
    expand_query_predicates,
    is_unresolved_reference,
)
from memory import orchestrator
from resource_pool import ResourceRequest

class ReferenceGroundingTests(unittest.TestCase):
    def test_portal_predicate_synonyms_share_a_retrieval_family(self):
        self.assertEqual(canonical_predicate("portal_url"), "HAS_PORTAL_LINK")
        expanded = set(expand_query_predicates(["portal_url"]))
        self.assertEqual(expanded, {
            "HAS_PORTAL_LINK", "USES_PORTAL", "PLACEMENTS_PORTAL_URL",
        })

    def test_search_expands_model_predicate_before_querying(self):
        graph = object.__new__(GraphStore)
        graph.query = mock.Mock(return_value=[])
        graph.search_facts(
            "my college portal", entity_keys=["Himanshu"],
            predicates=["portal_url"])
        predicates = set(graph.query.call_args.kwargs["preds"])
        self.assertIn("HAS_PORTAL_LINK", predicates)
        self.assertIn("PLACEMENTS_PORTAL_URL", predicates)

    def test_demonstratives_are_not_durable_entities(self):
        for value in ("this", "that", "it", "this playlist", "that song",
                      "this file", "the current one", "there"):
            with self.subTest(value=value):
                self.assertTrue(is_unresolved_reference(value))

    def test_concrete_entities_are_kept(self):
        for value in ("Discover Weekly", "Weightless by Marconi Union",
                      "D:/winter/README.md", "Transformer Architecture"):
            with self.subTest(value=value):
                self.assertFalse(is_unresolved_reference(value))

    def test_store_rejects_before_any_neo4j_write(self):
        store = object.__new__(GraphStore)
        store.query = mock.Mock()
        result = store.add_fact(
            "Himanshu", "HAS_PREFERENCE", "this playlist",
            source_id="conversation:test")
        self.assertTrue(result.startswith("[Rejected]"))
        store.query.assert_not_called()

    def test_consolidator_checks_objects_before_writing(self):
        from pathlib import Path
        source = (Path(__file__).resolve().parents[1] /
                  "memory" / "consolidator.py").read_text(encoding="utf-8")
        self.assertIn("is_unresolved_reference(obj)", source)

    def test_portfolio_locator_request_is_detected_from_resource_purpose(self):
        self.assertTrue(orchestrator._needs_resource_locators(
            "report the projects",
            ("projects",),
            (ResourceRequest(
                "graph_memory",
                "Find the authoritative locator for the personal portfolio",
                True,
            ),),
        ))

    def test_resource_locator_lookup_keeps_exact_url_when_primary_query_is_crowded(self):
        generic = {
            "fact_id": "generic",
            "subject": "personal portfolio",
            "predicate": "CONTAINS",
            "object": "all projects",
        }
        locator = {
            "fact_id": "locator",
            "subject": "Himanshu",
            "predicate": "HAS_PORTFOLIO_WEBSITE",
            "object": "https://himanshu-d7f2f.web.app",
        }
        with mock.patch.object(
                orchestrator.store, "search_facts",
                side_effect=[[generic], [locator]]) as search:
            rows = orchestrator._run_graph_lookup(
                "report the projects", ["Himanshu"], ["projects"],
                1, False, limit=10, include_resource_locators=True)

        self.assertEqual(rows[0], locator)
        self.assertIn(generic, rows)
        self.assertIn("report the projects", search.call_args_list[1].args[0])

if __name__ == "__main__":
    unittest.main()
