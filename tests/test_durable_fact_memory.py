from pathlib import Path
from unittest import mock


def test_unexpected_wording_reaches_model_extraction():
    """Phrase gates may advise, but may not discard a possible user fact."""
    import memory.consolidator as consolidator

    with mock.patch.object(consolidator, "_recovery_done", True), \
         mock.patch.object(consolidator._consolidation_queue, "put") as put:
        assert consolidator.queue_for_consolidation(
            "The portal link for college is https://example.edu/login",
            source_id="session_7",
        )
    put.assert_called_once_with((
        "The portal link for college is https://example.edu/login",
        "session_7", "personal", 0,
    ))


def test_invalid_extractor_output_is_not_treated_as_no_facts():
    from memory.consolidator import _parse_llm_json

    assert _parse_llm_json("not json") is None
    assert _parse_llm_json("{}") is None
    assert _parse_llm_json("[]") == []


def test_explicit_recall_has_a_code_owned_memory_contract():
    from memory.query_policy import requires_memory_recall

    assert requires_memory_recall("What is my college portal link?")
    assert requires_memory_recall("I told you this earlier, remember it")
    assert not requires_memory_recall("What is reinforcement learning?")


def test_user_owned_resources_cannot_be_hidden_by_the_router():
    from memory.query_policy import requires_personal_grounding

    assert requires_personal_grounding("check my gate pass status")
    assert requires_personal_grounding("open my college portal")
    assert requires_personal_grounding("check our flight booking")
    assert not requires_personal_grounding("what is a gate pass")
    assert not requires_personal_grounding("what is on my screen")


def test_personal_resource_lookup_includes_opaque_saved_locator():
    import memory.orchestrator as orchestrator

    ordinary = [{
        "fact_id": "f1", "subject": "Himanshu",
        "predicate": "HAS_PREFERENCE", "object": "quiet UI"}]
    locator = {
        "fact_id": "f2", "subject": "Himanshu",
        "predicate": "USES_PORTAL", "object": "https://my.amrita.edu",
    }
    with mock.patch.object(
            orchestrator.store, "search_facts",
            side_effect=[ordinary, [locator] + ordinary]):
        rows = orchestrator._run_graph_lookup(
            "check my gate pass status", ["Himanshu"], [], 1, False,
            limit=12, include_resource_locators=True)
    assert rows[0]["object"] == "https://my.amrita.edu"


def test_generated_directories_never_enter_the_vault_index():
    import memory

    root = Path(memory.VAULT_DIR)
    assert not memory._indexable_path(root / "projects" / "app" / "node_modules" / "x.js")
    assert not memory._indexable_path(root / "logs" / "winter.log")
    assert not memory._indexable_path(root / "browser_profile_firefox_real" / "cache2" / "x")
    assert memory._indexable_path(root / "projects" / "app" / "src" / "main.py")


def test_fact_rementions_preserve_the_original_fact_identity():
    source = (Path(__file__).resolve().parents[1] / "memory" /
              "neo4j_store.py").read_text(encoding="utf-8")
    repeat_block = source.split(
        "FOREACH (_ IN CASE WHEN cur IS NULL THEN [] ELSE [1] END |", 1
    )[1].split("WITH s,o", 1)[0]
    assert "cur.fact_id=$fact_id" not in repeat_block
    assert "cur.source_id=$source_id" not in repeat_block
    assert "FactEvidence" in source


def test_dialogue_turn_is_the_durable_extraction_queue():
    source = (Path(__file__).resolve().parents[1] / "memory" /
              "neo4j_store.py").read_text(encoding="utf-8")
    assert "fact_extraction_status" in source
    assert "pending_fact_extractions" in source
    assert "finish_fact_extraction" in source


def test_pending_memory_is_resumed_during_application_startup():
    root = Path(__file__).resolve().parents[1]
    agent_source = (root / "agent.py").read_text(encoding="utf-8")
    consolidator_source = (root / "memory" / "consolidator.py").read_text(
        encoding="utf-8")
    assert "recovered = recover_pending()" in agent_source
    assert "def recover_pending" in consolidator_source
    assert "An assistant should not require a fresh command" in consolidator_source
