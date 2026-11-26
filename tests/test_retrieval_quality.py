import threading
import time
from unittest import mock

from memory import orchestrator
from memory.ranking import rank_facts
from memory.neo4j_store import display_name
from providers import research_api as web


def fact(subject, predicate, obj, **metadata):
    return dict(subject=subject, predicate=predicate, object=obj, **metadata)


def test_named_locator_outranks_many_unrelated_high_authority_facts():
    wanted = fact('Himanshu', 'HAS_PORTFOLIO_WEBSITE', 'https://example.org/My_Portfolio')
    noise = [fact('Himanshu', 'RECEIVED_EMAIL', f'news number {n}', authority=100,
                  mention_count=10000) for n in range(50)]
    assert rank_facts(noise + [wanted], 'my portfolio website', 1) == [wanted]
    assert display_name(wanted['object']) == wanted['object']


def test_unrelated_facts_do_not_fill_unused_context_slots():
    language = fact('Himanshu', 'SPEAKS_LANGUAGE', 'Telugu')
    unrelated = fact('Himanshu', 'HAS_PORTFOLIO_WEBSITE', 'https://example.org')
    assert rank_facts([unrelated, language], 'which language do I speak', 12) == [language]
    assert not orchestrator._needs_resource_locators('report the weather')


def test_duplicate_claims_do_not_fill_the_context():
    rows = [fact('Person', 'HAS_ROLE', 'engineer', authority=a) for a in (50, 100)]
    ranked = rank_facts(rows, 'role', 10)
    assert len(ranked) == 1 and ranked[0]['authority'] == 100


def test_multihop_prefers_relevant_bridge_over_contact_fanout():
    edges = [('Himanshu', 'HAS_CONTACT', f'Contact {i}') for i in range(100)]
    edges.append(('Himanshu', 'HAS_FATHER', 'Parent'))
    with mock.patch.object(orchestrator, 'get_neighborhood', return_value=edges):
        anchors = orchestrator._expand_entities(['Himanshu'], 2, "my father's phone")
    assert anchors == ['Himanshu', 'Parent']


def test_empty_privacy_allowlist_and_expired_facts_are_respected():
    row = fact('Person', 'HAS_ROLE', 'engineer', sensitivity='private')
    assert orchestrator._filter_facts([row], 'role', 'user', set()) == []
    expired = dict(row, valid_to='2026-01-01')
    assert orchestrator._filter_facts([expired], 'role', 'user') == []
    assert orchestrator._filter_facts(
        [dict(row, sensitivity='restricted')], 'role', 'proactive', {'restricted'}) == []


def test_retrieval_deadline_does_not_wait_for_slow_worker():
    release = threading.Event()
    def stalled():
        release.wait(2)
        return 'late'
    started = time.monotonic()
    try:
        values, failures = orchestrator._collect_lookups(
            {'slow': (stalled, ()), 'fast': (lambda: 'ready', ())}, .05)
        assert time.monotonic() - started < .3
        assert failures == ['slow']
        assert values['fast'] == 'ready'
    finally:
        release.set()


def test_semantic_memory_cannot_bypass_evidence_and_privacy_filters():
    rows = [dict(kind='bootstrap_evidence', content='raw'),
            dict(kind='bootstrap_answer', content='raw answer'),
            dict(kind='profile', active=False, content='old'),
            dict(kind='personal_profile', status='historical', content='historical profile'),
            dict(kind='file', sensitivity='restricted', content='secret'),
            dict(kind='file', title='useful', content='approved source')]
    with mock.patch.object(orchestrator.store, 'search_memories', return_value=rows):
        result = orchestrator._run_semantic_lookup('query', 4)
    assert 'approved source' in result
    assert 'secret' not in result and 'raw' not in result and 'old' not in result
    assert 'historical profile' not in result


def test_graph_context_retains_source_and_fact_identity():
    text = orchestrator._format_graph_groups([fact(
        'Person', 'HAS_ROLE', 'engineer', fact_id='f1', source_id='interview:q2',
        confidence=.95, evidence_type='user_stated', authority=100)])
    assert 'interview:q2' in text and 'f1' in text and 'user_stated' in text


def response(rows, provider='tavily'):
    return dict(providers=[provider], results=rows)


def page(url, title='Portfolio', content='portfolio evidence', **metadata):
    return dict(url=url, title=title, content=content, **metadata)


def test_web_merges_corroborating_providers_without_losing_richer_text():
    data = web._merge_results('portfolio', [
        response([page('https://example.org/Portfolio?utm_source=x', content='short')]),
        response([page('https://example.org/Portfolio', content='longer source evidence')], 'google')], 5)
    assert len(data['results']) == 1
    row = data['results'][0]
    assert row['providers'] == ['google', 'tavily']
    assert row['content'] == 'longer source evidence'
    assert row['source_id'] == 'S1'


def test_web_path_case_and_query_values_are_not_collapsed():
    data = web._merge_results('test', [response([
        page('https://EXAMPLE.org/Read?token=A'),
        page('https://example.org/read?token=a'),
        page('javascript:alert(1)'), page('https://user:pass@example.org/')])], 10)
    assert len(data['results']) == 2


def test_domain_constraints_apply_to_all_providers_and_subdomains():
    data = web._merge_results('test', [response([
        page('https://docs.example.org/a'), page('https://example.org.evil.org/a'),
        page('https://blog.example.org/a')], 'google')], 5,
        include_domains=['example.org'], exclude_domains=['blog.example.org'])
    assert [r['url'] for r in data['results']] == ['https://docs.example.org/a']


def test_specific_source_beats_unrelated_top_provider_result():
    data = web._merge_results('transformer attention architecture', [response([
        page('https://example.org/ads', 'Sale', 'unrelated', score=1),
        page('https://example.org/paper', 'Transformer attention architecture',
             'Attention mechanism architecture evidence', score=.1)])], 2)
    assert data['results'][0]['url'].endswith('/paper')


def test_all_requested_dates_required_and_never_claim_verification():
    data = dict(query='travel 2026-11-01 to 2026-11-10', results=[
        page('https://example.org', 'Fare 2026-11-01', 'price estimate')])
    evidence = web.assess_evidence(data)
    assert evidence['matching_date_sources'] == 0
    assert not evidence['verified_for_requested_date']
    assert not web.assess_evidence(dict(query='news', results=data['results']))['verified_for_requested_date']


def test_context_budget_keeps_all_sources_and_late_relevant_passage():
    data = dict(query='special evidence', results=[
        page(f'https://example.org/{i}', f'Source {i}',
             'navigation ' * 1000 + 'special evidence: conclusion') for i in range(5)])
    text = web.format_for_model(data, max_chars=4500)
    assert all(f'https://example.org/{i}' in text for i in range(5))
    assert 'special evidence: conclusion' in text


def test_search_receipt_still_renders_as_structured_source_cards():
    from result_presenter import present
    data = dict(query='test', answer='A useful summary', results=[
        page('https://example.org/a', 'First source'),
        page('https://example.org/b', 'Second source')])
    rendered = present('SEARCH_WEB', web.format_for_model(data))
    assert len(rendered['sources']) == 2
    assert 'A useful summary' in rendered['summary']
    assert rendered['status'] == 'SEARCH RESULTS'


def test_cached_payloads_are_isolated_from_callers():
    web._remember('quality-isolation', dict(results=[dict(content='original')]))
    first = web._cached('quality-isolation')
    first['results'][0]['content'] = 'modified'
    assert web._cached('quality-isolation')['results'][0]['content'] == 'original'
    web._cache.pop('quality-isolation')


def test_all_provider_mode_uses_google_when_only_google_is_configured(monkeypatch):
    monkeypatch.delenv('TAVILY_API_KEY', raising=False)
    monkeypatch.setenv('SERPAPI_API_KEY', 'synthetic')
    with mock.patch.object(web, '_google_search', return_value=response([
            page('https://docs.example.org/api')], 'google')) as google:
        data = web.advanced_search('unique google only', provider='all',
                                   include_domains=['example.org'])
    assert data['results']
    assert 'site:example.org' in google.call_args.args[0]


def test_empty_primary_results_trigger_fallback(monkeypatch):
    monkeypatch.setenv('SERPAPI_API_KEY', 'synthetic')
    with mock.patch.object(web, 'search', return_value=response([])), \
         mock.patch.object(web, '_google_search', return_value=response([
             page('https://example.org/fallback')], 'google')) as google:
        data = web.advanced_search('unique empty fallback')
    assert data['results'][0]['url'].endswith('/fallback')
    assert google.call_count == 1


def test_http_auth_failures_are_not_retried_or_logged_with_secrets(monkeypatch):
    import requests
    monkeypatch.setenv('TAVILY_API_KEY', 'synthetic-secret')
    denied = mock.Mock(status_code=401)
    denied.raise_for_status.side_effect = requests.HTTPError('bad synthetic-secret')
    with mock.patch('requests.post', return_value=denied) as post:
        try:
            web.search('test')
        except web.SearchUnavailable as exc:
            assert 'synthetic-secret' not in str(exc)
        else:
            raise AssertionError('expected SearchUnavailable')
    assert post.call_count == 1
