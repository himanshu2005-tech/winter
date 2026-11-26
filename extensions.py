# -*- coding: utf-8 -*-
"""
extensions.py -- actions VANTA writes for itself.

When Himanshu asks for something there is no action for, the honest options
used to be "do it with RUN_PYTHON every time" or "wait for the source to be
edited". This lets VANTA write the action once, keep it, and use it by name
from then on.

WHAT "SAFE" MEANS HERE

Not sandboxing. VANTA already runs arbitrary Python through RUN_PYTHON and
RUN_SANDBOX, so an extension grants no capability it did not have; pretending
otherwise would be security theatre. The real hazard is *durability*: a broken
extension is loaded on every future turn, so one bad file could break the
assistant permanently rather than failing once. Everything below exists to stop
that.

  1. Four layers must agree. A built-in action only works if the dispatcher,
     the registry, the catalogue and the schema all know about it -- every
     mismatch this project has ever had was silent. An extension derives all
     four from one SPEC, so they cannot drift apart.

  2. Nothing runs at import. The module body may only import, assign, and
     define. This is the exact bug that had seed_memory.py rewriting the graph
     just because something imported it.

  3. It must survive being run before it is installed. A new extension is
     written to a staging file and executed in a *subprocess* against its own
     declared example. If that subprocess hangs, crashes, or returns something
     that is not a receipt string, the file never reaches the extensions
     directory.

  4. It cannot shadow a built-in. Names are checked against the real registry,
     so an extension can never quietly replace CLICK or SEND_EMAIL.

  5. It is quarantined on repeated failure. Three consecutive errors disable it
     and say so, instead of failing on every turn forever.

  6. It runs on a leash. Each call is given a worker thread and a timeout, so a
     loop in an extension costs one action, not the session.
"""

import ast
import json
import os
import re
import subprocess
import sys
import threading
import time

EXTENSION_DIR = "extensions"
MANIFEST = "manifest.json"

NAME_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]{2,31}$")
DEFAULT_TIMEOUT = 30
MAX_FAILURES = 3
MAX_SOURCE_BYTES = 40000

# Module level may only describe; it may not act. Anything else is a side
# effect at import time, which is how a file that merely gets loaded ends up
# writing to the graph or opening a window.
_ALLOWED_TOPLEVEL = (
    ast.Import, ast.ImportFrom, ast.Assign, ast.AnnAssign, ast.FunctionDef,
    ast.AsyncFunctionDef, ast.ClassDef, ast.Expr,  # Expr only for docstrings
)

_cache = {"loaded_at": 0.0, "actions": {}}
_failures = {}


def _directory(base_dir):
    return os.path.join(base_dir, EXTENSION_DIR)


def _manifest_path(base_dir):
    return os.path.join(_directory(base_dir), MANIFEST)


# ------------------------------------------------------------------ #
# Validation
# ------------------------------------------------------------------ #
# Names this module has merged into the shared schema table. Without tracking
# them, an installed extension shows up in ACTION_SPECS and then reads back as
# a built-in -- so replacing one reported "that is a built-in and cannot be
# replaced" instead of asking for overwrite="true".
_registered = set()


def _builtin_actions():
    """Every action name the core already owns, so nothing can be shadowed."""
    names = set()
    try:
        import autonomy
        names |= set(autonomy.KNOWN_ACTIONS)
    except Exception:
        pass
    try:
        import action_schema
        names |= set(action_schema.ACTION_SPECS)
    except Exception:
        pass
    return names - _registered


def validate_source(name, source):
    """Return a list of problems with a proposed extension. Empty means usable."""
    problems = []
    name = str(name or "").strip().upper()

    if not NAME_PATTERN.match(name):
        problems.append(
            f"'{name}' is not a usable action name. Use 3 to 32 characters, "
            "A-Z, digits and underscores, starting with a letter.")
    elif name in _builtin_actions():
        problems.append(
            f"{name} is already a built-in action and cannot be replaced. "
            "Choose a different name.")

    if len(source.encode("utf-8")) > MAX_SOURCE_BYTES:
        problems.append(f"The source is over {MAX_SOURCE_BYTES} bytes. Keep an "
                        "extension small; call out to a module for the rest.")

    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        problems.append(f"It does not parse: line {exc.lineno}: {exc.msg}")
        return problems

    has_run, has_spec = False, False
    for node in tree.body:
        if not isinstance(node, _ALLOWED_TOPLEVEL):
            problems.append(
                f"Line {node.lineno}: only imports, assignments and definitions "
                "are allowed at the top level. Anything that runs on import "
                "runs every time VANTA starts.")
            continue
        if isinstance(node, ast.Expr) and not isinstance(node.value, ast.Constant):
            problems.append(
                f"Line {node.lineno}: a bare expression at the top level runs "
                "on import. Move it inside run().")
        if isinstance(node, ast.FunctionDef) and node.name == "run":
            has_run = True
            arguments = [a.arg for a in node.args.args]
            if arguments[:3] != ["attrs", "base_dir", "context"]:
                problems.append(
                    "run() must be defined as run(attrs, base_dir, context), "
                    f"not run({', '.join(arguments) or ''}).")
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = ([t.id for t in node.targets if isinstance(t, ast.Name)]
                       if isinstance(node, ast.Assign)
                       else [node.target.id if isinstance(node.target, ast.Name) else ""])
            if "SPEC" in targets:
                has_spec = True

    if not has_run:
        problems.append("There is no run(attrs, base_dir, context) function.")
    if not has_spec:
        problems.append(
            "There is no SPEC dictionary. It needs at least a description and "
            "an example, so the action can be put in the catalogue.")
    return problems


def _normalise_spec(name, raw):
    """Coerce a SPEC into the shape the catalogue and schema both need."""
    spec = raw if isinstance(raw, dict) else {}
    requires = [str(x) for x in (spec.get("requires") or []) if str(x).strip()]
    optional = [str(x) for x in (spec.get("optional") or []) if str(x).strip()]
    params = spec.get("params") if isinstance(spec.get("params"), dict) else {}
    return {
        "description": " ".join(str(spec.get("description") or "").split())[:200],
        "params": {str(k): " ".join(str(v).split())[:120] for k, v in params.items()},
        "requires": requires,
        "optional": optional,
        "body": bool(spec.get("body")),
        "example": " ".join(str(spec.get("example") or "").split())[:300]
                   or f'<ACTION type="{name}" />',
        "side_effects": bool(spec.get("side_effects")),
        "timeout": max(1, min(int(spec.get("timeout") or DEFAULT_TIMEOUT), 120)),
    }


# ------------------------------------------------------------------ #
# Loading
# ------------------------------------------------------------------ #
def _read_manifest(base_dir):
    try:
        with open(_manifest_path(base_dir), "r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def _write_manifest(base_dir, data):
    path = _manifest_path(base_dir)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temporary = path + ".tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2, ensure_ascii=False)
    os.replace(temporary, path)


def load_all(base_dir, force=False):
    """Import every enabled extension. Returns {NAME: record}.

    A file that fails to import is skipped and recorded, never raised: one bad
    extension must not stop the others or the turn.
    """
    directory = _directory(base_dir)
    if not os.path.isdir(directory):
        return {}

    stamp = max([os.path.getmtime(directory)] + [
        os.path.getmtime(os.path.join(directory, f))
        for f in os.listdir(directory) if f.endswith(".py")] or [0])
    if not force and _cache["actions"] and stamp <= _cache["loaded_at"]:
        return _cache["actions"]

    import importlib.util

    manifest = _read_manifest(base_dir)
    actions = {}
    for filename in sorted(os.listdir(directory)):
        if not filename.endswith(".py") or filename.startswith("_"):
            continue
        name = filename[:-3].upper()
        entry = manifest.get(name, {})
        if entry.get("enabled") is False:
            continue
        path = os.path.join(directory, filename)
        try:
            spec_loader = importlib.util.spec_from_file_location(
                f"vanta_ext_{name.lower()}", path)
            module = importlib.util.module_from_spec(spec_loader)
            spec_loader.loader.exec_module(module)
            if not callable(getattr(module, "run", None)):
                raise AttributeError("no run() function")
            actions[name] = {
                "name": name,
                "module": module,
                "spec": _normalise_spec(name, getattr(module, "SPEC", {})),
                "path": path,
                "created": entry.get("created", ""),
            }
        except Exception as exc:
            print(f"[Extensions] {name} failed to load and was skipped: {exc}")
            manifest.setdefault(name, {})["load_error"] = str(exc)[:200]

    _cache["actions"] = actions
    _cache["loaded_at"] = time.time()
    return actions


def names(base_dir):
    return set(load_all(base_dir))


def schema_specs(base_dir):
    """Schema entries so extensions are validated like every other action."""
    result = {}
    for name, record in load_all(base_dir).items():
        spec = record["spec"]
        entry = {}
        if spec["requires"]:
            entry["requires"] = spec["requires"]
        if spec["optional"] or spec["params"]:
            optional = set(spec["optional"]) | set(spec["params"])
            entry["optional"] = sorted(optional - set(spec["requires"]))
        if spec["body"]:
            entry["body"] = True
        result[name] = entry
    return result


def catalog_text(base_dir):
    """The catalogue block for the prompt, or "" when there are none."""
    loaded = load_all(base_dir)
    if not loaded:
        return ""
    lines = ["11. ACTIONS YOU WROTE YOURSELF:",
             "  These exist because you created them. They work exactly like "
             "built-in actions."]
    for name in sorted(loaded):
        spec = loaded[name]["spec"]
        lines.append(f"  {spec['example']}")
        if spec["description"]:
            lines.append(f"    - {spec['description']}")
        for param, description in sorted(spec["params"].items()):
            lines.append(f"      {param}: {description}")
    lines.append("  Use CREATE_ACTION to add another when Himanshu asks for "
                 "something repeatable that has no action yet, LIST_ACTIONS to "
                 "see them, and REMOVE_ACTION to delete one.")
    return "\n".join(lines)


def describe(base_dir):
    loaded = load_all(base_dir)
    manifest = _read_manifest(base_dir)
    if not loaded and not manifest:
        return "No self-written actions yet."
    lines = []
    for name in sorted(set(loaded) | set(manifest)):
        entry = manifest.get(name, {})
        if name not in loaded:
            reason = entry.get("load_error") or (
                "disabled after repeated failures" if entry.get("enabled") is False
                else "not loaded")
            lines.append(f"- {name}: UNAVAILABLE ({reason})")
            continue
        spec = loaded[name]["spec"]
        failures = _failures.get(name, 0)
        note = f", {failures} recent failure(s)" if failures else ""
        lines.append(f"- {name}: {spec['description'] or '(no description)'}"
                     f"  [created {entry.get('created', 'unknown')}{note}]")
    return "\n".join(lines) or "No self-written actions yet."


# ------------------------------------------------------------------ #
# Creating
# ------------------------------------------------------------------ #
_SMOKE_TEST = '''
import importlib.util, json, sys
path, name = sys.argv[1], sys.argv[2]
loader = importlib.util.spec_from_file_location("probe_" + name.lower(), path)
module = importlib.util.module_from_spec(loader)
loader.loader.exec_module(module)
spec = getattr(module, "SPEC", {})
attrs = spec.get("test_attrs") or {}
result = module.run(dict(attrs), sys.argv[3], {"origin": "test"})
if not isinstance(result, str):
    print("NOT_A_STRING:" + type(result).__name__)
    raise SystemExit(2)
print("OK:" + result[:200].replace(chr(10), " "))
'''


def create(base_dir, name, source, overwrite=False):
    """Validate, smoke-test in a subprocess, then install. Returns a receipt."""
    name = str(name or "").strip().upper()
    source = str(source or "")
    if not source.strip():
        return "[Error] CREATE_ACTION needs the Python source in the tag body."

    directory = _directory(base_dir)
    os.makedirs(directory, exist_ok=True)
    target = os.path.join(directory, f"{name.lower()}.py")
    if os.path.exists(target) and not overwrite:
        return (f"[Error] {name} already exists. Read it back with READ_ACTION "
                'first; if replacing it is genuinely right, reissue with '
                'overwrite="true".')

    problems = validate_source(name, source)
    if problems:
        return ("[Error] That action was not installed:\n  - " +
                "\n  - ".join(problems))

    staging = os.path.join(directory, f"_staging_{name.lower()}.py")
    try:
        with open(staging, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(source if source.endswith("\n") else source + "\n")

        # Run it once, in its own process, before it can ever be loaded in
        # ours. A hang or a crash here costs a subprocess, not the assistant.
        probe = subprocess.run(
            [sys.executable, "-c", _SMOKE_TEST, staging, name, base_dir],
            capture_output=True, text=True, timeout=45,
            encoding="utf-8", errors="replace",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        output = (probe.stdout or "").strip()
        if probe.returncode != 0 or not output.startswith("OK:"):
            detail = output or (probe.stderr or "").strip().splitlines()[-1:] or ["no output"]
            if isinstance(detail, list):
                detail = detail[0]
            if "NOT_A_STRING" in output:
                detail = ("run() returned " + output.split(":", 1)[1] +
                          ", but it must return a string starting with "
                          "[Success] or [Error].")
            return (f"[Error] {name} ran but did not pass its own test, so it "
                    f"was not installed: {str(detail)[:300]}")

        os.replace(staging, target)
    except subprocess.TimeoutExpired:
        return (f"[Error] {name} did not finish within 45 seconds when tested, "
                "so it was not installed. Give it a smaller job.")
    except Exception as exc:
        return f"[Error] Could not install {name}: {exc}"
    finally:
        if os.path.exists(staging):
            os.remove(staging)

    manifest = _read_manifest(base_dir)
    manifest[name] = {"enabled": True, "created": time.strftime("%Y-%m-%d %H:%M"),
                      "source": "vanta"}
    _write_manifest(base_dir, manifest)
    _failures.pop(name, None)
    load_all(base_dir, force=True)
    _register(base_dir)
    return (f"[Success] {name} is installed and usable now. It passed its own "
            f"test: {output[3:170]}")


def remove(base_dir, name):
    name = str(name or "").strip().upper()
    target = os.path.join(_directory(base_dir), f"{name.lower()}.py")
    if not os.path.exists(target):
        return f"[Error] There is no self-written action called {name}."
    os.remove(target)
    manifest = _read_manifest(base_dir)
    manifest.pop(name, None)
    _write_manifest(base_dir, manifest)
    _failures.pop(name, None)
    load_all(base_dir, force=True)
    _register(base_dir)
    return f"[Success] Removed {name}."


def read(base_dir, name):
    name = str(name or "").strip().upper()
    target = os.path.join(_directory(base_dir), f"{name.lower()}.py")
    try:
        with open(target, "r", encoding="utf-8") as handle:
            return f"[Success] {name} source:\n\n{handle.read()[:8000]}"
    except OSError:
        return f"[Error] There is no self-written action called {name}."


# ------------------------------------------------------------------ #
# Calling
# ------------------------------------------------------------------ #
def call(base_dir, name, attrs, context=None):
    """Run one extension on a leash. Always returns a receipt string."""
    name = str(name or "").strip().upper()
    record = load_all(base_dir).get(name)
    if record is None:
        return f"[Error] {name} is not an available action."

    timeout = record["spec"]["timeout"]
    outcome = {}

    def worker():
        try:
            outcome["value"] = record["module"].run(
                dict(attrs or {}), base_dir, dict(context or {}))
        except Exception as exc:
            outcome["error"] = f"{type(exc).__name__}: {exc}"

    thread = threading.Thread(target=worker, daemon=True,
                              name=f"vanta-ext-{name.lower()}")
    thread.start()
    thread.join(timeout)

    if thread.is_alive():
        # The thread is left running rather than killed -- Python cannot safely
        # kill one -- but it is a daemon, so it dies with the process and the
        # turn is not held hostage waiting for it.
        _record_failure(base_dir, name, f"did not return within {timeout}s")
        return (f"[Error] {name} did not return within {timeout} seconds. "
                "It has been counted as a failure.")

    if "error" in outcome:
        _record_failure(base_dir, name, outcome["error"])
        return f"[Error] {name} raised {outcome['error']}."

    result = outcome.get("value")
    if not isinstance(result, str) or not result.strip():
        _record_failure(base_dir, name, "returned nothing usable")
        return (f"[Error] {name} returned {type(result).__name__} instead of a "
                "result string.")

    _failures.pop(name, None)
    if not result.lstrip().startswith(("[Success]", "[Error]", "[Approval Required]")):
        # Keep the receipt convention the rest of the pipeline reads, rather
        # than letting an extension's prose be classified as a failure.
        result = "[Success] " + result.strip()
    return result


def _record_failure(base_dir, name, reason):
    count = _failures.get(name, 0) + 1
    _failures[name] = count
    print(f"[Extensions] {name} failed ({count}/{MAX_FAILURES}): {reason}")
    if count < MAX_FAILURES:
        return
    manifest = _read_manifest(base_dir)
    manifest.setdefault(name, {})["enabled"] = False
    manifest[name]["disabled_reason"] = f"{reason} (after {count} failures)"
    _write_manifest(base_dir, manifest)
    load_all(base_dir, force=True)
    print(f"[Extensions] {name} disabled after {count} consecutive failures.")


def _register(base_dir):
    """Merge extension schemas into the validator so they are checked too."""
    try:
        import action_schema
        specs = schema_specs(base_dir)
        # Drop schemas for extensions that no longer exist, or a removed action
        # keeps being validated against a spec nothing can satisfy.
        for stale in _registered - set(specs):
            action_schema.ACTION_SPECS.pop(stale, None)
        _registered.clear()
        _registered.update(specs)
        action_schema.ACTION_SPECS.update(specs)
    except Exception as exc:
        print(f"[Extensions] Could not register schemas: {exc}")


def register(base_dir):
    """Called once at startup, after the core modules are importable."""
    load_all(base_dir, force=True)
    _register(base_dir)
    return names(base_dir)
