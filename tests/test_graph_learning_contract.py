import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from memory.orchestrator import _filter_facts, _format_graph_groups
import deduction


def test_repeated_model_proposal_remains_unconfirmed_until_reviewed():
    row = {
        "subject": "Himanshu", "predicate": "PREFERS", "object": "quiet focus",
        "fact_class": "derived_inference", "review_status": "auto_accepted",
        "sensitivity": "private", "confidence": .91,
    }
    selected = _filter_facts([row], "plan my study session", "user")
    assert selected == []
    selected = _filter_facts([row], "what possible patterns might exist", "user", include_pending=True)
    assert selected == [row]
    rendered = _format_graph_groups(selected)
    assert "UNCONFIRMED INFERENCES" in rendered


def test_legacy_and_current_external_observations_can_feed_deduction():
    assert "external_observation" in deduction.SOURCE_CLASSES
    assert "external_fact" in deduction.SOURCE_CLASSES
    assert "external_fact" not in deduction.STATED_CLASSES
