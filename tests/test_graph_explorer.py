import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from graph_explorer import load_graph_snapshot, radial_layout, snapshot_from_rows


class FakeStore:
    def __init__(self):
        self.cypher = ""
        self.params = {}
    def query(self, cypher, **params):
        self.cypher, self.params = cypher, params
        return [
            {"source_id": "himanshu", "source": "Himanshu", "target_id": "focus",
             "target": "Deep focus", "predicate": "PREFERS",
             "fact_class": "explicit_preference", "confidence": 0.95,
             "sensitivity": "private", "review_status": "accepted"},
            {"source_id": "himanshu", "source": "Himanshu", "target_id": "focus",
             "target": "Deep focus", "predicate": "PREFERS",
             "fact_class": "explicit_preference", "confidence": 0.95,
             "sensitivity": "private", "review_status": "accepted"},
        ]


def run():
    store = FakeStore()
    snapshot = load_graph_snapshot("focus", limit=999, store_obj=store)
    assert store.params["query"] == "focus"
    assert store.params["limit"] == 120
    assert store.params["sensitivities"] == ["public", "private"]
    assert "restricted" not in store.params["sensitivities"]
    assert len(snapshot["nodes"]) == 2
    assert len(snapshot["edges"]) == 1
    assert set(snapshot["edges"][0]) == {
        "source", "target", "predicate", "fact_class", "confidence",
        "sensitivity", "review_status", "evidence_source_id",
        "evidence_type", "authority",
    }
    assert snapshot["summary"]["fact_classes"] == {"explicit_preference": 1}
    positions = radial_layout(snapshot, 800, 500)
    assert set(positions) == {"himanshu", "focus"}
    assert positions["himanshu"] == (400.0, 250.0)
    assert snapshot_from_rows([]) == {"nodes": [], "edges": []}
    print("Graph explorer checks passed")


if __name__ == "__main__":
    run()
