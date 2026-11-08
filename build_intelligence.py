# -*- coding: utf-8 -*-
"""Compact, evidence-based project profiling for Build mode.

This module does not decide what to build.  It tells the model what kind of
artifact is open, which source map is trustworthy, and which project-owned
commands can prove that artifact.  Keeping this outside the prompt avoids a
growing collection of React/Blender-specific utterance rules.
"""

from __future__ import annotations

import json
import os
from pathlib import Path


SKIP = {".git", ".venv", "venv", "node_modules", "dist", "build", ".next",
        "__pycache__", ".vanta_blender"}
SOURCE_SUFFIXES = {".py", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs",
                   ".java", ".kt", ".swift", ".c", ".cc", ".cpp", ".h",
                   ".hpp", ".rs", ".go", ".cs", ".dart"}


def _json(path: Path):
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def _files(root: Path, limit=3000):
    rows = []
    for directory, dirs, names in os.walk(root):
        dirs[:] = [name for name in dirs if name not in SKIP and not name.startswith(".")]
        for name in names:
            path = Path(directory) / name
            rows.append(path)
            if len(rows) >= limit:
                return rows
    return rows


def profile(root):
    """Return a deterministic build profile without executing project code."""
    root = Path(root).resolve()
    files = _files(root)
    names = {path.name.casefold() for path in files}
    suffixes = {}
    for path in files:
        suffixes[path.suffix.casefold()] = suffixes.get(path.suffix.casefold(), 0) + 1

    package = _json(root / "package.json")
    dependencies = {}
    dependencies.update(package.get("dependencies") or {})
    dependencies.update(package.get("devDependencies") or {})
    dep_names = {str(name).casefold() for name in dependencies}
    scripts = {str(k): str(v) for k, v in (package.get("scripts") or {}).items()}

    kinds = []
    if any(path.suffix.casefold() == ".blend" for path in files) or ".vanta_blender" in {
            path.name for path in root.iterdir()}:
        kinds.append("blender-3d")
    if "react-native" in dep_names or "expo" in dep_names:
        kinds.append("react-native")
    elif "react" in dep_names:
        kinds.append("react-web")
    if suffixes.get(".py") or "pyproject.toml" in names or "requirements.txt" in names:
        kinds.append("python")
    if not kinds and package:
        kinds.append("node")
    if not kinds:
        kinds.append("generic")

    commands = []
    # Use only commands declared by the project.  Guessing a package manager
    # or test runner is exactly how generated projects get damaged.
    preferred = ("test", "typecheck", "lint", "build", "start", "dev")
    manager = "npm"
    if (root / "pnpm-lock.yaml").exists():
        manager = "pnpm"
    elif (root / "yarn.lock").exists():
        manager = "yarn"
    for name in preferred:
        if name in scripts:
            commands.append({"purpose": name, "command": f"{manager} run {name}"})
    if (root / "pyproject.toml").exists() or any(
            path.name.startswith("test_") and path.suffix == ".py" for path in files):
        commands.append({"purpose": "test", "command": "python -m pytest -q"})

    source_files = [path for path in files if path.suffix.casefold() in SOURCE_SUFFIXES]
    return {
        "root": str(root), "artifact_kinds": kinds,
        "file_count": len(files), "source_file_count": len(source_files),
        "languages": dict(sorted(suffixes.items(), key=lambda item: (-item[1], item[0]))[:12]),
        "declared_commands": commands[:8],
        "package_scripts": scripts,
        "ast": "python:stdlib-ast" if suffixes.get(".py") else "none",
        "unparsed_source_types": sorted(
            suffix for suffix in suffixes
            if suffix in SOURCE_SUFFIXES and suffix != ".py"),
    }


def context_block(root, max_chars=2400):
    data = profile(root)
    lines = [
        "BUILD PROFILE (measured, not inferred):",
        f"  artifact_kinds={','.join(data['artifact_kinds'])}",
        f"  files={data['file_count']} source_files={data['source_file_count']}",
        f"  source_map={data['ast']}",
    ]
    if data["unparsed_source_types"]:
        lines.append("  syntax_without_AST=" + ",".join(data["unparsed_source_types"]))
    if data["declared_commands"]:
        lines.append("  project_declared_checks=" + "; ".join(
            f"{item['purpose']}:{item['command']}" for item in data["declared_commands"]))
    lines.append(
        "  Choose the workflow from this profile: Blender uses the persistent runtime; "
        "React/React Native use project files plus their declared scripts; large Python "
        "projects use the AST map. Never claim an AST exists for an unparsed language.")
    return "\n".join(lines)[:max_chars]


def describe(root):
    return "[Success] " + json.dumps(profile(root), ensure_ascii=False, indent=2)
