import os
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cognitive_pipeline import (_validate, explicit_report_requested, route_turn,
                                should_apply_mode_switch,
                                should_begin_contextual_quiet)


def test_explicit_mode_switch_does_not_start_contextual_quiet():
    from cognitive_pipeline import CognitiveRoute, RetrievalRoute
    route = CognitiveRoute(
        mode_decision="switch", target_mode="build", interaction="quiet",
        task_kind="action", planning_depth="direct", needs_screen=False,
        confidence=1.0, explicit_mode_change=True,
        retrieval=RetrievalRoute(graph=False))
    assert route.should_be_quiet
    assert not should_begin_contextual_quiet(route, "user")


def test_real_attention_state_still_starts_contextual_quiet():
    from cognitive_pipeline import CognitiveRoute, RetrievalRoute
    route = CognitiveRoute(
        mode_decision="keep", target_mode="assistant", interaction="quiet",
        task_kind="conversation", planning_depth="direct", needs_screen=False,
        confidence=1.0, explicit_mode_change=False,
        retrieval=RetrievalRoute(graph=False))
    assert should_begin_contextual_quiet(route, "user")


def test_model_semantically_selects_report_presentation():
    route = route_turn(
        "prepare this as a durable visual comparison unique-report-route",
        "assistant",
        lambda *_args, **_kwargs: '''{
          "mode_decision":"keep","target_mode":"assistant",
          "interaction":"respond","task_kind":"question",
          "planning_depth":"direct","needs_screen":false,
          "presentation":"report","confidence":0.99,
          "retrieval":{"graph":false}
        }''',
        pending_approval=True,
    )
    assert route.presentation == "report"


def test_ordinary_lookup_defaults_to_inline_presentation():
    route = route_turn(
        "ordinary lookup unique-inline-route", "assistant",
        lambda *_args, **_kwargs: '''{
          "mode_decision":"keep","target_mode":"assistant",
          "interaction":"respond","task_kind":"question",
          "planning_depth":"direct","needs_screen":false,
          "confidence":0.99,"retrieval":{"graph":false}
        }''',
        pending_approval=True,
    )
    assert route.presentation == "inline"


def test_explicit_report_request_survives_router_misclassification():
    route = route_turn(
        "report the latest news unique-explicit-report-contract", "assistant",
        lambda *_args, **_kwargs: '''{
          "mode_decision":"keep","target_mode":"assistant",
          "interaction":"respond","task_kind":"question",
          "planning_depth":"direct","needs_screen":false,
          "presentation":"inline","confidence":0.99,
          "retrieval":{"graph":false}
        }''',
        pending_approval=True,
    )
    assert route.presentation == "report"


def test_passive_report_mention_does_not_force_a_window():
    assert explicit_report_requested("the report says latency is high") is False
    assert explicit_report_requested("show me the findings as a report") is True


def test_model_can_request_silent_background_execution():
    route = route_turn(
        "check this without taking over my screen unique-background-route",
        "assistant",
        lambda *_args, **_kwargs: '''{
          "mode_decision":"keep","target_mode":"assistant",
          "interaction":"respond","task_kind":"action",
          "planning_depth":"direct","needs_screen":false,
          "presentation":"inline","execution_visibility":"background",
          "confidence":0.99,"retrieval":{"graph":false}
        }''',
        pending_approval=True,
    )
    assert route.execution_visibility == "background"


def test_model_first_route_decides_resources_and_action_intents(monkeypatch):
    monkeypatch.setenv("VANTA_MODEL_FIRST_REASONING", "1")
    calls = []
    route = route_turn(
        "please handle a uniquely phrased resource task 91827", "assistant",
        lambda *_args, **kwargs: (calls.append(kwargs.get("task_level")) or '''{
          "mode_decision":"keep","target_mode":"assistant",
          "interaction":"respond","task_kind":"action",
          "planning_depth":"direct","needs_screen":false,
          "confidence":0.99,"action_intents":["retrieve a remote document"],
          "retrieval":{"graph":true,"episodes":true,"conversations":true,
            "entities":["Project Atlas"],"predicates":["HAS_DOCUMENT"],
            "sensitivities":["private"],"fact_limit":8,"memory_limit":3,
            "graph_hops":2}
        }'''))
    assert calls == ["cognitive_router"]
    assert route.action_intents == ("retrieve a remote document",)
    assert route.retrieval.entities == ("Project Atlas",)
    assert route.retrieval.graph_hops == 2


def test_model_first_is_the_default(monkeypatch):
    monkeypatch.delenv("VANTA_MODEL_FIRST_REASONING", raising=False)
    from cognitive_pipeline import model_first_enabled
    assert model_first_enabled()


def test_pure_mode_control_cannot_fall_through_to_the_executor():
    route = _validate({
        "mode_decision": "switch", "target_mode": "health",
        "explicit_mode_change": True, "interaction": "respond",
        "task_kind": "action", "planning_depth": "direct",
        "confidence": .99, "continue_after_control": True,
        "action_intents": ["activate health mode"],
        "resources": [], "retrieval": {"graph": False},
    }, "assistant")
    assert route.action_intents == ()
    assert route.continue_after_control is False


def test_mode_control_keeps_independent_compound_work():
    route = _validate({
        "mode_decision": "switch", "target_mode": "health",
        "explicit_mode_change": True, "interaction": "respond",
        "task_kind": "action", "planning_depth": "standard",
        "confidence": .99, "continue_after_control": True,
        "action_intents": ["activate health mode", "import the supplied archive"],
        "resources": [], "retrieval": {"graph": False},
    }, "assistant")
    assert route.action_intents == ("import the supplied archive",)
    assert route.continue_after_control is True


def run():
    # Production is model-first.  The legacy economy route remains an explicit
    # rollback path, so exercise it under the flag that actually enables it
    # instead of accidentally depending on an older default.
    from cognitive_pipeline import model_first_enabled
    assert model_first_enabled()
    os.environ["VANTA_MODEL_FIRST_REASONING"] = "0"

    fast_calls = []
    fast = route_turn(
        "Explain photosynthesis", "assistant",
        lambda *_args, **_kwargs: fast_calls.append(True))
    assert fast.planning_depth == "direct"
    assert not fast.retrieval.graph
    assert fast_calls == []

    # Ordinary chat and one-step work should not spend an additional model
    # call merely deciding how to route the model call that will answer it.
    economy_calls = []
    economy = route_turn(
        "Open the calculator", "assistant",
        lambda *_args, **_kwargs: economy_calls.append(True))
    assert economy.planning_depth == "direct"
    assert economy_calls == []
    assert not economy.retrieval.graph

    personal_calls = []
    personal = route_turn(
        "What should I work on next?", "assistant",
        lambda *_args, **_kwargs: personal_calls.append(True))
    assert personal.planning_depth == "direct"
    assert personal.retrieval.graph and personal.retrieval.conversations
    assert personal.retrieval.fact_limit == 6
    assert personal_calls == []

    # Questions about a known person and conversational follow-ups about that
    # person's history are memory-dependent.  They must not be treated as
    # context-free chat merely because they do not contain "my".
    person = route_turn(
        "What do you know about Madhu?", "assistant",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("person lookup should use the economy route")))
    assert person.retrieval.graph and person.retrieval.conversations
    history = route_turn(
        "You have history on her", "assistant",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("history follow-up should use the economy route")))
    assert history.retrieval.graph and history.retrieval.conversations

    contextual_calls = []
    contextual = route_turn(
        "Explain this diagram on my screen unique-context-route", "assistant",
        lambda *_args, **_kwargs: (
            contextual_calls.append(True) or
            '{"mode_decision":"keep","target_mode":"assistant",'
            '"interaction":"respond","task_kind":"observation",'
            '"planning_depth":"direct","needs_screen":true,"confidence":1,'
            '"retrieval":{"graph":false}}'))
    assert contextual_calls == []
    assert contextual.needs_screen and contextual.planning_depth == "direct"

    direct_action_calls = []
    direct_action = route_turn(
        "play one relaxing track unique direct action", "assistant",
        lambda *_args, **_kwargs: (direct_action_calls.append(True) or '''{"mode_decision":"keep",
        "target_mode":"assistant","interaction":"respond","task_kind":"action",
        "planning_depth":"direct","needs_screen":false,"confidence":1,
        "retrieval":{"graph":false}}'''))
    assert direct_action.planning_depth == "direct"
    assert direct_action_calls == []

    # Everything below exercises the production model-first route.
    os.environ["VANTA_MODEL_FIRST_REASONING"] = "1"

    calls = []

    attention_calls = []
    attention = route_turn(
        "I am occupied with something immersive unique-attention-test",
        "assistant",
        lambda *_args, **_kwargs: (
            attention_calls.append(True) or
            '{"mode_decision":"keep","target_mode":"assistant",'
            '"interaction":"quiet","task_kind":"observation",'
            '"planning_depth":"direct","needs_screen":false,"confidence":0.98,'
            '"acknowledgement":"Enjoy it. I will stay quiet.",'
            '"rationale":"current activity occupies attention",'
            '"retrieval":{"graph":false}}'))
    assert attention_calls == [True]
    assert attention.should_be_quiet

    def model(_messages, task_level=None):
        calls.append(task_level)
        return '''{
          "mode_decision":"switch","target_mode":"build","interaction":"quiet",
          "task_kind":"conversation","planning_depth":"direct","needs_screen":false,
          "confidence":0.97,"acknowledgement":"Rest well. I will stay quiet.",
          "rationale":"clear current intention",
          "retrieval":{"graph":false,"episodes":false,"conversations":false,
            "include_unconfirmed_inferences":false,"entities":["Himanshu"],
            "predicates":[],"sensitivities":["public","private"],"fact_limit":0,"memory_limit":0}
        }'''

    route = route_turn("switch mode so we can work on the vanta repo unique-cognitive-test", "assistant", model)
    assert route.should_switch_mode and route.target_mode == "build"
    assert route.should_be_quiet and not route.retrieval.graph
    assert calls == ["cognitive_router"]
    # The router may propose sleep, but Himanshu decides. It only applies on
    # its own when he named the mode, or when the target is an emergency.
    assert not should_apply_mode_switch(route)
    assert should_apply_mode_switch(route, explicit=True)
    from cognitive_pipeline import mode_switch_needs_confirmation
    assert mode_switch_needs_confirmation(route)

    # Meaning, not phrase tables: one semantic decision may contain an
    # explicit mode transition, an approval, and a remaining task.
    compound = route_turn(
        "semantic compound control unique test", "study",
        lambda *_args, **_kwargs: '''{
          "mode_decision":"switch","target_mode":"assistant",
          "explicit_mode_change":true,"approval_decision":"approve",
          "continue_after_control":true,"interaction":"respond",
          "task_kind":"action","planning_depth":"standard",
          "needs_screen":false,"confidence":0.99,
          "retrieval":{"graph":false}
        }''',
        pending_approval=True,
    )
    assert should_apply_mode_switch(compound)
    assert compound.target_mode == "assistant"
    assert compound.approval_decision == "approve"
    assert compound.continue_after_control

    # A screen-observation task must execute in the current mode, even if the
    # model incorrectly proposes Research mode.
    from cognitive_pipeline import CognitiveRoute, RetrievalRoute
    screen_route = CognitiveRoute(
        mode_decision="switch", target_mode="research", interaction="respond",
        task_kind="observation", planning_depth="direct", needs_screen=True,
        confidence=1.0, retrieval=RetrievalRoute(graph=False),
    )
    assert not should_apply_mode_switch(screen_route)

    def invalid(_messages, task_level=None):
        return "not json"

    # An unparseable payload must leave the current mode exactly as it was.
    fallback = route_turn("switch mode unique invalid routing payload", "build", invalid)
    assert not fallback.should_switch_mode
    assert fallback.target_mode == "build"
    assert fallback.retrieval.graph

    def low_confidence(_messages, task_level=None):
        return '{"mode_decision":"switch","target_mode":"build","interaction":"quiet","task_kind":"action","planning_depth":"deep","needs_screen":true,"confidence":0.2,"retrieval":{"graph":true}}'

    cautious = route_turn("switch mode unique ambiguous routing payload", "assistant", low_confidence)
    assert not cautious.should_switch_mode and not cautious.should_be_quiet

    orchestrator = (Path(__file__).resolve().parents[1] / "memory" / "orchestrator.py").read_text(encoding="utf-8")
    assert "code_allowed.intersection(rr.sensitivities)" in orchestrator
    assert "specific or routed_entities or naive_entities" in orchestrator
    print("Cognitive pipeline checks passed")


if __name__ == "__main__":
    run()
