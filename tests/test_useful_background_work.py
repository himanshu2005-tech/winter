import json
import queue
import threading
from unittest import mock

import curiosity
import proactive_cognition as pc
from model_invocation import invoke


def learned(base, identifier, topic, summary, shown=0):
    curiosity.record(str(base), dict(id=identifier, at=curiosity._stamp(), topic=topic,
        status='learned', summary=summary, why='Helps current project work',
        usefulness='Resolve a design decision with cited evidence', shown=shown,
        sources=['https://example.org/paper']))


def test_research_is_reused_for_relevant_work_with_its_sources(tmp_path):
    learned(tmp_path, 'a', 'Sparse attention', 'Attention reduces long context memory use.', shown=10)
    learned(tmp_path, 'b', 'Garden soil', 'Soil acidity changes plant growth.')
    block = curiosity.prompt_block(str(tmp_path), query='Explain sparse attention memory use')
    assert 'Attention reduces long context memory use.' in block
    assert 'https://example.org/paper' in block
    assert 'Garden soil' not in block
    assert curiosity.presented_ids() == ('a',)
    curiosity.begin_turn()


def test_irrelevant_turn_does_not_consume_findings(tmp_path):
    learned(tmp_path, 'a', 'Sparse attention', 'Long context optimisation.')
    assert not curiosity.prompt_block(str(tmp_path), query='Thanks Vanta')
    curiosity.note_shown(str(tmp_path), ids=curiosity.presented_ids())
    assert curiosity.log_entries(str(tmp_path))[0]['shown'] == 0
    assert not curiosity.prompt_block(str(tmp_path), query='How are you?')


def test_only_selected_findings_are_marked_exposed(tmp_path):
    for index in range(6):
        learned(tmp_path, str(index), f'topic {index}', 'A prepared public note.')
    curiosity.prompt_block(str(tmp_path), limit=2)
    selected = curiosity.presented_ids()
    curiosity.note_shown(str(tmp_path), ids=selected)
    counts = {entry['id']: entry['shown'] for entry in curiosity.log_entries(str(tmp_path))}
    assert sum(counts.values()) == 2
    curiosity.begin_turn()
    assert not curiosity.presented_ids()


def test_no_failed_write_can_be_counted_as_a_saved_fact():
    store = mock.Mock()
    store.save_knowledge.side_effect = ConnectionError('offline')
    store.add_fact.return_value = '[Error] unavailable'
    receipt = {}
    counts = curiosity.store_findings(store, 's', 'Topic', {
        'summary': 'A sourced summary with enough useful detail for later retrieval.',
        'concepts': [{'name': 'Concept', 'parent': 'Topic', 'definition': 'Definition'}],
        'relations': []}, [{'url': 'https://example.org'}], receipt=receipt)
    assert counts == (0, 0)
    assert not receipt['memory_saved'] and receipt['facts_saved'] == 0


def test_session_does_not_claim_learning_when_graph_is_unavailable(tmp_path, monkeypatch):
    monkeypatch.setattr(curiosity, 'choose_topics', lambda *a, **k: [dict(topic='Topic', why='Project', queries=['topic'])])
    monkeypatch.setattr(curiosity, 'read_sources', lambda *a, **k: [dict(url='https://example.org', content='text')])
    monkeypatch.setattr(curiosity, 'extract', lambda *a, **k: dict(summary='A useful note.', usefulness='Project work', concepts=[], relations=[]))
    store = mock.Mock()
    store.save_knowledge.side_effect = ConnectionError('offline')
    result = curiosity.run_session(str(tmp_path), mock.Mock(), store=store, run=mock.Mock())
    assert result[0]['status'] == 'not saved'
    assert not curiosity.unseen(str(tmp_path))


def decision(**overrides):
    value = dict(decision='investigate', objective='Check the saved opportunity deadline',
                 reason='A relevant source changed', priority='normal', confidence=.9,
                 evidence_refs=['TASKS'], expected_benefit='Avoid missing an application', next_check_minutes=30)
    value.update(overrides)
    return lambda *_a, **_k: json.dumps(value)


def test_rewording_reason_and_alternating_objectives_do_not_repeat_work(tmp_path):
    inbox = queue.Queue()
    for call in (decision(), decision(objective='Check the project dependency'),
                 decision(reason='The same source still matters')):
        pc.evaluate_once(str(tmp_path), call, inbox, lambda: '--- TASKS ---\nA deadline and project')
    assert inbox.qsize() == 2
    assert pc._load_state(str(tmp_path))['last_queue_status'] == 'suppressed_repeat'
    evidence = pc.gather_evidence(str(tmp_path), lambda: '--- TASKS ---')
    assert 'RECENT QUEUED INITIATIVES' in evidence
    assert 'not proof of completion' in evidence
    assert 'Check the saved opportunity deadline' in evidence


def test_real_priority_escalation_can_break_repeat_cooldown_once(tmp_path):
    inbox = queue.Queue()
    for call in (decision(), decision(priority='critical'), decision(priority='critical', reason='Still critical')):
        pc.evaluate_once(str(tmp_path), call, inbox, lambda: '--- TASKS ---\nAn urgent deadline')
    assert inbox.qsize() == 2


def test_nonfinite_confidence_and_missing_reason_do_not_start_work():
    assert not pc.decide('evidence', decision(confidence=float('nan'))).should_queue
    assert not pc.decide('evidence', decision(reason='')).should_queue
    assert pc.decide('evidence', decision(next_check_minutes=float('inf'))).next_check_seconds > 0


def test_director_sees_useful_prepared_knowledge(tmp_path):
    learned(tmp_path, 'a', 'Sparse attention', 'Compare sparse attention before choosing a memory strategy.')
    block = pc.gather_evidence(str(tmp_path), lambda: '--- TASKS ---\nChoose an architecture')
    assert 'PREPARED KNOWLEDGE' in block and 'Compare sparse attention' in block
    assert 'https://example.org/paper' in block


def test_large_live_context_cannot_silently_drop_prepared_knowledge(tmp_path):
    learned(tmp_path, 'a', 'Sparse attention', 'A relevant sourced design result.')
    evidence = pc.gather_evidence(str(tmp_path), lambda: '--- LIVE ---\n' + 'state ' * 10000)
    assert len(evidence) <= pc.MAX_EVIDENCE_CHARS
    assert 'PREPARED KNOWLEDGE' in evidence and 'A relevant sourced design result' in evidence


def test_learning_to_initiative_to_task_context_is_connected(tmp_path, monkeypatch):
    source = dict(url='https://example.org/research', content='Sourced research')
    monkeypatch.setattr(curiosity, 'choose_topics', lambda *a, **k: [dict(topic='Sparse attention', why='Reduce project memory use', queries=['sparse attention'])])
    monkeypatch.setattr(curiosity, 'read_sources', lambda *a, **k: [source])
    monkeypatch.setattr(curiosity, 'extract', lambda *a, **k: dict(
        summary='Sparse attention is relevant to the current memory design decision.',
        usefulness='Compare memory strategies before implementation', concepts=[], relations=[]))
    store = mock.Mock()
    store.save_knowledge.return_value = 1
    entries = curiosity.run_session(str(tmp_path), mock.Mock(), store=store, run=mock.Mock())
    assert entries[0]['status'] == 'learned'
    inbox = queue.Queue()

    def director(messages, **kwargs):
        prompt = messages[-1]['content']
        assert 'current memory design decision' in prompt
        assert '--- TASKS ---' in prompt
        return decision(objective='Compare sourced memory strategies for the project')()

    pc.evaluate_once(str(tmp_path), director, inbox, lambda: '--- TASKS ---\nChoose a memory strategy')
    assert inbox.get_nowait()[0] == 'proactive'
    prepared = curiosity.prompt_block(str(tmp_path), query='Compare sparse attention memory strategies')
    assert 'Sparse attention is relevant' in prepared and source['url'] in prepared
    curiosity.begin_turn()


def test_internal_type_error_never_replays_a_paid_model_call():
    call = mock.Mock(side_effect=TypeError('internal provider failure'))
    try:
        invoke(call, [], task_level='routine', quiet=True)
    except TypeError:
        pass
    call.assert_called_once()


def test_narrow_legacy_model_callable_still_works_once():
    seen = []
    def call(messages):
        seen.append(messages)
        return 'result'
    assert invoke(call, ['context'], task_level='routine', quiet=True) == 'result'
    assert len(seen) == 1


def test_change_bursts_coalesce_before_another_paid_review(tmp_path, monkeypatch):
    now = [0.0]
    stop = threading.Event()
    waits = []
    class Wake:
        def wait(self, seconds):
            waits.append(seconds)
            if len(waits) == 2:
                now[0] = 1
            elif len(waits) == 3:
                now[0] = 60
            elif len(waits) > 3:
                stop.set()
        def clear(self):
            pass
    reviews = mock.Mock(return_value=pc.Decision(next_check_seconds=1800))
    monkeypatch.setattr(pc, '_wake', Wake())
    monkeypatch.setattr(pc.time, 'monotonic', lambda: now[0])
    monkeypatch.setattr(pc, 'MIN_INTERVAL_SECONDS', 60)
    monkeypatch.setattr(pc, 'evaluate_once', reviews)
    pc.worker(str(tmp_path), mock.Mock(), queue.Queue(), lambda: 'evidence',
              stop_event=stop, initial_delay=1)
    assert reviews.call_count == 2
    assert waits[2] == 59
