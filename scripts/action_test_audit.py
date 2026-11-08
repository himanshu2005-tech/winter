"""Opt-in pytest metadata audit; stores action names and test IDs, never payloads.

python -m pytest tests -p scripts.action_test_audit --action-audit-output=reports/action-tests.json --isolated-action-audit
"""
import ipaddress
import json
import socket
from pathlib import Path
from unittest import mock

import pytest

_reached = {}
_outcomes = {}
_network = None


def pytest_addoption(parser):
    parser.addoption('--action-audit-output', default=None)
    parser.addoption('--isolated-action-audit', action='store_true')


def pytest_sessionstart(session):
    global _network
    _reached.clear()
    _outcomes.clear()
    if session.config.getoption('--isolated-action-audit'):
        original = socket.socket.connect

        def isolated_connect(sock, address):
            if isinstance(address, tuple):
                host, port = address[:2]
                try:
                    local = ipaddress.ip_address(host).is_loopback
                except ValueError:
                    local = str(host).lower() == 'localhost'
                if not local or int(port) in {7687, 7474, 7473}:
                    raise OSError('Isolated tests cannot connect to external services or the production graph')
            return original(sock, address)

        _network = mock.patch.object(socket.socket, 'connect', isolated_connect)
        _network.start()


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_call(item):
    if not item.config.getoption('--action-audit-output'):
        yield
        return
    import action_bus
    original = action_bus.ActionBus.dispatch

    def observe(bus, ctx):
        _reached.setdefault(ctx.action_type, set()).add(item.nodeid)
        return original(bus, ctx)

    with mock.patch.object(action_bus.ActionBus, 'dispatch', observe):
        yield


def pytest_runtest_logreport(report):
    if report.when == 'call' or report.failed or report.skipped:
        previous = _outcomes.get(report.nodeid)
        if previous != 'failed':
            _outcomes[report.nodeid] = report.outcome


def pytest_sessionfinish(session, exitstatus):
    global _network
    if _network is not None:
        _network.stop()
        _network = None
    output = session.config.getoption('--action-audit-output')
    if not output:
        return
    from scripts.audit_actions import audit
    inventory = audit()
    schema_tests = [value for key, value in _outcomes.items()
                    if 'test_action_contract_matrix.py::SchemaMatrixTests::' in key]
    schema_checked = len(schema_tests) == 4 and all(value == 'passed' for value in schema_tests)
    for row in inventory['actions']:
        cases = sorted(_reached.get(row['name'], ()))
        row['schema_matrix_passed'] = schema_checked
        row['dispatcher_test_cases'] = [dict(test=case, result=_outcomes.get(case, 'unknown'))
                                        for case in cases]
        # Dispatch may exercise a precondition or a mocked provider. It is not
        # evidence of an authenticated live operation or all handler branches.
    report = dict(
        pytest_exit_status=int(exitstatus), inventory=inventory,
        dispatcher_actions_observed=sum(bool(row['dispatcher_test_cases']) for row in inventory['actions']),
        test_results={state: sum(value == state for value in _outcomes.values())
                      for state in ('passed', 'failed', 'skipped')},
        limitations=[
            'Schema checks exercise contracts, not provider side effects.',
            'A dispatcher observation may stop at a precondition or use a mocked provider.',
            'No authenticated live action is marked verified by this report.',
            'Isolation guards Python socket connections; browser subprocesses use temporary profiles in browser fixtures.',
        ])
    path = Path(output)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2), encoding='utf-8')
