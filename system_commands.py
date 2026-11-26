"""Shared shell preparation and bounded, recoverable command output."""
import json
import os
from pathlib import Path
import shutil
import uuid


def prepare(command, base_dir, cwd='', shell='powershell'):
    """Return argv and a verified working directory without running anything."""
    if not str(command or '').strip():
        raise ValueError('A nonempty command is required')
    host = str(shell or 'powershell').strip().lower()
    if host not in {'powershell', 'pwsh', 'cmd'}:
        raise ValueError('shell must be powershell, pwsh or cmd')
    root = Path(base_dir or os.getcwd()).resolve()
    directory = Path(os.path.expandvars(os.path.expanduser(str(cwd or root))))
    if not directory.is_absolute():
        directory = root / directory
    directory = directory.resolve()
    if not directory.is_dir():
        raise ValueError(f'Working directory does not exist: {directory}')
    if host == 'pwsh' and not shutil.which('pwsh'):
        raise ValueError('PowerShell 7 (pwsh) is not installed; use powershell or install the requested host')
    if host == 'cmd':
        argv = ['cmd.exe', '/d', '/s', '/c', 'chcp 65001 >nul & ' + command]
    else:
        prefix = ("$ErrorActionPreference = 'Stop'; $global:LASTEXITCODE = 0; "
                  "[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false); ")
        script = prefix + command + '\nif ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }'
        argv = [host, '-NoProfile', '-NonInteractive', '-Command', script]
    return argv, str(directory)


def preserve_output(base_dir, stdout, stderr, exit_code, shell, cwd):
    """Keep long output outside semantic memory instead of silently losing it.

    This is an execution log, not a learned fact. No command text is duplicated
    here. Individual streams are capped to bound persisted log size.
    """
    root = Path(base_dir).resolve() / 'vault' / 'logs' / 'commands'
    root.mkdir(parents=True, exist_ok=True)
    path = root / (uuid.uuid4().hex + '.json')
    cap = 2_000_000
    data = {'exit_code': exit_code, 'shell': shell, 'cwd': cwd,
            'stdout': stdout[:cap], 'stderr': stderr[:cap],
            'stdout_chars': len(stdout), 'stderr_chars': len(stderr),
            'truncated': len(stdout) > cap or len(stderr) > cap}
    with path.open('x', encoding='utf-8') as stream:
        json.dump(data, stream, ensure_ascii=False)
    return str(path)


def preview(text, path='', limit=1500):
    if len(text) <= limit:
        return text
    # Preserve the error prefix and the tail where build diagnostics often live.
    return (text[:650] + '\n...(middle omitted)...\n' + text[-500:]
            + (f'\nExecution output: {path} (READ_FILE to inspect; log may be size-capped).' if path else
               '\n[Output truncated; full output could not be saved.]'))
