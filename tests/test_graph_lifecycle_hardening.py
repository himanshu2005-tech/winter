"""Graph lifecycle boundaries; no production database or network."""
from unittest.mock import Mock, patch
import pytest

from memory.fact_validation import grounded_candidate
from memory.neo4j_store import GraphStore


@pytest.mark.parametrize('confidence', [float('nan'), float('inf'), -1, 1.01, True])
def test_bad_confidence_cannot_touch_database(confidence):
    graph = GraphStore()
    graph.query = Mock(side_effect=AssertionError('unexpected database access'))
    with pytest.raises(ValueError):
        graph.add_fact('User', 'LIKES', 'Coffee', confidence)
    graph.query.assert_not_called()


@pytest.mark.parametrize('subject,predicate,obj,metadata', [
    ('', 'LIKES', 'Coffee', {}), ('User', 'LIKES', '', {}),
    ('User', '!!!', 'Coffee', {}),
    ('User', 'LIKES', 'Coffee', {'sensitivity': 'secretish'}),
])
def test_invalid_fact_rejected_before_entity_creation(subject, predicate, obj, metadata):
    graph = GraphStore()
    graph.query = Mock()
    with pytest.raises(ValueError):
        graph.add_fact(subject, predicate, obj, **metadata)
    graph.query.assert_not_called()


def test_only_grounded_durable_candidates_are_admitted():
    assert grounded_candidate({'quote': 'I like coffee'}, 'Today: I like coffee.')
    assert not grounded_candidate({'quote': 'I like coffee', 'retention': 'transient'}, 'I like coffee')
    assert not grounded_candidate({'quote': 'I like tea'}, 'I like coffee')
    assert not grounded_candidate({}, 'I like coffee')


def test_forget_without_target_is_rejected_at_store_boundary():
    graph = GraphStore()
    graph.query = Mock()
    with pytest.raises(ValueError):
        graph.invalidate_fact('User')
    graph.query.assert_not_called()


@pytest.mark.parametrize('status', ['rejected', 'suspended', 'withdrawn'])
def test_rejected_claims_cannot_reenter_context_even_when_patterns_requested(status):
    from memory.orchestrator import _filter_facts
    assert _filter_facts([{'fact_class': 'derived_inference', 'review_status': status}],
                         'possible patterns', 'user', include_pending=True) == []


@pytest.mark.parametrize('fail_write', [True, False])
def test_correction_uses_one_transaction_and_preserves_privacy(fail_write):
    """The fake transaction commits its snapshot only if the callback returns."""
    graph = GraphStore()
    state = ['old']
    tx = Mock()
    session = Mock()
    session.__enter__ = Mock(return_value=session)
    session.__exit__ = Mock(return_value=False)
    graph._get_driver = Mock(return_value=Mock(session=Mock(return_value=session)))
    graph._database = lambda: 'isolated'
    def transact(callback):
        snapshot = state[:]
        try:
            return callback(tx)
        except Exception:
            state[:] = snapshot
            raise
    session.execute_write.side_effect = transact
    def retire(self, *args):
        state.clear()
        return [{'sensitivity': 'restricted'}]
    def write(self, *args, **kwargs):
        assert kwargs['sensitivity'] == 'restricted'
        if fail_write:
            raise RuntimeError('simulated failure')
        state.append('new')
        return 'Added fact'
    with patch.object(GraphStore, 'invalidate_fact', retire), patch.object(GraphStore, 'add_fact', write):
        if fail_write:
            with pytest.raises(RuntimeError):
                graph.correct_fact('User', 'LIKES', 'Tea', 'Coffee')
            assert state == ['old']
        else:
            assert graph.correct_fact('User', 'LIKES', 'Tea', 'Coffee')['retired'] == 1
            assert state == ['new']
    session.execute_write.assert_called_once()


def test_duplicate_cleanup_retains_history_and_checks_all_metadata():
    from dedupe_graph import COLLAPSE
    assert 'DELETE' not in COLLAPSE
    assert 'keys(e) + keys(keep)' in COLLAPSE
    assert 'doomed.valid_to=' in COLLAPSE


def test_low_authority_cannot_replace_explicit_claim_and_privacy_never_downgrades():
    graph = GraphStore()
    graph.canonical_name = lambda text: text
    graph.query = Mock(return_value=[])
    graph.add_fact('User', 'LIKES', 'Tea', authority=50, sensitivity='public')
    query = graph.query.call_args.args[0]
    assert 'coalesce(old.authority,50)<=$authority' in query
    assert "coalesce(cur.sensitivity,'private')='restricted'" in query
    assert 'cur.fact_class=CASE WHEN $authority>coalesce(cur.authority,0)' in query


def test_changed_bootstrap_manifest_cannot_use_old_approval():
    from memory.bootstrap_importer import _hash, commit_manifest
    manifest = {'interview_id': 'synthetic', 'mutations': {'create_facts': []}}
    digest = _hash(manifest)
    manifest.update(manifest_hash=digest, import_run_id=f'import:synthetic:{digest[:16]}', status='dry_run')
    manifest['mutations']['create_facts'].append({'object': 'tampered'})
    graph = Mock()
    with pytest.raises(ValueError, match='changed after review'):
        commit_manifest(manifest, digest, graph)
    graph._get_driver.assert_not_called()


@pytest.mark.parametrize('old_object,retired', [
    ('outdated selector', []),
    (None, [{'object': 'Coffee'}, {'object': 'Tea'}]),
])
def test_correction_rejects_stale_or_ambiguous_targets(old_object, retired):
    graph = GraphStore()
    session = Mock()
    session.__enter__ = Mock(return_value=session)
    session.__exit__ = Mock(return_value=False)
    session.execute_write.side_effect = lambda callback: callback(Mock())
    graph._get_driver = Mock(return_value=Mock(session=Mock(return_value=session)))
    graph._database = lambda: 'isolated'
    with patch.object(GraphStore, 'invalidate_fact', return_value=retired), patch.object(GraphStore, 'add_fact') as write:
        with pytest.raises(ValueError):
            graph.correct_fact('User', 'LIKES', 'Juice', old_object)
        write.assert_not_called()


def test_bootstrap_transaction_cannot_mutate_reviewed_manifest():
    from memory.bootstrap_importer import _hash, commit_manifest
    manifest = {'interview_id': 'synthetic', 'mutations': {'create_facts': []}}
    digest = _hash(manifest)
    manifest.update(manifest_hash=digest, import_run_id=f'import:synthetic:{digest[:16]}', status='dry_run')
    graph, session = Mock(), Mock()
    session.__enter__ = Mock(return_value=session)
    session.__exit__ = Mock(return_value=False)
    graph._get_driver.return_value.session.return_value = session
    session.execute_write.side_effect = lambda callback: callback(Mock())
    def commit(tx, transaction_manifest):
        transaction_manifest['mutations']['create_facts'].append({'supersedes_fact_id': 'history'})
        return {'status': 'synthetic'}
    with patch('memory.bootstrap_importer._tx_commit', side_effect=commit):
        assert commit_manifest(manifest, digest, graph)['status'] == 'synthetic'
    assert manifest['mutations']['create_facts'] == []


def test_grounding_handles_real_multiline_quotes():
    from memory.retention import normalize_candidate
    candidate = normalize_candidate({'quote': 'I prefer\n quiet rooms', 'confidence': 1},
                                    'I prefer\n quiet rooms')
    assert grounded_candidate(candidate, 'I prefer\n quiet rooms')
