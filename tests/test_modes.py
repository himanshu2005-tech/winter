import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mode_manager import mode_model, ModeManager, WinterMode, POLICIES, MODE_QUALITY_CONTRACTS, MODE_SPEECH_LIMITS, parse_mode_command, infer_mode_intent, quality_contract, speech_word_limit


class FakeGraph:
    def __init__(self, active="assistant"):
        self.active = active
        self.transitions = []

    def query(self, cypher, **params):
        if "RETURN m.active_mode AS mode" in cypher:
            return [{"mode": self.active}]
        if "CREATE (t:ModeTransition" in cypher:
            previous = self.active
            self.active = params["target"]
            self.transitions.append((previous, self.active, params["transition_id"]))
            return [{"previous": previous, "active": self.active}]
        return []


def run():
    assert parse_mode_command("sleep") == ("set", WinterMode.SLEEP)
    assert parse_mode_command("go to sleep") == ("set", WinterMode.SLEEP)
    assert parse_mode_command("sleep mode") == ("set", WinterMode.SLEEP)
    assert parse_mode_command("I want to sleep") == ("set", WinterMode.SLEEP)
    assert parse_mode_command("study time") == ("set", WinterMode.STUDY)
    assert parse_mode_command("research mode") == ("set", WinterMode.RESEARCH)
    assert parse_mode_command("coder mode") == ("set", WinterMode.BUILD)
    assert parse_mode_command("back to normal") == ("set", WinterMode.ASSISTANT)
    # Natural deactivation wording is interpreted semantically by the typed
    # cognitive router rather than accumulated here as phrase variants.
    assert parse_mode_command("deactivate study mode") is None
    assert parse_mode_command("what mode are you in") == ("status", None)
    assert parse_mode_command("help me with sleep research") is None

    def route(payload, task_level=None):
        assert task_level == "cognitive_router"
        return '{"mode_decision":"switch","target_mode":"build","interaction":"quiet","task_kind":"conversation","planning_depth":"direct","needs_screen":false,"confidence":0.96,"acknowledgement":"Switching to build.","rationale":"project work","retrieval":{"graph":false,"episodes":false,"conversations":false}}'

    decision = infer_mode_intent("lets build the vanta repo", WinterMode.ASSISTANT, route)
    assert decision.decision == "switch" and decision.mode is WinterMode.BUILD

    keep = infer_mode_intent(
        "I was sleeping earlier", WinterMode.ASSISTANT,
        lambda *_args, **_kwargs: '{"mode_decision":"keep","target_mode":"assistant","interaction":"respond","task_kind":"conversation","planning_depth":"direct","needs_screen":false,"confidence":0.98,"acknowledgement":"","rationale":"past event","retrieval":{"graph":false}}',
    )
    assert keep.decision == "keep"

    quiet = infer_mode_intent(
        "just monitor, I will ask if I need anything", WinterMode.BUILD,
        lambda *_args, **_kwargs: '{"mode_decision":"keep","target_mode":"build","interaction":"quiet","task_kind":"conversation","planning_depth":"direct","needs_screen":false,"confidence":0.94,"acknowledgement":"Understood. I will stay quiet.","rationale":"quiet request","retrieval":{"graph":false}}',
    )
    assert quiet.decision == "quiet" and "quiet" in quiet.reply

    graph = FakeGraph()
    manager = ModeManager(graph)
    assert manager.active_mode() is WinterMode.ASSISTANT

    changed = manager.switch(WinterMode.BUILD)
    assert changed["changed"] and manager.active_mode() is WinterMode.BUILD
    assert graph.active == "build" and len(graph.transitions) == 1

    # Switching to the mode already active is not a transition.
    repeated = manager.switch(WinterMode.BUILD)
    assert not repeated["changed"] and len(graph.transitions) == 1

    manager.switch(WinterMode.ASSISTANT)
    assert manager.active_mode() is WinterMode.ASSISTANT
    assert len(graph.transitions) == 2

    # Build mode exists to swap the model. If that stops being true it is
    # indistinguishable from assistant mode and should not exist at all.
    build = POLICIES[WinterMode.BUILD]
    assert build.model_env or build.model_default

    # Assistant asks for no particular model, so it uses WINTER_MODEL.
    assistant = POLICIES[WinterMode.ASSISTANT]
    assert not assistant.model_env and not assistant.model_default

    # Six modes, and every policy table covers all of them.
    assert set(WinterMode) == {
        WinterMode.ASSISTANT, WinterMode.STUDY, WinterMode.RESEARCH,
        WinterMode.BUILD, WinterMode.HEALTH, WinterMode.SLEEP,
    }
    assert len(WinterMode) == 6
    assert set(MODE_QUALITY_CONTRACTS) == set(WinterMode)
    assert set(MODE_SPEECH_LIMITS) == set(WinterMode)
    for mode in WinterMode:
        contract = quality_contract(mode)
        assert isinstance(contract, str) and len(contract) >= 60
        assert speech_word_limit(mode) > 0

    source = (Path(__file__).resolve().parents[1] / "agent.py").read_text(encoding="utf-8")
    assert "plan.question = None" in source
    assert 'getattr(active_mode, "value", None) == "sleep"' in source
    print("Mode system checks passed")


if __name__ == "__main__":
    run()
