"""Durable memory failures from reasoning output and retry backlogs; no live DB."""
import ast
import json
import queue
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

from memory import consolidator


FACT = {'subject': 'Himanshu', 'predicate': 'LIKES', 'object': 'Coffee', 'confidence': 1, 'quote': 'I like Coffee'}


@pytest.mark.parametrize('text', [
    '<think>Maybe return []; the prompt has [examples].</think>\n' + json.dumps([FACT]),
    '<think>Reasoning quotes [{"subject":"Not the user"}]</think>\n```json\n' + json.dumps([FACT]) + '\n```',
    'Here are the facts:\n' + json.dumps([FACT]),
    json.dumps({'facts': [FACT]}), json.dumps({'triples': [FACT]}),
])
def test_reasoning_and_fences_do_not_corrupt_complete_facts(text):
    assert consolidator._parse_llm_json(text) == [FACT]


@pytest.mark.parametrize('text', [
    '<think>Unfinished private reasoning with []',
    '<think>[{"subject":"Private","predicate":"IS_A","object":"Thought"}]</think>',
    '[{"subject":"Truncated","extra":' + json.dumps([FACT]),
    json.dumps([FACT])[:-2], '[]\n' + json.dumps([FACT]),
    '["not a fact"]', '[{"confidence":NaN}]', '{}', 'not json',
])
def test_invalid_ambiguous_and_truncated_outputs_are_not_treated_as_no_facts(text):
    assert consolidator._parse_llm_json(text) is None


def test_actual_empty_answer_is_successfully_empty():
    assert consolidator._parse_llm_json('<think>There are no durable facts [] here.</think>\n[]') == []


def test_cooldown_is_exponential_bounded_and_logging_is_throttled():
    clock = [0.]
    pacer = consolidator.ExtractionPacer(clock=lambda: clock[0], jitter=lambda: 1)
    assert pacer.failed() == (2., True)
    assert pacer.failed() == (4., False)
    clock[0] = 31
    assert pacer.failed() == (8., True)
    for _ in range(30):
        delay, _ = pacer.failed()
        assert delay <= 60
    waits = []
    def wait(seconds):
        waits.append(seconds)
        clock[0] += seconds
    with mock.patch.object(consolidator.threading, 'Event') as event:
        event.return_value.wait.side_effect = wait
        pacer.wait()
    assert waits and all(n <= 30 for n in waits)
    pacer.succeeded()
    assert pacer.failures == 0 and pacer.next_at == 0


def test_retry_callbacks_keep_their_own_source_and_never_mark_invalid_output_complete(monkeypatch):
    work = queue.Queue()
    work.put(('First explicit statement', 'source-a', 'personal', 0))
    work.put(('Second explicit statement', 'source-b', 'personal', 0))
    work.put(None)
    callbacks = []
    def timer(seconds, callback):
        callbacks.append(callback)
        return SimpleNamespace(start=lambda: None, daemon=False)
    store = mock.Mock()
    store.claim_fact_extraction.return_value = None
    pacer = mock.Mock()
    pacer.failed.return_value = (2, False)
    model = mock.Mock(return_value='invalid answer')
    monkeypatch.setattr(consolidator, '_consolidation_queue', work)
    monkeypatch.setattr(consolidator, 'store', store)
    monkeypatch.setattr(consolidator, '_extraction_pacer', pacer)
    monkeypatch.setattr(consolidator, 'call_llm', model)
    monkeypatch.setattr(consolidator.threading, 'Timer', timer)
    consolidator._consolidation_worker()
    assert work.unfinished_tasks == 0
    assert pacer.wait.call_count == 2 and not pacer.succeeded.called
    assert [c.args[0] for c in store.finish_fact_extraction.call_args_list] == ['source-a', 'source-b']
    assert all(c.kwargs['status'] == 'retry' for c in store.finish_fact_extraction.call_args_list)
    assert all(c.kwargs['task_level'] == 'memory_extraction' for c in model.call_args_list)
    for callback in callbacks:
        callback()
    assert work.get_nowait()[1] == 'source-a'
    assert work.get_nowait()[1] == 'source-b'


def test_pacer_yields_to_foreground_without_requiring_twenty_minutes_idle(monkeypatch):
    import idle_gate
    busy = mock.Mock(side_effect=[True, True, False])
    monkeypatch.setattr(idle_gate, 'agent_busy', busy)
    with mock.patch.object(consolidator.threading, 'Event') as event:
        consolidator.ExtractionPacer().wait()
    assert event.return_value.wait.call_args_list == [mock.call(.25), mock.call(.25)]


@pytest.mark.parametrize('verified', [True, False])
def test_only_verified_graph_writes_complete_durable_extraction(monkeypatch, verified):
    import memory.retention
    work = queue.Queue()
    work.put(('I like Coffee', 'source-test', 'personal', 0))
    work.put(None)
    store, pacer = mock.Mock(), mock.Mock()
    store.claim_fact_extraction.return_value = None
    pacer.failed.return_value = (2, False)
    monkeypatch.setattr(consolidator, '_consolidation_queue', work)
    monkeypatch.setattr(consolidator, 'store', store)
    monkeypatch.setattr(consolidator, '_extraction_pacer', pacer)
    monkeypatch.setattr(consolidator, 'call_llm', mock.Mock(return_value=json.dumps([FACT])))
    monkeypatch.setattr(consolidator, 'add_edge', mock.Mock(return_value='Saved'))
    monkeypatch.setattr(consolidator, 'get_current_edges', lambda subject: [('Himanshu', 'LIKES', 'Coffee')] if verified else [])
    monkeypatch.setattr(memory.retention, 'normalize_candidate', lambda value, text: value)
    monkeypatch.setattr(memory.retention, 'apply_operational_memory', mock.Mock(return_value=None))
    monkeypatch.setattr(consolidator.threading, 'Timer', mock.Mock())
    consolidator._consolidation_worker()
    state = store.finish_fact_extraction.call_args.kwargs['status']
    assert state == ('complete' if verified else 'retry')
    assert pacer.succeeded.called is verified


@pytest.mark.parametrize('origin,startup,presentation,report,expected', [
    ('proactive', True, 'report', 'Briefing', True),
    ('proactive', False, 'report', 'Ordinary background event', False),
    ('user', False, 'report', '', True),
    ('user', False, 'inline', '', False),
])
def test_actual_report_gate_allows_startup_but_not_routine_proactive_windows(origin, startup, presentation, report, expected):
    tree = ast.parse(Path('agent.py').read_text(encoding='utf-8'))
    assignment = next(n for n in ast.walk(tree) if isinstance(n, ast.Assign)
                      and any(isinstance(t, ast.Name) and t.id == 'wants_report_surface' for t in n.targets))
    expression = ast.Expression(body=assignment.value)
    actual = eval(compile(expression, 'agent.py', 'eval'), {
        'origin': origin, 'is_startup_turn': startup, 'display_report': report,
        'response': f'<REPORT>{report}</REPORT>' if report else '',
        'plan': SimpleNamespace(needed_catalogs=[]),
        'report_surface_requested': __import__('conversation_output').wants_report_surface,
        'cognitive_route': SimpleNamespace(presentation=presentation)})
    assert actual is expected
