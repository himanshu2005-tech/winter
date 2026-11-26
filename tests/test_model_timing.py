"""Model timing and failure diagnostics, using only synthetic stream results."""
import threading
from unittest import mock

import pytest

import llm_router
import performance_metrics


@pytest.fixture
def client(monkeypatch):
    performance_metrics.reset()
    connection = mock.Mock()
    connection.with_options.return_value = connection
    monkeypatch.setattr(llm_router, '_candidates', lambda stage: [('fixture', connection, 'fixture-model')])
    monkeypatch.setattr(llm_router, 'active_provider', 'fixture')
    monkeypatch.setattr(llm_router, 'winter_model', 'fixture-model')
    monkeypatch.setattr(llm_router, '_CALL_STATE', threading.local())
    monkeypatch.setattr(llm_router, 'last_failure', '')
    monkeypatch.setattr(llm_router, 'record_usage', mock.Mock())
    monkeypatch.setattr(llm_router, 'estimate_tokens', lambda messages: (1, 0))
    yield connection
    performance_metrics.reset()


def test_first_content_and_elapsed_are_recorded_without_prompt_text(client, monkeypatch):
    delivered = []

    def synthetic(*args, **kwargs):
        kwargs['on_delta']('Synthetic response')
        return 'Synthetic response'

    monkeypatch.setattr(llm_router, '_stream', synthetic)
    result = llm_router.call_llm([{'role': 'user', 'content': 'private synthetic input'}],
                                 task_level='routine', quiet=True, on_delta=delivered.append)
    assert result == 'Synthetic response' and delivered == [result]
    row = performance_metrics.snapshot()[-1]
    assert row['ok'] and 0 <= row['first_content_ms'] <= row['elapsed_ms']
    assert row['name'] == 'routine'
    assert 'private synthetic input' not in str(performance_metrics.snapshot())


def test_timeout_records_failure_and_retains_the_reason(client, monkeypatch):
    monkeypatch.setattr(llm_router, '_stream', mock.Mock(side_effect=TimeoutError('synthetic timeout')))
    assert llm_router.call_llm([], task_level='routine', quiet=True) == llm_router.TIMEOUT_MARKER
    assert not performance_metrics.snapshot()[-1]['ok']
    assert llm_router.failure_kind() == 'timeout'


def test_empty_response_is_a_failure_not_silent_success(client, monkeypatch):
    monkeypatch.setattr(llm_router, '_stream', mock.Mock(return_value=''))
    assert llm_router.call_llm([], task_level='routine', quiet=True) == ''
    assert not performance_metrics.snapshot()[-1]['ok']
    assert 'empty response' in llm_router.last_failure.lower()


def test_memory_extraction_has_its_own_structured_output_budget(client, monkeypatch):
    monkeypatch.delenv('WINTER_BUDGET_MEMORY_EXTRACTION', raising=False)
    def synthetic(*args, **kwargs):
        assert args[4] == .1
        assert args[5] == 4000
        return '[]'
    monkeypatch.setattr(llm_router, '_stream', synthetic)
    assert llm_router.call_llm([], task_level='memory_extraction', quiet=True) == '[]'
    assert performance_metrics.snapshot()[-1]['name'] == 'memory_extraction'


def test_healthy_primary_never_resolves_unused_fallback_catalog(client, monkeypatch):
    monkeypatch.setattr(llm_router, 'active_provider', 'airouter')
    monkeypatch.setattr(llm_router, '_working_provider', None)
    monkeypatch.setattr(llm_router, '_candidates', _real_candidates)
    monkeypatch.setattr(llm_router, '_client_for', lambda provider: client if provider in {'airouter', 'chatgpt'} else None)
    monkeypatch.setattr(llm_router, 'fallback_providers', lambda: [])
    monkeypatch.setattr(llm_router, '_role_provider', lambda stage: '')

    def resolve(provider, stage):
        assert provider == 'airouter', 'Unused ChatGPT fallback catalog was contacted'
        return 'fixture-model'

    monkeypatch.setattr(llm_router, '_model_for_provider', resolve)
    monkeypatch.setattr(llm_router, '_stream', mock.Mock(return_value='Primary answered'))
    assert llm_router.call_llm([], quiet=True) == 'Primary answered'


_real_candidates = llm_router._candidates


def test_fallback_resets_preview_and_ignores_late_provider_deltas(client, monkeypatch):
    monkeypatch.setattr(llm_router, '_working_provider', None)
    monkeypatch.setattr(llm_router, '_preferred_retry_at', 0)
    monkeypatch.setattr(llm_router, '_candidates', lambda stage: [
        ('fixture', client, 'fixture-model'), ('backup', client, 'fixture-model')])
    received, old = [], []

    def delta(text):
        received.append(text)

    delta.reset = lambda: received.append('RESET')

    def synthetic(connection, provider, *args, **kwargs):
        callback = kwargs['on_delta']
        if provider == 'fixture':
            old.append(callback)
            callback('Abandoned draft')
            raise ConnectionError('Synthetic failure')
        old[0]('Late abandoned text')
        callback('Replacement draft')
        return 'Replacement draft'

    monkeypatch.setattr(llm_router, '_stream', synthetic)
    assert llm_router.call_llm([], quiet=True, on_delta=delta) == 'Replacement draft'
    old[0]('Still too late')
    assert received == ['Abandoned draft', 'RESET', 'Replacement draft']
