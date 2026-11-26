import queue
import threading
from unittest import mock

import action_attrs
import action_bus
import action_protocol
import action_registry
import actions
import capability_migration


def test_parser_preserves_order_and_nested_examples_are_data():
    text = ('<ACTION type="WRITE_PROJECT_FILE" path="a.py">'
            'example = \'<ACTION type="SHUTDOWN" />\'\n'
            '</ACTION><ACTION type="READ_FILE" path="a.py" />'
            '<ACTION type="WRITE_PROJECT_FILE" path="b.py">second</ACTION>')
    calls = action_protocol.extract(text)
    assert [action_attrs.parse(c.attributes)['type'] for c in calls] == [
        'WRITE_PROJECT_FILE', 'READ_FILE', 'WRITE_PROJECT_FILE']
    assert 'SHUTDOWN' in calls[0].body
    assert calls[2].body == 'second'


def test_parser_handles_escaped_quotes_comparisons_and_single_quoted_types():
    text = r'''< Action type='RUN_COMMAND' cmd="Write-Output \"a > b\"" />'''
    calls = action_protocol.extract(text)
    assert len(calls) == 1
    assert action_attrs.parse(calls[0].attributes)['cmd'] == 'Write-Output "a > b"'
    assert actions._tag_body("<action type='RUN_PYTHON'>print('ok')</action>",
                             'RUN_PYTHON') == "print('ok')"


def test_unverified_or_malformed_receipts_never_count_as_success():
    for receipt in ('', '[Unverified] clicked', '[Partial] some fields failed',
                    '[Calendar Policy] invalid event purpose',
                    '[Unavailable] handler absent', '[ERROR] failed',
                    '[Browser Receipt] {bad', '[Browser Receipt] []',
                    '[Browser Receipt] {"ok":"false"}',
                    '[Browser Receipt] {"ok":true,"verified":false}'):
        assert not actions._classify_output(receipt), receipt


def test_completion_ledger_cannot_promote_uncertain_or_empty_receipts():
    import execution_reliability
    import outcomes
    for text in ('', '[Unverified] saved', '[Partial] filled some fields', '[Not Run] cancelled'):
        assert execution_reliability._receipt_state(dict(ok=True, output=text)) != outcomes.SUCCEEDED


def test_transport_failure_does_not_trigger_a_plan_repair_call():
    import planner
    call = mock.Mock()
    for raw in ('', '[API Timeout]'):
        plan = planner._repair_or_fallback(raw, 'work request', call)
        assert plan.goal == 'work request' and plan.trivial
    call.assert_not_called()


def test_generated_catalog_keeps_the_code_body_and_its_line_breaks():
    syntax = '<ACTION type="RUNTIME_APPLY" project="demo">\nimport bpy\nprint("ok")\n</ACTION>'
    entry = capability_migration.catalogue_entries({'CODE': syntax})['RUNTIME_APPLY']
    assert entry['syntax'] == syntax
    with mock.patch.object(action_registry, 'search', return_value=[dict(
            name='RUNTIME_APPLY', catalog='CODE', text=entry['syntax'], notes='')]):
        rendered = action_registry.discover_text('.', 'build scene')
    assert '\nimport bpy\nprint("ok")\n' in rendered
    assert 'tag body OR code' in rendered


def test_static_inventory_accounts_for_legacy_deferred_actions():
    import integrity
    source = 'DEFERRED_ACTIONS = frozenset({"SAVE_FILE", "WRITE_FILE"})'
    assert {'SAVE_FILE', 'WRITE_FILE'} <= integrity.executable_names(source)


def dispatch(text, handler, interrupted=None):
    actions.reset_task_ledger('synthetic action sequence')
    with mock.patch.object(actions, '_action_allowed', return_value=(True, None)), \
         mock.patch.object(actions, '_policy_confirmation_required', return_value=False), \
         mock.patch.object(actions, '_observer_mode_block', return_value=None), \
         mock.patch.object(actions, 'announce_action'), \
         mock.patch.object(actions.action_bus.BUS, 'dispatch', side_effect=handler), \
         mock.patch.object(actions, 'SETTLE_DELAY_SECONDS', 0), \
         mock.patch('authentication_gate.awaiting', return_value=False):
        actions.execute_actions(text, {}, queue.Queue(), '.', interrupt_event=interrupted)
    return actions.execute_actions._results


def test_same_valid_receipt_from_two_actions_is_not_a_missing_receipt():
    receipts = dispatch('<ACTION type="LIST_WINDOWS" /><ACTION type="LIST_PROCESSES" />',
                        lambda ctx: ctx.report('No matching entries.'))
    assert len(receipts) == 2 and all(row['ok'] for row in receipts)


def test_each_code_action_receives_its_own_body():
    bodies = []
    def handler(ctx):
        bodies.append(actions._tag_body(ctx.response_text, 'RUNTIME_APPLY'))
        ctx.report('[Success] applied revision=1')
    text = ('<ACTION type="RUNTIME_APPLY" project="a">first</ACTION>'
            '<ACTION type="RUNTIME_APPLY" project="b">second</ACTION>')
    assert all(row['ok'] for row in dispatch(text, handler))
    assert bodies == ['first', 'second']


def test_nested_alarm_example_never_reaches_legacy_post_loop_handler():
    text = ('<ACTION type="CREATE_ACTION" name="EXAMPLE">'
            'example = \'<ACTION type="SET_ALARM" time="10:15" label="example" />\''
            '</ACTION>')
    with mock.patch('alarms.set_alarm') as set_alarm:
        results = dispatch(text, lambda ctx: ctx.report('[Success] installed id=example'))
    set_alarm.assert_not_called()
    assert [row['type'] for row in results] == ['CREATE_ACTION']


def test_alarm_text_inside_a_saved_document_is_not_an_alarm_request(tmp_path):
    import approval_isolation
    path = tmp_path / 'examples.txt'
    payload = '<ACTION type="SET_ALARM" time="10:15" label="example" />'
    with mock.patch('alarms.set_alarm') as set_alarm, \
         approval_isolation.no_approval_writes():
        results = dispatch(f'<ACTION type="SAVE_FILE" path="{path}">{payload}</ACTION>',
                           lambda ctx: None)
    set_alarm.assert_not_called()
    assert path.read_text(encoding='utf-8') == payload
    assert results[0]['ok']


def test_invalid_alarm_never_bypasses_the_schema_via_post_loop():
    with mock.patch('alarms.set_alarm') as set_alarm:
        results = dispatch('<ACTION type="SET_ALARM" label="missing time" />', lambda ctx: None)
    set_alarm.assert_not_called()
    assert results and not results[0]['ok']


def test_alarm_delete_has_an_execution_receipt():
    with mock.patch('alarms.delete_alarm', return_value='[Success] Deleted alarm id=synthetic'):
        results = dispatch('<ACTION type="DELETE_ALARM" id="synthetic" />', lambda ctx: None)
    assert results[0]['type'] == 'DELETE_ALARM' and results[0]['ok']


def test_uncertain_prerequisite_stops_later_mutations():
    called = []
    def handler(ctx):
        called.append(ctx.action_type)
        ctx.report('[Unverified] destination not confirmed')
    results = dispatch('<ACTION type="LIST_WINDOWS" /><ACTION type="CLIPBOARD_WRITE" text="payload" />', handler)
    assert called == ['LIST_WINDOWS']
    assert results[1]['not_run'] and not results[1]['ok']


def test_abort_between_actions_stops_remaining_calls():
    interrupted = threading.Event()
    def handler(ctx):
        ctx.report('[Success] observed id=1')
        interrupted.set()
    results = dispatch('<ACTION type="LIST_WINDOWS" /><ACTION type="LIST_PROCESSES" />', handler, interrupted)
    assert results[1]['not_run']


def test_save_then_read_runs_in_original_order(tmp_path):
    path = tmp_path / 'ordered.txt'
    observed = []
    def handler(ctx):
        observed.append(path.read_text(encoding='utf-8'))
        ctx.report('[Success] read ordered.txt')
    # The actual file writer runs; its audit persistence is isolated.
    import approval_isolation
    with approval_isolation.no_approval_writes():
        results = dispatch(
            f'<ACTION type="SAVE_FILE" path="{path}">correct body</ACTION>'
            f'<ACTION type="READ_FILE" path="{path}" />', handler)
    assert observed == ['correct body']
    assert all(row['ok'] for row in results)
