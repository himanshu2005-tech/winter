import sys
import time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from memory.neo4j_store import GraphStore


def run():
    unavailable = GraphStore()
    unavailable._unavailable_until = time.monotonic() + 2.0
    started = time.monotonic()
    try:
        unavailable._get_driver()
        raise AssertionError("open circuit should fail")
    except ConnectionError as exc:
        assert "temporarily unavailable" in str(exc)
    assert time.monotonic() - started < 0.2

    from neo4j.exceptions import ServiceUnavailable

    class BrokenDriver:
        def __init__(self):
            self.closed = False
        def execute_query(self, *_args, **_kwargs):
            raise ServiceUnavailable("synthetic outage")
        def close(self):
            self.closed = True

    class BrokenStore(GraphStore):
        def __init__(self):
            super().__init__()
            self.broken = BrokenDriver()
        def _get_driver(self):
            self._driver = self.broken
            return self.broken
        def _database(self):
            return "neo4j"

    broken = BrokenStore()
    try:
        broken.query("RETURN 1")
        raise AssertionError("lost connection should fail")
    except ConnectionError as exc:
        assert "paused briefly" in str(exc)
    assert broken.broken.closed
    assert broken._unavailable_until > time.monotonic()
    print("Neo4j resilience checks passed")


if __name__ == "__main__":
    run()