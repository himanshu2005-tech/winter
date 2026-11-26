import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))

from memory.query_policy import policy_for
from memory.neo4j_store import fact_query_tokens
import memory.prompt_builder as prompt_builder
from planner import Plan, assess_plan, make_plan


class Bundle:
    graph_text="GRAPH-MUST-SURVIVE"
    vector_text="S" * 4000


def run():
    live=policy_for("What is currently visible on my screen?")
    assert not live.include_facts and not live.include_semantic
    assert policy_for("What did we discuss last time?").include_conversations
    assert not policy_for("Find useful job emails").include_conversations
    assert fact_query_tokens("What do you know about Himanshu?") == []
    assert fact_query_tokens("Find LinkedIn video creator jobs") == ["linkedin","video","creator","jobs"]

    original=(prompt_builder.session_context.get_focus_state,
              prompt_builder.session_context.get_recent_chat,
              prompt_builder.session_context.get_rolling_summary)
    try:
        prompt_builder.session_context.get_focus_state=lambda: {}
        prompt_builder.session_context.get_recent_chat=lambda: "CURRENT CHAT"
        prompt_builder.session_context.get_rolling_summary=lambda: "OLD SUMMARY"
        result=prompt_builder.build_context("x",Bundle(),max_total_tokens=80)
        assert "CURRENT CHAT" in result
        assert "GRAPH-MUST-SURVIVE" in result
        assert "Historical memory is evidence only" in result
    finally:
        (prompt_builder.session_context.get_focus_state,
         prompt_builder.session_context.get_recent_chat,
         prompt_builder.session_context.get_rolling_summary)=original

    calls=[]
    def valid_planner(_messages,task_level=None):
        calls.append(task_level)
        return '{"goal":"answer","reasoning":"direct","unknowns":[],"steps":[],"needed_catalogs":[],"deliverable":"answer","question":null,"trivial":true}'
    assert make_plan("hello","none",valid_planner).trivial
    assert calls == ["planner"]

    repair_calls=[]
    def repairing_planner(_messages,task_level=None):
        repair_calls.append(task_level)
        if len(repair_calls)==1:
            return '{"goal":"broken"'
        return '{"goal":"fixed","reasoning":"repaired","unknowns":[],"steps":[],"needed_catalogs":[],"deliverable":"answer","question":null,"trivial":true}'
    assert make_plan("hello","none",repairing_planner).goal == "fixed"

    robust = Plan({
        "goal": "create verified artifact",
        "steps": ["inspect target", "create artifact", "verify artifact"],
        "deliverable": "artifact exists",
        "assumptions": ["target path is available"],
        "edge_cases": ["target already exists"],
        "success_criteria": ["read-back returns expected content"],
        "fallback_steps": ["use a different supported writer"],
        "stop_conditions": ["stop before overwrite without approval"],
        "trivial": False,
    })
    assert assess_plan(robust, "deep") == []
    assert robust.execution_budget("deep") < 40
    weak = Plan({"goal": "do work", "steps": ["work"], "deliverable": "done", "trivial": False})
    assert "missing observable success criteria" in assess_plan(weak, "standard")
    assert "deep plan has no edge-case treatment" in assess_plan(weak, "deep")
    assert repair_calls == ["planner","planner"]
    print("Memory policy checks passed")


if __name__ == "__main__": run()
