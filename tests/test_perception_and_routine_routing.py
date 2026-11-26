"""Regression tests from the 2026-10-17 camera and portal failures."""

from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import actions
import autonomy
import cognitive_pipeline
import outcome_compiler
import outcome_planner
import outcomes


def _selected(text):
    compiled = outcome_compiler.compile(text)
    bound = outcome_planner.bind(compiled, lambda _query, _limit: (), 8)
    return compiled, outcome_planner.select(bound, text, ())


def test_camera_source_is_not_an_interchangeable_screen_capture():
    for request in (
            "capture the camera",
            "what is on your camera identify them",
            "watch through the webcam"):
        compiled, selected = _selected(request)
        assert [item.operation for item in compiled.outcomes] == [
            outcomes.OBSERVE_CAMERA]
        assert [item.selected_action for item in selected] == [
            "CAPTURE_CAMERA"]
        assert not selected[0].alternatives


def test_screen_source_stays_on_the_screen():
    compiled, selected = _selected("what is on my screen")
    assert compiled.outcomes[0].operation == outcomes.OBSERVE_SCREEN
    assert selected[0].selected_action == "TAKE_SCREENSHOT"
    assert not selected[0].alternatives


def test_source_free_visual_language_defaults_to_physical_camera():
    for request in (
            "what can you see",
            "tell me what you see",
            "can you tell what you see",
            "tell me who you see",
            "look at this please"):
        compiled, selected = _selected(request)
        assert compiled.outcomes[0].operation == outcomes.OBSERVE_CAMERA
        assert selected[0].selected_action == "CAPTURE_CAMERA"


def test_old_browser_context_cannot_poison_a_new_camera_question():
    request = "what can you see"
    compiled = outcome_compiler.compile(
        request, history=("I have Chrome open on a browser tab",))
    bound = outcome_planner.bind(compiled, lambda _query, _limit: (), 8)
    selected = outcome_planner.select(
        bound, request, ("I have Chrome open on a browser tab",))
    assert compiled.outcomes[0].operation == outcomes.OBSERVE_CAMERA
    assert selected[0].selected_action == "CAPTURE_CAMERA"


def test_digital_deictic_language_routes_to_current_screen():
    for request in (
            "read the highlighted text",
            "explain the selected code",
            "what does this browser tab show",
            "look at the window on my screen",
            "read the error under my cursor"):
        compiled, selected = _selected(request)
        assert compiled.outcomes[0].operation == outcomes.OBSERVE_SCREEN, request
        assert selected[0].selected_action == "TAKE_SCREENSHOT", request


def test_physical_scene_language_routes_to_camera_without_device_word():
    for request in (
            "what am I holding",
            "who can you see in the room",
            "describe the person in front of me"):
        compiled, selected = _selected(request)
        assert compiled.outcomes[0].operation == outcomes.OBSERVE_CAMERA, request
        assert selected[0].selected_action == "CAPTURE_CAMERA", request


def test_registry_shortlist_drops_the_wrong_visual_source():
    camera = outcome_compiler.compile("what can you see")
    bound = outcome_planner.bind(camera, lambda _q, _l: (), 8)
    selected = outcome_planner.select(bound, "what can you see", ())
    entries = [
        {"name": "TAKE_SCREENSHOT"},
        {"name": "READ_SCREEN_TEXT"},
        {"name": "CAPTURE_CAMERA"},
        {"name": "DISCOVER_ACTIONS"},
    ]
    assert [entry["name"] for entry in
            outcome_planner.constrain_registry_entries(entries, selected)] == [
                "CAPTURE_CAMERA", "DISCOVER_ACTIONS"]


def test_cognitive_route_cannot_turn_camera_language_into_screen_capture():
    route = cognitive_pipeline.route_turn(
        "what can you see", "assistant",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("fast visual route should not need an LLM")))
    assert route.task_kind == "observation"
    assert route.needs_screen is False
    assert route.vision_strategy == "none"


def test_cognitive_route_requests_screen_for_digital_evidence():
    route = cognitive_pipeline.route_turn(
        "read the highlighted text", "assistant",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("economy visual route should not need an LLM")))
    assert route.task_kind == "observation"
    assert route.needs_screen is True


def test_saved_capture_display_is_a_visible_artifact_outcome():
    for request in (
            "bring up the captured image",
            "I said bring up the image you just captured.",
            "show the previous camera photo"):
        compiled, selected = _selected(request)
        assert compiled.outcomes[0].operation == outcomes.DISPLAY_CAMERA_CAPTURE
        assert selected[0].selected_action == "VIEW_CAMERA_CAPTURE"


def test_ui_has_a_real_image_artifact_renderer_and_agent_uses_it():
    root = Path(__file__).resolve().parents[1]
    ui_source = (root / "ui.py").read_text(encoding="utf-8")
    agent_source = (root / "agent.py").read_text(encoding="utf-8")
    assert "def _show_image_artifact" in ui_source
    assert 'elif s == "image_artifact"' in ui_source
    assert 'ui_queue.put(("image_artifact"' in agent_source


def test_scheduled_portal_inspection_has_navigation_sequence():
    request = (
        "Open https://placements.haveloc.com, check for pending jobs and "
        "newly posted internship opportunities, and report the listings.")
    compiled, selected = _selected(request)
    assert compiled.outcomes[0].operation == outcomes.INSPECT_WEB_PAGE
    assert selected[0].selected_action == "BROWSER_READ"
    guidance = outcome_planner.render_selection(selected)
    assert "BROWSER_START -> BROWSER_GOTO -> BROWSER_READ" in guidance
    assert outcome_compiler.should_compile("routine")
    assert not outcome_compiler.should_compile("proactive")


def test_routine_can_navigate_read_only_but_cannot_act_on_page():
    assert autonomy.action_allowed("BROWSER_GOTO", "routine")[0]
    assert autonomy.action_allowed("BROWSER_READ", "routine")[0]
    assert not autonomy.action_allowed("BROWSER_CLICK", "routine")[0]
    assert not autonomy.action_allowed("SEND_EMAIL", "routine")[0]
    assert not autonomy.action_allowed("BROWSER_GOTO", "proactive")[0]


def _browser_context(attrs):
    return SimpleNamespace(
        action_type="BROWSER_READ", attrs=attrs, ui_queue=mock.Mock(),
        base_dir=".", origin="user", turn_id="t", interrupt_event=None,
        element_map={}, response_text="")


def test_blank_browser_snapshot_is_not_a_success_receipt():
    blank = ('[Browser Snapshot] '
             '{"tab":"t1","url":"about:blank","title":"","elements":[],"text":""}')
    with mock.patch("browser.snapshot", return_value=blank):
        actions._run_browser_read(_browser_context({}))
    assert actions.execute_actions._last_output.startswith("[Error]")
    assert "BROWSER_GOTO" in actions.execute_actions._last_output
