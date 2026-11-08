"""No hardware mutations: provider scripts and readback tested with mocks."""
import json
import queue
from types import SimpleNamespace
from unittest.mock import Mock, patch
import pytest
import windows_radios


@pytest.mark.parametrize('kind,state', [('wifi;exit', 'off'), ('wifi', 'off;exit'), ('all','off')])
def test_invalid_or_broad_mutations_never_produce_commands(kind, state):
    with pytest.raises(ValueError):
        windows_radios.command(kind, state)


def test_real_radio_api_is_used_with_state_readback():
    script = windows_radios.command('bluetooth', 'off')
    for text in ['GetRadiosAsync', 'RequestAccessAsync', 'SetStateAsync', '$radio.State -ne $desired']:
        assert text in script
    assert 'Disable-PnpDevice' not in script
    assert "$kind = 'bluetooth'" in script


@pytest.mark.parametrize('output', ['[Error] Windows denied permission', '', '{}',
    '{"verified":true,"radios":[{"name":"Wi-Fi","kind":"WiFi","state":"On"}]}'])
def test_errors_or_stale_readback_never_claim_radio_off(output):
    assert windows_radios.receipt(output, 'wifi', 'off').startswith('[Error]')


def test_verified_receipt_is_about_radio_not_device_connection():
    output = json.dumps({'verified': True, 'radios': [{'name': 'adapter', 'kind': 'WiFi', 'state': 'On'}]})
    result = windows_radios.receipt(output, 'wifi', 'on')
    assert result.startswith('[Success]')
    assert 'connected' not in result


def test_capability_is_authorized_discoverable_and_not_proactive():
    import capabilities, autonomy, action_registry
    cap = capabilities.get('WINDOWS_RADIO')
    assert cap and cap.verification == 'readback'
    assert autonomy.action_allowed('WINDOWS_RADIO', 'user')[0]
    assert not autonomy.action_allowed('WINDOWS_RADIO', 'proactive')[0]
    action_registry.configure(capabilities.catalog_overlay())
    for query in ['turn off Bluetooth', 'disconnect my wifi', 'enable wireless radio']:
        assert 'WINDOWS_RADIO' in [r['name'] for r in action_registry.search('.', query, limit=4)]


def test_wifi_off_readback_has_local_delivery_without_another_cloud_call():
    import actions
    attrs = {'kind': 'wifi', 'state': 'off'}
    ctx = SimpleNamespace(attrs=attrs)
    def shell(context):
        assert 'SetStateAsync' in context.attrs['cmd']
        actions.execute_actions._last_output = json.dumps({'verified': True, 'radios': [
            {'name': 'Wi-Fi', 'kind': 'WiFi', 'state': 'Off'}]})
    with patch.object(actions, '_run_run_command', side_effect=shell):
        actions._run_windows_radio(ctx)
    assert ctx.attrs is attrs
    assert 'WiFi' in actions.execute_actions._offline_completion
    assert actions.execute_actions._stop_batch is True
    assert actions.execute_actions._direct_user_delivery.content


def test_general_shell_errors_are_not_masked_by_later_success():
    import actions
    process = Mock(returncode=1)
    process.communicate.return_value = ('', 'Access denied')
    ctx = SimpleNamespace(attrs={'cmd': 'Write-Error denied; Write-Output success'},
        action_type='RUN_COMMAND', ui_queue=queue.Queue(), base_dir='.', origin='user',
        turn_id='', interrupt_event=None, element_map={}, response_text='')
    with patch.object(actions.subprocess, 'Popen', return_value=process) as start:
        actions._run_run_command(ctx)
    assert start.call_args.args[0][-1].startswith("$ErrorActionPreference = 'Stop';")
    assert actions.execute_actions._last_output.startswith('[Error]')
