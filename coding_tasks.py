# -*- coding: utf-8 -*-
"""Background Codex/Claude task bridge for VANTA.

Prompts travel over stdin rather than command-line arguments, so private task
text is not exposed in process listings.  Each task owns a local receipt and
log.  The agents work in the requested project; Antigravity remains the visual
surface and is not controlled through fragile UI automation.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

_lock = threading.RLock()
_processes = {}


def _root(base_dir):
    path = Path(base_dir) / "vault" / "system" / "coding_tasks"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _record_path(base_dir, task_id):
    return _root(base_dir) / f"{task_id}.json"


def _write(base_dir, record):
    path = _record_path(base_dir, record["id"])
    temporary = path.with_name(path.name + f".{threading.get_ident()}.tmp")
    payload = json.dumps(record, indent=2, ensure_ascii=False)
    with _lock:
        temporary.write_text(payload, encoding="utf-8")
        # Virus scanners and an external status reader can briefly hold the
        # destination on Windows. A bounded retry preserves atomic state rather
        # than crashing the worker before the agent receives its prompt.
        for attempt in range(20):
            try:
                os.replace(temporary, path)
                break
            except PermissionError:
                if attempt == 19:
                    raise
                time.sleep(0.025)


def _read(base_dir, task_id):
    path = _record_path(base_dir, task_id)
    if not path.is_file():
        raise FileNotFoundError(f"Unknown coding task: {task_id}")
    with _lock:
        return json.loads(path.read_text(encoding="utf-8"))


def _find_codex():
    found = shutil.which("codex")
    if found:
        return found
    matches = sorted((Path.home() / ".antigravity-ide" / "extensions").glob(
        "openai.chatgpt-*/bin/windows-x86_64/codex.exe"), reverse=True)
    if matches:
        return str(matches[0])
    raise FileNotFoundError("Codex CLI was not found. Install or enable the OpenAI extension in Antigravity.")


def _find_claude():
    found = shutil.which("claude")
    if found:
        return found
    matches = sorted((Path.home() / ".antigravity-ide" / "extensions").glob(
        "anthropic.claude-code-*/resources/native-binary/claude.exe"), reverse=True)
    if matches:
        return str(matches[0])
    raise FileNotFoundError("Claude Code CLI was not found. Install or enable the Anthropic extension in Antigravity.")


def agent_status():
    rows = {}
    for name, finder in (("codex", _find_codex), ("claude", _find_claude)):
        try:
            rows[name] = {"available": True, "path": finder()}
        except FileNotFoundError as exc:
            rows[name] = {"available": False, "error": str(exc)}
    return rows


def _command(agent, project, mode):
    read_only = mode == "review"
    if agent == "codex":
        return [_find_codex(), "exec", "-C", str(project), "-s",
                "read-only" if read_only else "workspace-write", "--color", "never", "-"]
    if agent == "claude":
        return [_find_claude(), "-p", "--permission-mode",
                "plan" if read_only else "acceptEdits", "--permission-prompts", "none",
                "--output-format", "text"]
    raise ValueError("agent must be codex or claude")


def start(base_dir, project, prompt, agent="codex", mode="implement"):
    project_path = Path(project).expanduser().resolve()
    if not project_path.is_dir():
        raise NotADirectoryError(f"Coding project does not exist: {project_path}")
    if not str(prompt or "").strip():
        raise ValueError("A coding task needs a non-empty prompt.")
    mode = str(mode or "implement").lower()
    if mode not in {"implement", "review"}:
        raise ValueError("mode must be implement or review")
    agent = str(agent or "codex").lower()
    # Two agents editing one worktree concurrently produce interleaved diffs
    # that neither can reason about. Reviews are read-only and may coexist;
    # implementation jobs are serialized per resolved project root.
    if mode == "implement":
        for path in _root(base_dir).glob("code-*.json"):
            try:
                existing = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if (existing.get("mode") == "implement"
                    and existing.get("status") in {"queued", "running"}
                    and Path(existing.get("project", "")).resolve() == project_path):
                raise RuntimeError(
                    f"Coding task {existing.get('id')} is already editing this project. "
                    "Wait, cancel it, or run the second agent in review mode.")
    command = _command(agent, project_path, mode)
    task_id = "code-" + uuid.uuid4().hex[:12]
    log_path = _root(base_dir) / f"{task_id}.log"
    now = datetime.now(timezone.utc).isoformat()
    record = {"id": task_id, "agent": agent, "mode": mode,
              "project": str(project_path), "status": "queued",
              "created_at": now, "updated_at": now, "exit_code": None,
              "log_path": str(log_path), "summary": ""}
    _write(base_dir, record)

    def worker():
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        try:
            with log_path.open("w", encoding="utf-8", errors="replace") as log:
                process = subprocess.Popen(
                    command, cwd=str(project_path), stdin=subprocess.PIPE,
                    stdout=log, stderr=subprocess.STDOUT, text=True,
                    encoding="utf-8", errors="replace", creationflags=flags)
                with _lock:
                    _processes[task_id] = process
                record.update(status="running", pid=process.pid,
                              updated_at=datetime.now(timezone.utc).isoformat())
                _write(base_dir, record)
                process.communicate(str(prompt).strip() + "\n")
                record.update(status="completed" if process.returncode == 0 else "failed",
                              exit_code=process.returncode,
                              updated_at=datetime.now(timezone.utc).isoformat())
        except Exception as exc:
            record.update(status="failed", summary=str(exc),
                          updated_at=datetime.now(timezone.utc).isoformat())
        finally:
            with _lock:
                _processes.pop(task_id, None)
            try:
                if log_path.is_file():
                    text = log_path.read_text(encoding="utf-8", errors="replace")
                    record["summary"] = text[-4000:]
            except OSError:
                pass
            _write(base_dir, record)

    threading.Thread(target=worker, daemon=True, name=task_id).start()
    return (f"[Success] Started {agent} {mode} task {task_id} in {project_path}. "
            "Use CODING_TASK_STATUS to inspect it.")


def status(base_dir, task_id):
    record = _read(base_dir, task_id)
    summary = str(record.get("summary") or "").strip()
    text = (f"Coding task {record['id']}: {record['status']} using {record['agent']} "
            f"in {record['project']}.")
    if record.get("exit_code") is not None:
        text += f" Exit code: {record['exit_code']}."
    if summary:
        text += "\nLatest output:\n" + summary
    return "[Success] " + text


def cancel(base_dir, task_id):
    record = _read(base_dir, task_id)
    with _lock:
        process = _processes.get(task_id)
    if process is None or process.poll() is not None:
        return f"[Attention] Coding task {task_id} is not currently running ({record['status']})."
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
    record.update(status="cancelled", exit_code=process.returncode,
                  updated_at=datetime.now(timezone.utc).isoformat())
    _write(base_dir, record)
    return f"[Success] Cancelled coding task {task_id}."


def list_tasks(base_dir, limit=20):
    rows = []
    for path in sorted(_root(base_dir).glob("code-*.json"),
                       key=lambda item: item.stat().st_mtime, reverse=True)[:max(1, int(limit))]:
        try:
            row = json.loads(path.read_text(encoding="utf-8"))
            rows.append(f"- {row['id']}: {row['status']} ({row['agent']} {row['mode']}) {row['project']}")
        except (OSError, ValueError, KeyError):
            continue
    return "[Success] Coding tasks:\n" + "\n".join(rows) if rows else "No coding tasks recorded."
