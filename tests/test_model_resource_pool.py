import inspect
import threading
from pathlib import Path
from unittest import mock

import action_registry
import cognitive_pipeline
import resource_broker
import resource_pool
import session_context


ROOT = Path(__file__).resolve().parents[1]


def _route(payload, marker):
    return cognitive_pipeline.route_turn(
        f"resource contract test {marker}", "assistant",
        lambda *_args, **_kwargs: payload,
        pending_approval=True,
    )


def test_request_parser_is_strict_and_canonical():
    requests = resource_pool.parse_requests([
        {"name": "screen", "required": "false", "purpose": "read pixels"},
        {"name": "conversation_history", "required": "true"},
        {"name": "made_up_sensor", "required": True},
    ])
    assert [(item.name, item.required) for item in requests] == [
        ("screen", False), ("historical_conversations", True)]


def test_model_resource_contract_is_authoritative_over_legacy_flags():
    route = _route('''{
      "mode_decision":"keep","target_mode":"assistant",
      "interaction":"respond","task_kind":"observation",
      "planning_depth":"direct","needs_screen":false,
      "vision_strategy":"hybrid","confidence":0.99,
      "resources":[
        {"name":"screen","purpose":"inspect the active UI","required":true},
        {"name":"graph_memory","purpose":"use saved project facts"},
        {"name":"historical_conversations","purpose":"resolve older context"}
      ],
      "retrieval":{"graph":false,"episodes":false,"conversations":false}
    }''', "authoritative")
    assert route.needs_screen
    assert route.vision_strategy == "hybrid"
    assert route.resource_names == {
        "screen", "graph_memory", "historical_conversations"}
    assert route.retrieval.graph
    assert route.retrieval.conversations


def test_low_confidence_cannot_acquire_resources_or_memory():
    route = _route('''{
      "mode_decision":"keep","target_mode":"assistant",
      "interaction":"respond","task_kind":"action",
      "planning_depth":"direct","confidence":0.2,
      "resources":[{"name":"camera","required":true},
                   {"name":"graph_memory","required":true}],
      "action_intents":["identify the person"],
      "retrieval":{"graph":true,"episodes":true,"conversations":true}
    }''', "low-confidence")
    assert route.resource_requests == ()
    assert route.action_intents == ()
    assert not route.needs_screen
    assert not route.retrieval.graph
    assert not route.retrieval.episodes
    assert not route.retrieval.conversations
    assert route.retrieval.fact_limit == 0
    assert route.retrieval.memory_limit == 0


def test_string_booleans_do_not_change_control_state():
    route = _route('''{
      "mode_decision":"switch","target_mode":"build",
      "interaction":"respond","task_kind":"action",
      "planning_depth":"direct","confidence":0.99,
      "explicit_mode_change":"false","continue_after_control":"false",
      "resources":[]
    }''', "strict-booleans")
    assert not route.explicit_mode_change
    assert route.continue_after_control is False


def test_code_policy_removes_live_local_sensors_from_remote_turns():
    route = _route('''{
      "mode_decision":"keep","target_mode":"assistant",
      "interaction":"respond","task_kind":"observation",
      "planning_depth":"direct","confidence":0.99,
      "vision_strategy":"vlm",
      "resources":[{"name":"screen"},{"name":"camera"},
                   {"name":"web"},{"name":"graph_memory"}]
    }''', "remote-policy")
    filtered = cognitive_pipeline.apply_resource_policy(
        route, origin="user", remote=True)
    assert filtered.resource_names == {"web", "graph_memory"}
    assert not filtered.needs_screen
    assert filtered.vision_strategy == "none"
    assert filtered.retrieval.graph


def test_guest_policy_cannot_pull_private_resources():
    requests = resource_pool.parse_requests([
        {"name": "web"}, {"name": "calendar"}, {"name": "display"}])
    allowed = resource_pool.authorize_requests(
        requests, origin="user", confidence=1.0, guest=True)
    assert resource_pool.names(allowed) == {"web", "display"}


def test_broker_runs_independent_providers_concurrently():
    barrier = threading.Barrier(2)

    def provider(value):
        barrier.wait(timeout=1.0)
        return value

    requests = (
        resource_pool.ResourceRequest("calendar"),
        resource_pool.ResourceRequest("tasks"),
    )
    bundle = resource_broker.collect(
        requests,
        {"calendar": lambda: provider("calendar evidence"),
         "tasks": lambda: provider("task evidence")},
        timeout_sec=2.0,
    )
    assert [item.ok for item in bundle.items] == [True, True]
    assert "calendar evidence" in bundle.render()
    assert "task evidence" in bundle.render()


def test_required_provider_failure_survives_context_assembly_without_payload_log():
    def fail():
        raise RuntimeError("private raw value")

    bundle = resource_broker.collect(
        (resource_pool.ResourceRequest("selected_text", required=True),),
        {"selected_text": fail}, timeout_sec=1.0)
    assert [item.name for item in bundle.missing_required] == ["selected_text"]
    assert "REQUIRED RESOURCE UNAVAILABLE" in bundle.render()
    assert "private raw value" not in bundle.audit_line()

    empty = resource_broker.collect(
        (resource_pool.ResourceRequest("camera", required=True),),
        {"camera": lambda: ""}, timeout_sec=1.0)
    assert [item.name for item in empty.missing_required] == ["camera"]


def test_resource_surface_augments_but_does_not_replace_primary_outcome():
    base = [{"name": "PRIMARY", "catalog": "X", "text": "primary"},
            {"name": "DISCOVER_ACTIONS", "catalog": "DISCOVERY",
             "text": "discover"}]
    surfaced = {"name": "BROWSER_GOTO", "catalog": "BROWSER",
                "text": "goto"}
    with mock.patch.object(action_registry, "search",
                           return_value=[surfaced]):
        result = action_registry.augment_with_resource_queries(
            base, ROOT, ("browser navigate to a page",), limit=4)
    assert [item["name"] for item in result] == [
        "PRIMARY", "BROWSER_GOTO", "DISCOVER_ACTIONS"]


def test_executor_integration_uses_resource_surface_and_prompt_contract():
    source = (ROOT / "agent.py").read_text(encoding="utf-8")
    assert "action_registry.augment_with_resource_queries" in source
    assert "resource_pool.prompt_block" in source
    assert "if use_legacy_perception:" in source


def test_contextualizer_preserves_evidence_without_forcing_old_proposal():
    source = inspect.getsource(session_context.contextualize_user_input)
    assert "continuity evidence" in source
    assert "Execute the most recently proposed" not in source
    assert "re.fullmatch" not in source


def test_desktop_launch_authenticates_before_heavy_services():
    source = (ROOT / "start_quiet.bat").read_text(encoding="utf-8")
    assert source.index("identity_preflight.py") < source.index("start_neo4j.py")
    assert source.index("start_neo4j.py") < source.index("agent.py")


def test_dashboard_refresh_does_not_take_a_full_context_snapshot():
    source = (ROOT / "agent.py").read_text(encoding="utf-8")
    start = source.index("# 0. Dashboard Live Data Sync")
    end = source.index("# 0a. Daily planning", start)
    assert "gather_context(" not in source[start:end]
