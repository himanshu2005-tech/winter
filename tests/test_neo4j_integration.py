import uuid, sys, os
import pytest
from urllib.parse import urlsplit
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from memory.neo4j_store import store
from memory.state_db import execute_read, execute_write
import session_context


def _endpoint(uri):
    parsed = urlsplit(uri)
    host = (parsed.hostname or '').lower()
    if host in {'localhost', '127.0.0.1', '::1'}:
        host = 'loopback'
    return host, parsed.port or 7687


@pytest.fixture(scope='module', autouse=True)
def disposable_graph_only(request):
    if request.config.getoption('--isolated-action-audit', default=False):
        pytest.skip('Live Neo4j round trips are excluded from the isolated action audit')
    target = os.getenv('VANTA_TEST_NEO4J_URI', '').strip()
    production = os.getenv('NEO4J_URI', 'bolt://localhost:7687').strip()
    if not target or _endpoint(target) == _endpoint(production):
        pytest.skip('Set VANTA_TEST_NEO4J_URI to a separate disposable Neo4j instance')
    with pytest.MonkeyPatch.context() as patch:
        store.close()
        patch.setenv('NEO4J_URI', target)
        try:
            if not _neo4j_reachable():
                pytest.skip('Disposable Neo4j instance is unavailable')
            yield
        finally:
            store.close()

def test_neo4j_health_and_hybrid_retrieval():
    assert store.health()["status"] == "ok"
    indexes = store.query("SHOW INDEXES YIELD name,state WHERE name IN ['memory_embedding','memory_text'] RETURN name,state")
    assert {row["name"] for row in indexes if row["state"] == "ONLINE"} == {"memory_embedding","memory_text"}
    rows = store.search_memories("VANTA",limit=2)
    assert isinstance(rows,list)

def test_alarm_compatibility_round_trip():
    alarm_id = "test-" + uuid.uuid4().hex
    try:
        execute_write("INSERT INTO alarms (id, label, fire_at, recurrence, enabled, created_by, source, status) VALUES (?, ?, ?, ?, 1, ?, ?, 'pending')",(alarm_id,"integration check","2099-01-01T09:00:00+05:30","none","test","integration"))
        assert execute_read("SELECT id, fire_at FROM alarms WHERE id = ?",(alarm_id,)) == [(alarm_id,"2099-01-01T09:00:00+05:30")]
    finally:
        store.query("MATCH (a:Alarm {id:$id}) DETACH DELETE a",id=alarm_id)

def test_dialogue_reference_resolution():
    session_context.start_session()
    session_id = session_context.get_session_id()
    try:
        session_context.record_user("Should we review the useful emails?")
        session_context.record_assistant("I can retrieve the useful unread emails and summarize them.")
        session_context.record_user("do it")
        resolved=session_context.contextualize_user_input("do it")
        assert "retrieve the useful unread emails" in resolved
    finally:
        store.query("MATCH (s:DialogueSession {id:$id}) OPTIONAL MATCH (s)-[:CONTAINS]->(t) DETACH DELETE s,t",id=session_id)
        # record_assistant also writes the retrievable conversation Memory.
        # Integration checks must leave the production graph exactly as they
        # found it, including that second persistence surface.
        store.query("MATCH (m:Memory) WHERE m.id STARTS WITH $prefix DETACH DELETE m",prefix=session_id)

def test_task_checkpoint_round_trip():
    identity="test-"+uuid.uuid4().hex
    try:
        store.save_checkpoint(identity,identity,identity,"thinking",{"turn_id":"t1","context":{"goal":"test"}})
        row=store.latest_checkpoint(identity)
        assert row["task_id"]==identity and row["state"]=="thinking"
    finally:
        store.query("MATCH (c:TaskCheckpoint {id:$id}) DELETE c",id=identity)

def _neo4j_reachable():
    """Whether there is a database to integrate with right now.

    These are integration tests: with Neo4j stopped they fail for a reason that
    says nothing about the code, and a suite that goes red when a service is
    merely off trains you to ignore it. Absent database means skipped, not
    broken -- but it says so out loud, so a genuinely missing database is never
    mistaken for a pass.
    """
    try:
        store.query("RETURN 1 AS ok")
        return True
    except Exception as exc:
        print(f"SKIPPED: Neo4j is not reachable ({str(exc).splitlines()[0][:90]}). "
              f"Start it to run these integration checks.")
        return False


if __name__ == "__main__":
    target = os.getenv('VANTA_TEST_NEO4J_URI', '').strip()
    if not target or _endpoint(target) == _endpoint(os.getenv('NEO4J_URI', 'bolt://localhost:7687').strip()):
        print('SKIPPED: Select a separate disposable database with VANTA_TEST_NEO4J_URI.')
        raise SystemExit(0)
    store.close()
    os.environ['NEO4J_URI'] = target
    if not _neo4j_reachable():
        raise SystemExit(0)
    test_neo4j_health_and_hybrid_retrieval()
    test_alarm_compatibility_round_trip()
    test_dialogue_reference_resolution()
    test_task_checkpoint_round_trip()
    print("Neo4j integration checks passed")
