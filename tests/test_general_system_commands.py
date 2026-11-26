"""Generic Windows execution, using isolated files and harmless commands."""
import json
import os
import queue
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
import pytest
import system_commands


def test_working_directory_is_explicit_and_resolved_from_project(tmp_path):
    target = tmp_path / 'project with spaces'
    target.mkdir()
    args, cwd = system_commands.prepare('Get-Location', tmp_path, 'project with spaces')
    assert Path(cwd) == target
    assert args[0] == 'powershell'
    assert "$ErrorActionPreference = 'Stop'" in args[-1]


@pytest.mark.parametrize('shell,cwd', [('bash', ''), ('powershell', 'does-not-exist')])
def test_bad_shell_or_directory_fails_before_launch(tmp_path, shell, cwd):
    with pytest.raises(ValueError):
        system_commands.prepare('echo test', tmp_path, cwd, shell)


def test_missing_pwsh_does_not_silently_change_shell(tmp_path):
    with patch('system_commands.shutil.which', return_value=None), pytest.raises(ValueError, match='not installed'):
        system_commands.prepare('Get-Location', tmp_path, shell='pwsh')


def test_full_output_can_be_reopened_but_is_excluded_from_graph_index(tmp_path):
    import memory
    stdout, stderr = 'head\n' + 'x'*1800, 'important final error'
    path = system_commands.preserve_output(tmp_path, stdout, stderr, 17, 'powershell', str(tmp_path))
    data = json.loads(Path(path).read_text(encoding='utf-8'))
    assert data['stdout'] == stdout and data['stderr'] == stderr
    assert data['exit_code'] == 17 and not data['truncated']
    text = system_commands.preview('[Error]\n' + stdout + '\n' + stderr, path)
    assert text.startswith('[Error]') and stderr in text and path in text
    assert 'logs' in memory.EXCLUDED_INDEX_DIRS


def test_generic_handler_passes_directory_and_shell_to_managed_process(tmp_path):
    import actions
    process = Mock(returncode=0)
    process.communicate.return_value = ('test output', '')
    ctx = SimpleNamespace(attrs={'cmd': 'echo test', 'cwd': str(tmp_path), 'shell': 'cmd'},
        action_type='RUN_COMMAND', ui_queue=queue.Queue(), base_dir=str(tmp_path), origin='user',
        turn_id='', interrupt_event=None, element_map={}, response_text='')
    with patch.object(actions.subprocess, 'Popen', return_value=process) as start:
        actions._run_run_command(ctx)
    assert start.call_args.args[0][0] == 'cmd.exe'
    assert start.call_args.kwargs['cwd'] == str(tmp_path)
    assert actions.execute_actions._last_command_result['exit_code'] == 0
    assert actions.execute_actions._last_command_result['shell'] == 'cmd'


@pytest.mark.skipif(os.name != 'nt', reason='Windows shell integration')
@pytest.mark.parametrize('shell,command,expected', [
    ('powershell', "[Console]::WriteLine('తెలుగు execution')", 'తెలుగు execution'),
    ('cmd', 'echo native-cmd-ok', 'native-cmd-ok'),
])
def test_harmless_real_shell_commands(shell, command, expected, tmp_path):
    args, cwd = system_commands.prepare(command, tmp_path, shell=shell)
    completed = subprocess.run(args, cwd=cwd, capture_output=True, text=True,
                               encoding='utf-8', errors='replace', timeout=10)
    assert completed.returncode == 0, completed.stderr
    assert expected in completed.stdout


@pytest.mark.skipif(os.name != 'nt', reason='Windows shell integration')
def test_filesystem_task_runs_in_requested_project_and_can_be_verified(tmp_path):
    args, cwd = system_commands.prepare(
        "Set-Content -LiteralPath 'result.txt' -Value 'verified task'; Get-Content -LiteralPath 'result.txt'",
        tmp_path)
    completed = subprocess.run(args, cwd=cwd, capture_output=True, text=True,
                               encoding='utf-8', errors='replace', timeout=10)
    assert completed.returncode == 0, completed.stderr
    assert (tmp_path / 'result.txt').read_text().strip() == 'verified task'


@pytest.mark.skipif(os.name != 'nt', reason='Windows shell integration')
def test_native_failure_is_preserved_after_successful_powershell_output(tmp_path):
    args, cwd = system_commands.prepare('cmd /c exit 7; Write-Output misleading-success', tmp_path)
    completed = subprocess.run(args, cwd=cwd, capture_output=True, text=True,
                               encoding='utf-8', errors='replace', timeout=10)
    assert completed.returncode == 7
