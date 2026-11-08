# -*- coding: utf-8 -*-
"""Stable IDE integration for VANTA.

Antigravity is VS Code-derived and exposes a supported command-line surface.
This adapter never clicks or types into the IDE.  Paths are passed directly to
the executable, which keeps opening a file deterministic even when the window
layout or focused control changes.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path


DEFAULT_CONFIG = {"default_ide": "antigravity", "reuse_window": True}


def _config_path(base_dir):
    return Path(base_dir) / "vault" / "system" / "ide_config.json"


def load_config(base_dir):
    config = dict(DEFAULT_CONFIG)
    try:
        value = json.loads(_config_path(base_dir).read_text(encoding="utf-8"))
        if isinstance(value, dict):
            config.update(value)
    except (OSError, ValueError):
        pass
    return config


def antigravity_executable():
    candidates = [
        Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Antigravity IDE" / "Antigravity IDE.exe",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "antigravity" / "Antigravity IDE.exe",
    ]
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    raise FileNotFoundError("Antigravity IDE is not installed in a known location.")


def _existing(path, kind="path"):
    value = Path(path).expanduser().resolve()
    if not value.exists():
        raise FileNotFoundError(f"{kind.capitalize()} does not exist: {value}")
    return value


def _launch(args):
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    subprocess.Popen(args, close_fds=True, creationflags=flags)


def open_project(base_dir, path):
    project = _existing(path, "project")
    if not project.is_dir():
        raise NotADirectoryError(f"Project is not a folder: {project}")
    config = load_config(base_dir)
    args = [antigravity_executable(), str(project)]
    args.append("--reuse-window" if config.get("reuse_window", True) else "--new-window")
    _launch(args)
    return f"[Success] Opened project in Antigravity: {project}"


def open_file(base_dir, path, line=1, column=1):
    target = _existing(path, "file")
    if not target.is_file():
        raise IsADirectoryError(f"Expected a file, got a folder: {target}")
    try:
        line, column = max(1, int(line or 1)), max(1, int(column or 1))
    except (TypeError, ValueError):
        raise ValueError("line and column must be positive integers")
    config = load_config(base_dir)
    args = [antigravity_executable(), "--goto", f"{target}:{line}:{column}"]
    args.append("--reuse-window" if config.get("reuse_window", True) else "--new-window")
    _launch(args)
    return f"[Success] Opened {target.name} at line {line}, column {column} in Antigravity."


def show_diff(base_dir, left, right):
    before, after = _existing(left, "left file"), _existing(right, "right file")
    _launch([antigravity_executable(), "--diff", str(before), str(after), "--reuse-window"])
    return f"[Success] Opened an Antigravity diff for {before.name} and {after.name}."


def status(base_dir):
    try:
        executable = antigravity_executable()
        return {"ok": True, "ide": "antigravity", "executable": executable,
                "text": f"[Success] Antigravity is available at {executable}."}
    except FileNotFoundError as exc:
        return {"ok": False, "ide": "antigravity", "text": f"[Error] {exc}"}
