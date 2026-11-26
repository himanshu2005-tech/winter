import os
import sys
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from memory import orchestrator


def test_unmatched_model_predicate_retries_without_the_hint():
    remembered = {
        "subject": "Himanshu",
        "predicate": "HAS_PORTAL_LINK",
        "object": "https://example.edu/login",
    }
    with mock.patch.object(
            orchestrator.store, "search_facts",
            side_effect=[[], [remembered]]) as search:
        rows = orchestrator._run_graph_lookup(
            "my college portal link", ["Himanshu"],
            ["model_invented_portal_predicate"], 1, False, limit=10)

    assert rows == [remembered]
    assert search.call_count == 2
    assert search.call_args_list[0].kwargs["predicates"] == [
        "model_invented_portal_predicate"]
    assert search.call_args_list[1].kwargs["predicates"] is None
