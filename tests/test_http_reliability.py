from unittest import mock
import pytest
import requests

from providers.http_client import get_json


def response(status, data=None, headers=None):
    result = mock.Mock(status_code=status, headers=headers or {})
    result.json.return_value = data or {'ok': True}
    return result


@pytest.mark.parametrize('status', [400, 401, 403, 404, 422])
def test_permanent_http_errors_do_not_retry_or_expose_urls(status):
    with mock.patch('providers.http_client.requests.get', return_value=response(status)) as get, \
         mock.patch('providers.http_client.time.sleep') as sleep:
        with pytest.raises(RuntimeError) as failure:
            get_json('https://fixture.invalid/?token=secret-value')
    assert str(status) in str(failure.value) and 'secret-value' not in str(failure.value)
    get.assert_called_once()
    sleep.assert_not_called()


def test_transient_error_retries_and_returns_recovery():
    with mock.patch('providers.http_client.requests.get', side_effect=[response(503), response(200)]) as get, \
         mock.patch('providers.http_client.time.sleep') as sleep:
        assert get_json('https://fixture.invalid/')['ok']
    assert get.call_count == 2 and sleep.call_count == 1


def test_rate_limit_long_retry_after_is_not_retried_early():
    with mock.patch('providers.http_client.requests.get', return_value=response(429, headers={'Retry-After': '60'})) as get, \
         mock.patch('providers.http_client.time.sleep') as sleep:
        with pytest.raises(RuntimeError, match='retry after 60s'):
            get_json('https://fixture.invalid/')
    get.assert_called_once()
    sleep.assert_not_called()


def test_exception_url_is_not_returned_to_model_context():
    with mock.patch('providers.http_client.requests.get', side_effect=requests.ConnectionError('https://fixture.invalid?token=secret-value')), \
         mock.patch('providers.http_client.time.sleep'):
        with pytest.raises(RuntimeError) as failure:
            get_json('https://fixture.invalid/', attempts=99)
    assert '5 attempts' in str(failure.value)
    assert 'ConnectionError' in str(failure.value) and 'secret-value' not in str(failure.value)


@pytest.mark.parametrize('timeout', [float('nan'), float('inf'), 0, -1])
def test_invalid_timeout_never_starts_network_io(timeout):
    with mock.patch('providers.http_client.requests.get') as get:
        with pytest.raises(ValueError):
            get_json('https://fixture.invalid/', timeout=timeout)
    get.assert_not_called()
