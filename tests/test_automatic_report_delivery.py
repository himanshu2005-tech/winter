"""The spoken digest must have a real destination for the omitted details."""
import queue
from unittest.mock import Mock, patch
import pytest
from conversation_output import spoken_digest, wants_report_surface, dashboard_response_text


PLAN = '''You have twenty days. Choose one concrete outcome and keep room for rest.

## Build something useful
Choose a project you already care about. Define a small finished version first:
one working flow, a short demonstration and a clear explanation of what it does.
Spend the first few days inspecting what already works and identifying the
smallest missing piece. Build that piece, test it and write down the result.

## Learn with evidence
Pick one topic connected to that project. Work through an example, explain it
in your own words and then implement a small experiment. Use the result to
choose the next question, rather than collecting courses without finishing any.

## Keep the plan realistic
Leave time for sleep, exercise and people you want to see. Review progress at
the end of each week. If the project is taking longer than expected, reduce
scope and keep the demonstration working. Treat the final days as time for
testing and presenting your work, with a small buffer for unexpected problems.
'''


def test_long_inline_answer_gets_a_report_without_any_catalog_or_keyword():
    assert wants_report_surface(PLAN, presentation='inline', catalogs=[])
    assert not wants_report_surface('You have twenty days. Choose one useful project.')
    assert not wants_report_surface('Ready')


def test_unwrapped_diagrams_keep_their_renderable_fences():
    content = '## Flow\n\n```mermaid\ngraph LR\nA[Input] --> B[Output]\n```'
    assert '```mermaid' in dashboard_response_text(content)
    assert wants_report_surface(content)


@pytest.mark.parametrize('body', ['- one\n- two\n- three', '| Item | Price |\n|---|---|\n| A | 12 |', '$$\nx^2\n$$'])
def test_reading_material_gets_a_surface_even_when_it_is_short(body):
    assert wants_report_surface(body)


def test_speech_cannot_invent_a_screen_pointer_without_a_destination():
    text = 'Here are three useful choices.\n- Build a project\n- Learn one topic\n- Rest'
    assert 'on your screen' not in spoken_digest(text)
    assert 'on your screen' in spoken_digest(text, screen_available=True)
    assert 'on your screen' not in spoken_digest('The details are on your screen.')


def test_verified_delivery_enqueues_the_complete_report_before_speaking():
    import agent
    events = queue.Queue()
    def speak(*args, **kwargs):
        posted = list(events.queue)
        assert any(kind == 'show_result' and payload['content'] == PLAN.strip() for kind, payload in posted)
    with patch.object(agent, 'ui_queue', events), patch.object(agent, 'speak', side_effect=speak), \
         patch.object(agent, '_was_already_spoken', return_value=False), \
         patch('session_context.record_assistant') as remember:
        agent._deliver_verified_completion(PLAN, 'Here are a few useful options.', 'user', report_text=PLAN)
        remember.assert_called_once_with(PLAN)


def test_existing_report_is_restored_on_final_delivery_but_not_stream_updates():
    import report_window
    server, proc = Mock(port=12345, token='synthetic'), Mock()
    proc.poll.return_value = None
    with patch.dict(report_window._state, {'server': server, 'proc': proc}), \
         patch.object(report_window, 'browser_binary', return_value='msedge.exe'), \
         patch.object(report_window, '_activate_owned_window', return_value=True) as activate:
        result = report_window.show({'title': 'Plan', 'html': 'real content'})
        assert result['activated'] and not result['launched']
        activate.assert_called_once_with(proc)
        activate.reset_mock()
        report_window.show({'title': 'Plan', 'html': 'preview'}, activate=False)
        activate.assert_not_called()
