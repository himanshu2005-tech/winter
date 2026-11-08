"""Contact sync must report actual results and contain systemic failures."""
from unittest.mock import Mock, patch
import pytest


@pytest.mark.parametrize('failure,expected,attempts', [
    (None, 3, 6), (ConnectionError('offline'), 0, 1),
    (ValueError('one bad entry'), 2, 5),
])
def test_sync_counts_only_successes_and_stops_for_systemic_errors(failure, expected, attempts, capsys):
    from providers import google_api
    contacts = [{'names': [{'displayName': f'private-person-{i}'}],
                 'phoneNumbers': [{'canonicalForm': f'+91900000000{i}'}]} for i in range(3)]
    service = Mock()
    service.people.return_value.connections.return_value.list.return_value.execute.return_value = {'connections': contacts}
    edge = Mock(side_effect=[failure] + [None]*6 if failure else None)
    with patch.object(google_api, 'get_credentials', return_value=Mock()), \
         patch('googleapiclient.discovery.build', return_value=service), \
         patch('memory.graph_store.add_edge', edge), \
         patch('memory.entities.get_canonical_name', side_effect=lambda name: name), \
         patch('memory.entities.add_entity'), \
         patch.object(google_api, '_sync_contact_details', return_value=0) as details:
        assert google_api.sync_contacts('.') == expected
    assert edge.call_count == attempts
    if isinstance(failure, ConnectionError):
        details.assert_not_called()
    output = capsys.readouterr().out
    assert 'private-person' not in output
    assert f'Synced {expected}/3' in output


def test_cypher_syntax_error_stops_batch(capsys):
    from providers import google_api
    from neo4j.exceptions import CypherSyntaxError
    error = CypherSyntaxError('invalid statement')
    # Driver errors expose the stable server code through this property.
    error._neo4j_code = 'Neo.ClientError.Statement.SyntaxError'
    service = Mock()
    service.people.return_value.connections.return_value.list.return_value.execute.return_value = {'connections': [
        {'names': [{'displayName': 'private name'}], 'phoneNumbers': [{'value': '12345'}]},
        {'names': [{'displayName': 'second private name'}], 'phoneNumbers': [{'value': '67890'}]}]}
    with patch.object(google_api, 'get_credentials', return_value=Mock()), \
         patch('googleapiclient.discovery.build', return_value=service), \
         patch('memory.graph_store.add_edge', side_effect=error) as edge, \
         patch('memory.entities.get_canonical_name', side_effect=lambda name: name), \
         patch('memory.entities.add_entity'), \
         patch.object(google_api, '_sync_contact_details') as details:
        assert google_api.sync_contacts('.') == 0
    assert edge.call_count == 1
    details.assert_not_called()
    assert 'private name' not in capsys.readouterr().out


def test_graph_sensitivity_query_uses_cypher_negation():
    from scripts.check_graph_queries import captured_queries
    query, params = captured_queries()[0]
    assert "WHEN NOT (coalesce(cur.sensitivity,'private') IN" in query
    assert ' NOT IN ' not in query
    assert params['sensitivity'] == 'private'
