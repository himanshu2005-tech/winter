"""Real local Python children: receipts, cancellation and temporary-file cleanup."""
import os
import queue
import threading
import time
from unittest import mock

import pytest

import action_bus
import actions


def run_python(code, interrupted=None):
    ctx = action_bus.ActionContext(
        'RUN_PYTHON', {}, queue.Queue(), '.', interrupt_event=interrupted,
        response_text=f"<ACTION type='RUN_PYTHON'>{code}</ACTION>",
        legacy=actions.execute_actions)
    popen = actions.subprocess.Popen
    children = []

    def start(argv, **kwargs):
        child = popen(argv, **kwargs)
        children.append((argv[1], child))
        return child

    # These scripts only compute, print or sleep. No approval persistence.
    with mock.patch.object(actions, 'python_needs_approval', return_value=''), \
         mock.patch.object(actions.subprocess, 'Popen', side_effect=start):
        actions._run_run_python(ctx)
    assert children
    for path, child in children:
        # taskkill is launched by subprocess.run on Windows during cancellation.
        if path == '/PID':
            continue
        assert not os.path.exists(path)
        assert child.poll() is not None
    return actions.execute_actions._last_output


@pytest.mark.parametrize('code,expected,ok', [
    ("print('synthetic-ok')", 'synthetic-ok', True),
    ('value = 1', 'exit_code=0', True),
    ("raise ValueError('synthetic-failure')", 'exit code 1', False),
])
def test_python_exit_receipts_and_cleanup(code, expected, ok):
    receipt = run_python(code)
    assert expected in receipt
    assert actions._classify_output(receipt) is ok


def test_abort_stops_running_python_and_cleans_script():
    interrupted = threading.Event()
    timer = threading.Timer(0.35, interrupted.set)
    timer.start()
    started = time.monotonic()
    try:
        receipt = run_python("import time\ntime.sleep(20)\nprint('should-not-run')", interrupted)
    finally:
        timer.cancel()
    assert time.monotonic() - started < 6
    assert receipt.startswith('[Interrupted]')
    assert not actions._classify_output(receipt)
    assert 'should-not-run' not in receipt


def test_python_approval_preserves_complete_script_without_executing():
    code = "# review\n" * 100 + "print('complete-script')"
    ctx = action_bus.ActionContext(
        'RUN_PYTHON', {}, queue.Queue(), '.',
        response_text=f'<ACTION type="RUN_PYTHON">{code}</ACTION>')
    with mock.patch.object(actions, 'python_needs_approval', return_value='external work'), \
         mock.patch.object(actions, '_queue_for_approval', return_value='[Approval Required] queued') as pending, \
         mock.patch.object(actions, '_record'), \
         mock.patch.object(actions.subprocess, 'Popen') as start:
        actions._run_run_python(ctx)
    assert pending.call_args.args[1]['code'] == code
    start.assert_not_called()
