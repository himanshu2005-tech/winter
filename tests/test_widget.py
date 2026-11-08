import os
import queue
import time
from unittest import mock

import action_bus
import action_schema
import actions  # noqa: F401 -- registers handlers
import autonomy
import capabilities
import widget_spec


ROOT = os.path.dirname(os.path.dirname(__file__))


def _dispatch(body, attrs=None):
    response = f'<ACTION type="WIDGET" action="show">{body}</ACTION>'
    events = queue.Queue()
    ctx = action_bus.ActionContext(
        "WIDGET", attrs or {"action": "show"}, events, ROOT,
        response_text=response)
    action_bus.BUS.dispatch(ctx)
    found = []
    while not events.empty():
        found.append(events.get_nowait())
    return ctx.receipt, found


def test_widget_is_declared_authorized_registered_and_schema_checked():
    assert capabilities.get("WIDGET") is not None
    assert "WIDGET" in autonomy.KNOWN_ACTIONS
    assert action_bus.BUS.handles("WIDGET")
    assert action_schema.ACTION_SPECS["WIDGET"]["body"] is True


def test_timer_widget_is_data_not_executable_markup():
    receipt, events = _dispatch(
        '{"id":"focus","kind":"timer","title":"Focus",'
        '"duration_seconds":900}')
    assert receipt.startswith("[Success]")
    assert events[0][0] == "show_widget"
    assert events[0][1]["kind"] == "timer"
    assert events[0][1]["deadline"] > time.time()


def test_widget_supports_tables_charts_and_checklists():
    table = widget_spec.normalize({
        "kind": "table", "columns": ["Task", "State"],
        "rows": [["Revise", "Done"], ["Quiz", "Next"]]})
    chart = widget_spec.normalize({
        "kind": "bar_chart", "series": [{"label": "Math", "value": 8}]})
    checklist = widget_spec.normalize({
        "kind": "checklist", "items": [{"label": "Review", "done": True}]})
    assert table["rows"][1][1] == "Next"
    assert chart["series"][0]["value"] == 8
    assert checklist["items"][0]["done"] is True


def test_widget_rejects_html_or_code_as_a_kind():
    try:
        widget_spec.normalize({"kind": "html", "value": "<script>x()</script>"})
    except ValueError as exc:
        assert "widget kind" in str(exc)
    else:
        raise AssertionError("arbitrary executable widget type was accepted")


def test_hide_widget_routes_one_close_event():
    events = queue.Queue()
    ctx = action_bus.ActionContext(
        "WIDGET", {"action": "hide", "id": "focus"}, events, ROOT,
        response_text='<ACTION type="WIDGET" action="hide" id="focus" />')
    action_bus.BUS.dispatch(ctx)
    assert events.get_nowait() == ("hide_widget", "focus")


@mock.patch("system_tools.set_timer",
            return_value="[Success] Timer set for 15m — fires at 18:15.")
def test_visible_timer_creates_alarm_and_live_widget(set_timer):
    events = queue.Queue()
    ctx = action_bus.ActionContext(
        "SET_TIMER",
        {"duration": "15m", "label": "Study", "display": "true"},
        events, ROOT, response_text='<ACTION type="SET_TIMER" duration="15m" />')
    action_bus.BUS.dispatch(ctx)
    found = []
    while not events.empty():
        found.append(events.get_nowait())
    assert ctx.receipt.startswith("[Success]")
    assert any(kind == "show_widget" and value["kind"] == "timer"
               for kind, value in found)
    set_timer.assert_called_once_with(ROOT, "15m", "Study")
