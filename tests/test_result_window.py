import os
import queue
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import action_bus
import action_schema
import actions  # noqa: F401 -- registers handlers on the shared bus
import autonomy


def dispatch(response, attrs):
    events = queue.Queue()
    ctx = action_bus.ActionContext(
        "RESULT_WINDOW", attrs, events, os.path.dirname(os.path.dirname(__file__)),
        response_text=response,
    )
    action_bus.BUS.dispatch(ctx)
    captured = []
    while not events.empty():
        captured.append(events.get_nowait())
    return ctx.receipt, captured


def test_result_window_is_authorized_registered_and_schema_checked():
    assert "RESULT_WINDOW" in autonomy.KNOWN_ACTIONS
    assert action_bus.BUS.handles("RESULT_WINDOW")
    assert action_schema.ACTION_SPECS["RESULT_WINDOW"]["body"] is True


def test_plain_body_opens_result_surface():
    actions.reset_task_ledger("plain report")
    response = (
        '<ACTION type="RESULT_WINDOW" action="show" title="Answer">\n'
        '## Result\nUseful detail\n</ACTION>')
    receipt, events = dispatch(response, {"action": "show", "title": "Answer"})
    assert receipt.startswith("[Success]")
    assert events == [("show_result", {
        "title": "Answer", "content": "## Result\nUseful detail"})]
    assert actions.last_task_report()["content"] == "## Result\nUseful detail"


def test_nested_report_protocol_is_removed_before_rendering():
    actions.reset_task_ledger("nested report")
    response = (
        '<ACTION type="RESULT_WINDOW" action="show" title="News">'
        '<REPORT>## News\n\n| Item | Status |\n|---|---|\n| One | Verified |\n\n'
        '```mermaid\nflowchart LR\nA-->B\n```\n\n'
        'Source: https://example.com/story</REPORT></ACTION>')
    receipt, events = dispatch(response, {"action": "show", "title": "News"})

    kind, payload = events[0]
    assert kind == "show_result"
    assert payload["content"].startswith("## News")
    assert "<REPORT>" not in payload["content"]
    assert "tables=1" in receipt
    assert "diagrams=1" in receipt
    assert "example.com" in receipt
    assert actions.last_task_report()["title"] == "News"

    actions.reset_task_ledger("next task")
    assert actions.last_task_report() is None


def test_explicit_result_window_action_is_authoritative_even_if_router_said_inline():
    actions.set_turn_presentation("inline")
    try:
        receipt, events = dispatch(
            '<ACTION type="RESULT_WINDOW" action="show">Useful detail</ACTION>',
            {"action": "show"},
        )
        assert receipt.startswith("[Success]")
        assert events == [("show_result", {
            "title": "VANTA Result", "content": "Useful detail"})]
    finally:
        actions.set_turn_presentation("report")


def test_rich_document_routes_tables_diagrams_and_buttons_as_data():
    body = ('{"title":"Choice","blocks":['
            '{"type":"table","headers":["A"],"rows":[["B"]]},'
            '{"type":"diagram","nodes":["Start","Done"],"edges":[["0","1"]]},'
            '{"type":"actions","items":[{"label":"Continue","command":"continue"}]}'
            ']}')
    response = f'<ACTION type="RESULT_WINDOW" action="show">{body}</ACTION>'
    receipt, events = dispatch(response, {"action": "show"})
    assert "3 structured block" in receipt
    assert events[0][0] == "rich_result"
    assert events[0][1]["blocks"][2]["items"][0]["command"] == "continue"


def test_hide_preserves_existing_content():
    receipt, events = dispatch(
        '<ACTION type="RESULT_WINDOW" action="hide" />', {"action": "hide"})
    assert "preserved" in receipt
    assert events == [("hide_result", "")]


def test_only_explicit_reports_are_routed_to_visible_result_window():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, "agent.py"), encoding="utf-8") as handle:
        source = handle.read()
    assert "display_report = report_text(response)" in source
    assert 'report_surface_requested(response,' in source
    assert "if display_report:" in source
    assert 'ui_queue.put(("show_result", {' in source
    assert '"content": display_report' in source


def test_verified_nontrivial_report_is_reopened_after_critic():
    # Importing here keeps the inexpensive action-contract tests above free of
    # the main agent module's optional audio/vision imports.
    import agent
    from unittest.mock import patch

    events = queue.Queue()
    with patch.object(agent, "ui_queue", events), patch.object(agent, "speak"):
        agent._deliver_verified_completion(
            "## News\n- Verified item", "", "user",
            report_text="## News\n- Verified item",
            report_title="News report")

    assert events.get_nowait() == ("info", "## News\n- Verified item")
    assert events.get_nowait() == ("show_result", {
        "title": "News report", "content": "## News\n- Verified item"})


def test_completion_narration_cannot_replace_an_action_rendered_report():
    import agent

    actions.reset_task_ledger("render report")
    dispatch(
        '<ACTION type="RESULT_WINDOW" action="show" title="Actual">'
        '## Actual report\nSubstantive evidence</ACTION>',
        {"action": "show", "title": "Actual"})
    assert agent._task_report_already_rendered() is True

    source = (Path(__file__).resolve().parents[1] / "agent.py").read_text(
        encoding="utf-8")
    assert 'if _task_report_already_rendered():' in source
    assert 'display_report = ""' in source
    assert 'dashboard_text = ""\n                display_report = ""' in source


def test_teaching_catalog_uses_only_the_rich_report_surface():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, "agent.py"), encoding="utf-8") as handle:
        source = handle.read()
    start = source.index('ACTION_CATALOGS["TUTOR"] =')
    end = source.index("# Capabilities declared", start)
    catalog = source[start:end]
    assert "RESULT_WINDOW" in catalog
    assert "LaTeX" in catalog
    assert "```mermaid" in catalog
    assert "| Part | Meaning |" in catalog
    assert "<ACTION type=\"BOARD_" not in catalog


def test_invalid_3d_report_returns_actionable_failure_without_showing_or_remembering_it():
    actions.reset_task_ledger('invalid geometry')
    body = '```scene3d\n{"objects":[{"type":"made-up-shape"}]}\n```'
    response = '<ACTION type="RESULT_WINDOW" action="show">' + body + '</ACTION>'
    receipt, events = dispatch(response, {'action': 'show'})
    assert receipt.startswith('[Error]') and 'made-up-shape' in receipt
    assert not events and actions.last_task_report() is None


def test_corrected_geometry_can_display_after_failed_attempt():
    actions.reset_task_ledger('repair geometry')
    invalid = '```scene3d\n{"objects":[{"type":"unknown"}]}\n```'
    dispatch('<ACTION type="RESULT_WINDOW">' + invalid + '</ACTION>', {'action': 'show'})
    valid = '```scene3d\n{"objects":[{"type":"cylinder","radius":0.2,"height":0.5}]}\n```'
    receipt, events = dispatch('<ACTION type="RESULT_WINDOW">' + valid + '</ACTION>', {'action': 'show'})
    assert receipt.startswith('[Success]') and events[0][0] == 'show_result'
    assert actions.last_task_report()['content'] == valid


def test_model_catalogue_matches_the_geometry_contract():
    import agent
    from report_geometry import contract
    matching = [text for text in agent.ACTION_CATALOGS.values() if 'RESULT_WINDOW' in text]
    assert matching and all(contract() in text for text in matching)
    assert all('__REPORT_SCENE_CONTRACT__' not in text for text in agent.ACTION_CATALOGS.values())
