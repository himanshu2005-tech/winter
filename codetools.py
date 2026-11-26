# -*- coding: utf-8 -*-
"""
codetools.py — writing and running code on Himanshu's behalf.

Two capabilities that sound similar and must not be confused:

WRITE_CODE saves something he keeps. It goes to a real path, creates the
directories, refuses to silently clobber existing work, and reports where it
landed so he can open it.

RUN_SANDBOX answers a question. It executes a throwaway snippet in an isolated
temporary directory and returns what it printed. The isolation matters:
RUN_PYTHON runs inside VANTA's own project folder, where a snippet that writes
`memory.py` or `test.txt` lands on top of the running system. Scratch code has
no business being able to do that.

Neither is a security boundary. Both run as Himanshu with his permissions; the
sandbox limits blast radius and accidents, not intent.
"""

import os
import re
import shutil
import subprocess
import sys
import tempfile

LANGUAGES = {
    "python": {"ext": ".py", "cmd": None},          # resolved to the venv interpreter
    "javascript": {"ext": ".js", "cmd": ["node"]},
    "node": {"ext": ".js", "cmd": ["node"]},
    "powershell": {"ext": ".ps1",
                   "cmd": ["powershell", "-NoProfile", "-NonInteractive", "-File"]},
    "bash": {"ext": ".sh", "cmd": ["bash"]},
}

_EXT_LANGUAGE = {
    ".py": "python", ".js": "javascript", ".ps1": "powershell", ".sh": "bash",
}


def find_blender():
    """Locate blender.exe wherever it actually is.

    The path was pinned to "Blender 4.2", so any other version — and every
    Steam or Microsoft Store install — reported Blender as missing on a machine
    where it was installed and working.
    """
    explicit = os.getenv("BLENDER_EXE", "").strip().strip('"')
    if explicit:
        return (explicit, None) if os.path.exists(explicit) else (
            None, f"[Error] BLENDER_EXE points at {explicit}, which does not exist.")

    roots = [
        r"C:\Program Files\Blender Foundation",
        r"C:\Program Files (x86)\Blender Foundation",
        os.path.expandvars(r"%LOCALAPPDATA%\Programs\Blender Foundation"),
        r"C:\Program Files\Steam\steamapps\common\Blender",
        r"D:\Steam\steamapps\common\Blender",
    ]
    found = []
    for root in roots:
        if not os.path.isdir(root):
            continue
        candidate = os.path.join(root, "blender.exe")
        if os.path.exists(candidate):
            found.append(candidate)
        for entry in sorted(os.listdir(root)):
            candidate = os.path.join(root, entry, "blender.exe")
            if os.path.exists(candidate):
                found.append(candidate)
    if found:
        # Newest version last alphabetically ("Blender 4.2" < "Blender 4.5").
        return found[-1], None
    return None, ("[Error] Blender is not installed anywhere I looked. Install it, "
                  "or set BLENDER_EXE in .env to the full path of blender.exe.")


def _interpreter(base_dir):
    venv = os.path.join(base_dir, ".venv", "Scripts", "python.exe")
    return venv if os.path.exists(venv) else sys.executable


def _strip_fence(code):
    """Models wrap code in markdown fences even when asked not to."""
    text = str(code or "")
    fenced = re.match(r"\s*```[a-zA-Z0-9_+-]*\s*\n([\s\S]*?)\n?\s*```\s*$", text)
    return fenced.group(1) if fenced else text.strip("\n")


def _describe(result, where):
    out = (result.stdout or "").strip()
    err = (result.stderr or "").strip()
    parts = []
    if out:
        parts.append(f"stdout:\n{out[:6000]}" + ("\n[...truncated]" if len(out) > 6000 else ""))
    if err:
        parts.append(f"stderr:\n{err[:3000]}" + ("\n[...truncated]" if len(err) > 3000 else ""))
    if result.returncode == 0:
        if not parts:
            return f"[Success] Ran with no output (exit 0). {where}"
        return f"[Success] exit 0. " + "\n".join(parts)
    return f"[Error] exit {result.returncode}.\n" + ("\n".join(parts) or "(no output)")


def run_sandbox(base_dir, code="", language="python", timeout=60, path=""):
    """Run a snippet in a throwaway directory and report what it printed."""
    language = str(language or "python").lower()
    if path:
        # Must be absolute: the command runs with cwd set to the sandbox, so a
        # relative path would resolve inside the temp directory instead.
        target = os.path.abspath(path if os.path.isabs(path) else os.path.join(base_dir, path))
        if not os.path.exists(target):
            return (f"[Error] There is no file at {target}. " + "Use FIND_FILES to locate it rather than guessing another path, or ask Himanshu where it is.")
        language = _EXT_LANGUAGE.get(os.path.splitext(target)[1].lower(), language)
        code = None
    else:
        code = _strip_fence(code)
        if not code.strip():
            return "[Error] No code to run."

    spec = LANGUAGES.get(language)
    if not spec:
        return f"[Error] I cannot run '{language}'. Supported: {', '.join(sorted(LANGUAGES))}."

    workspace = tempfile.mkdtemp(prefix="vanta_sandbox_")
    try:
        if code is not None:
            target = os.path.join(workspace, f"snippet{spec['ext']}")
            with open(target, "w", encoding="utf-8") as handle:
                handle.write(code)

        command = ([_interpreter(base_dir)] if spec["cmd"] is None else list(spec["cmd"]))
        command.append(target)

        result = subprocess.run(
            command, capture_output=True, text=True,
            timeout=max(1, min(int(timeout), 300)),
            cwd=workspace,  # scratch code cannot touch the project it runs from
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        )
        return _describe(result, f"(sandbox: {workspace})")
    except subprocess.TimeoutExpired:
        return (
            f"[Error] Timed out after {timeout}s and was killed. If it is genuinely "
            "long-running, save it with WRITE_CODE and run it with RUN_COMMAND instead."
        )
    except FileNotFoundError:
        return f"[Error] The interpreter for '{language}' is not installed on this machine."
    except Exception as exc:
        return f"[Error] Sandbox failed: {exc}"
    finally:
        shutil.rmtree(workspace, ignore_errors=True)


def write_code(base_dir, path, content, language="", overwrite=False, run_after=False,
               timeout=60):
    """Save a script where Himanshu asked for it."""
    raw = str(path or "").strip().strip('"').strip("'")
    if not raw:
        return "[Error] WRITE_CODE needs a path."
    content = _strip_fence(content)
    if not content.strip():
        return "[Error] WRITE_CODE needs code in the tag body."

    if not os.path.splitext(raw)[1]:
        spec = LANGUAGES.get(str(language or "python").lower())
        raw += spec["ext"] if spec else ".txt"

    target = raw if os.path.isabs(raw) else os.path.join(base_dir, "generated", raw)
    target = os.path.abspath(os.path.normpath(target))

    if os.path.exists(target) and not overwrite:
        return (
            f"[Error] {target} already exists and was not touched. Read it first; if "
            "replacing it is genuinely correct, reissue with overwrite=\"true\"."
        )
    try:
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content if content.endswith("\n") else content + "\n")
    except Exception as exc:
        return f"[Error] Could not write {target}: {exc}"

    lines = content.count("\n") + 1
    message = f"[Success] Wrote {lines} lines to {target}"

    if run_after:
        outcome = run_sandbox(base_dir, path=target, timeout=timeout)
        return f"{message}\nThen ran it:\n{outcome}"
    return message
