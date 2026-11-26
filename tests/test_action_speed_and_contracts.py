from queue import Queue
from types import SimpleNamespace
from unittest import mock
import pytest
import action_schema

import action_bus
import actions
import performance_metrics


def test_legacy_receipts_are_checked_by_the_real_contract():
    bus = action_bus.ActionBus()
    legacy = SimpleNamespace(_last_output='[Success] stale id=old')

    @bus.register('SAMPLE', verify=action_bus.must_cite_evidence)
    def handler(ctx):
        ctx.legacy._last_output = '[Success] done'

    ctx = action_bus.ActionContext('SAMPLE', {}, Queue(), '.', legacy=legacy)
    bus.dispatch(ctx)
    assert ctx.receipt.startswith('[Unverified]')
    assert legacy._last_output == ctx.receipt


def test_a_missing_legacy_receipt_cannot_reuse_an_older_success():
    bus = action_bus.ActionBus()
    bus.register('SAMPLE')(lambda ctx: None)
    ctx = action_bus.ActionContext('SAMPLE', {}, Queue(), '.',
                                  legacy=SimpleNamespace(_last_output='[Success] id=old'))
    bus.dispatch(ctx)
    assert not ctx.receipt and ctx.legacy._last_output is None


def test_verifier_failure_preserves_evidence_and_marks_it_uncertain():
    bus = action_bus.ActionBus()

    def broken(ctx, text):
        raise ValueError('synthetic verifier failure')

    bus.register('SAMPLE', verify=broken)(lambda ctx: ctx.report('[Success] id=123'))
    ctx = action_bus.ActionContext('SAMPLE', {}, Queue(), '.')
    bus.dispatch(ctx)
    assert ctx.receipt.startswith('[Unverified]') and 'id=123' in ctx.receipt


def test_api_reads_do_not_pay_a_desktop_settle_delay():
    actions.reset_task_ledger('synthetic read latency')
    with mock.patch.object(actions, '_action_allowed', return_value=(True, None)), \
         mock.patch.object(actions, '_policy_confirmation_required', return_value=False), \
         mock.patch.object(actions, '_observer_mode_block', return_value=None), \
         mock.patch('authentication_gate.awaiting', return_value=False), \
         mock.patch.object(actions, 'announce_action'), \
         mock.patch.object(actions.action_bus.BUS, 'dispatch', side_effect=lambda ctx: ctx.report('0 entries')), \
         mock.patch.object(actions.time, 'sleep') as sleep:
        actions.execute_actions('<ACTION type="LIST_DIRECTORY" path="." />', {}, Queue(), '.')
    assert actions.execute_actions._results[0]['ok']
    sleep.assert_not_called()
    assert {'CLICK', 'TYPE', 'LAUNCH_APP'} <= actions.DESKTOP_SETTLE_ACTIONS
    assert not {'READ_EMAILS', 'LIST_DIRECTORY', 'BROWSER_CLICK', 'RUN_PYTHON'} & actions.DESKTOP_SETTLE_ACTIONS


def test_performance_samples_are_bounded_and_contain_only_metadata():
    performance_metrics.reset()
    for _ in range(700):
        performance_metrics.record('model', 'routine', .2, ok=True, first_content=.05, provider='fixture')
    rows = performance_metrics.snapshot()
    assert len(rows) == 512
    assert rows[-1]['elapsed_ms'] == 200
    assert rows[-1]['first_content_ms'] == 50
    assert set(rows[-1]) == {'kind', 'name', 'ok', 'elapsed_ms', 'first_content_ms', 'provider'}
    assert performance_metrics.summary()[0]['count'] == 512
    performance_metrics.reset()


@pytest.mark.parametrize('name', sorted(action_schema.ACTION_SPECS))
def test_every_action_rejects_unknown_inputs_before_dispatch(name, tmp_path):
    spec = action_schema.ACTION_SPECS[name]
    attrs = {key: '1' for key in spec.get('requires', [])}
    for key in (spec.get('any_of') or [[]])[0]:
        if key != '__body__':
            attrs[key] = '1'
    args = ' '.join(f'{key}="{value}"' for key, value in attrs.items())
    text = f'<ACTION type="{name}" {args} definitely_misspelled="1">sample</ACTION>'
    actions.reset_task_ledger('synthetic invalid input')
    with mock.patch.object(actions, '_action_allowed', return_value=(True, None)), \
         mock.patch.object(actions, '_observer_mode_block', return_value=None), \
         mock.patch('authentication_gate.awaiting', return_value=False), \
         mock.patch.object(actions.action_bus.BUS, 'dispatch') as dispatch, \
         mock.patch.object(actions, '_queue_for_approval') as approval, \
         mock.patch.object(actions, 'announce_action') as announce:
        actions.execute_actions(text, {}, Queue(), str(tmp_path))
    dispatch.assert_not_called()
    approval.assert_not_called()
    announce.assert_not_called()
    assert actions.execute_actions._results
    assert all(not row['ok'] for row in actions.execute_actions._results)


def test_percentile_uses_nearest_rank_without_off_by_one():
    performance_metrics.reset()
    for value in range(1, 21):
        performance_metrics.record('action', 'fixture', value / 1000, ok=True)
    assert performance_metrics.summary()[0]['p95_ms'] == 19
    performance_metrics.reset()
