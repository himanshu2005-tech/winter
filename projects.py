# -*- coding: utf-8 -*-
"""
projects.py -- the codebases VANTA works on, and safe access to their files.

Everything under vault/projects is a project. Opening one puts VANTA in it:
the Code page shows its tree, the coder model is told what is in it, and the
file actions are scoped to it, so "implement this" has an unambiguous *this*.

The whole surface here is file access driven by a language model, so path
containment is the thing that matters. Every path an action supplies is
resolved to an absolute path and checked to still be inside the project root
afterwards -- resolving first and comparing after is what catches `../../..`,
a symlink pointing out of the tree, and on Windows the short-name and
alternate-separator spellings of the same escape. A check on the string before
resolution catches none of those.

The rest is about not drowning the model. A tree listing skips the directories
that are always noise (.git, node_modules, __pycache__, virtualenvs), file
reads are capped, and binary files are reported as binary rather than dumped as
mojibake into a prompt.
"""

import fnmatch
import json
import os
import re
import shutil
import time

PROJECTS_DIRNAME = os.path.join("vault", "projects")
ACTIVE_FILE = os.path.join("vault", "system", "active_project.json")

# Directories that are never worth showing: generated, vendored, or huge.
SKIP_DIRS = {
    ".git", ".hg", ".svn", "__pycache__", ".pytest_cache", ".mypy_cache",
    ".ruff_cache", "node_modules", ".venv", "venv", "env", ".idea", ".vscode",
    "dist", "build", ".next", ".nuxt", "target", ".gradle", ".tox", ".cache",
    "site-packages", ".DS_Store", "coverage", ".parcel-cache",
}
SKIP_SUFFIXES = (
    ".pyc", ".pyo", ".pyd", ".so", ".dll", ".dylib", ".exe", ".bin", ".o",
    ".class", ".jar", ".zip", ".tar", ".gz", ".7z", ".rar", ".pdf", ".png",
    ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".mp3", ".mp4", ".wav", ".mov",
    ".sqlite", ".db", ".lock", ".pack", ".woff", ".woff2", ".ttf",
)

MAX_TREE_ENTRIES = 400
MAX_READ_BYTES = 200_000
MAX_WRITE_BYTES = 2_000_000
NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.\-]{0,48}$")

LANGUAGE_BY_SUFFIX = {
    ".py": "Python", ".js": "JavaScript", ".ts": "TypeScript",
    ".tsx": "TypeScript", ".jsx": "JavaScript", ".java": "Java",
    ".c": "C", ".h": "C", ".cpp": "C++", ".hpp": "C++", ".cs": "C#",
    ".go": "Go", ".rs": "Rust", ".rb": "Ruby", ".php": "PHP",
    ".swift": "Swift", ".kt": "Kotlin", ".m": "Objective-C", ".r": "R",
    ".sql": "SQL", ".sh": "Shell", ".ps1": "PowerShell", ".html": "HTML",
    ".css": "CSS", ".scss": "CSS", ".md": "Markdown", ".json": "JSON",
    ".yml": "YAML", ".yaml": "YAML", ".toml": "TOML", ".ipynb": "Notebook",
}


class ProjectError(Exception):
    """Something the model asked for that cannot be done, with a reason."""


# ------------------------------------------------------------------ #
# Locations
# ------------------------------------------------------------------ #
def projects_root(base_dir):
    root = os.path.join(base_dir, PROJECTS_DIRNAME)
    os.makedirs(root, exist_ok=True)
    return root


def project_path(base_dir, name):
    """The absolute path of one project, refusing anything that escapes."""
    cleaned = " ".join(str(name or "").split())
    if not cleaned:
        raise ProjectError("Which project?")
    if not NAME_PATTERN.match(cleaned):
        raise ProjectError(
            f"'{cleaned}' is not a usable project name. Use letters, digits, "
            "spaces, dots, dashes and underscores.")
    root = projects_root(base_dir)
    candidate = os.path.realpath(os.path.join(root, cleaned))
    if not _inside(candidate, os.path.realpath(root)):
        raise ProjectError("A project name cannot point outside vault/projects.")
    return candidate


def _inside(path, root):
    """True when `path` is `root` or lives under it, after resolution.

    os.path.commonpath on resolved paths is the check that survives `..`,
    symlinks and Windows' alternate spellings; comparing raw strings does not.
    """
    try:
        return os.path.commonpath([os.path.normcase(path),
                                   os.path.normcase(root)]) == os.path.normcase(root)
    except ValueError:      # different drives on Windows
        return False


def resolve(base_dir, project, relative):
    """Resolve a path inside a project, or raise. The containment boundary."""
    root = project_path(base_dir, project)
    if not os.path.isdir(root):
        raise ProjectError(f"There is no project called '{project}'.")
    wanted = str(relative or "").strip().strip('"').replace("\\", "/").lstrip("/")
    if not wanted:
        raise ProjectError("Which file?")
    target = os.path.realpath(os.path.join(root, wanted))
    if not _inside(target, root):
        raise ProjectError(
            f"'{relative}' resolves outside the project. File actions are "
            "scoped to the project you have open.")
    return root, target


# ------------------------------------------------------------------ #
# Projects
# ------------------------------------------------------------------ #
def _meta_path(path):
    return os.path.join(path, ".vanta-project.json")


def _read_meta(path):
    try:
        with open(_meta_path(path), "r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def create(base_dir, name, description="", language=""):
    path = project_path(base_dir, name)
    if os.path.isdir(path):
        return f"[Error] A project called '{name}' already exists."
    os.makedirs(path, exist_ok=True)
    meta = {"name": os.path.basename(path), "description": str(description or ""),
            "language": str(language or ""), "created": time.strftime("%Y-%m-%d %H:%M")}
    with open(_meta_path(path), "w", encoding="utf-8") as handle:
        json.dump(meta, handle, indent=2, ensure_ascii=False)
    set_active(base_dir, meta["name"])
    return (f"[Success] Created the project '{meta['name']}' at {path} and "
            "opened it. It is empty; add files with WRITE_PROJECT_FILE.")


def listing(base_dir):
    """Every project, with enough detail to choose between them."""
    root = projects_root(base_dir)
    result = []
    for entry in sorted(os.listdir(root)):
        path = os.path.join(root, entry)
        if not os.path.isdir(path):
            continue
        meta = _read_meta(path)
        files, languages, newest = 0, {}, 0.0
        for _relative, absolute, is_dir in _walk(path, limit=MAX_TREE_ENTRIES):
            if is_dir:
                continue
            files += 1
            suffix = os.path.splitext(absolute)[1].lower()
            language = LANGUAGE_BY_SUFFIX.get(suffix)
            if language:
                languages[language] = languages.get(language, 0) + 1
            try:
                newest = max(newest, os.path.getmtime(absolute))
            except OSError:
                pass
        result.append({
            "name": entry, "path": path,
            "description": meta.get("description", ""),
            "files": files,
            "languages": sorted(languages, key=languages.get, reverse=True)[:3],
            "modified": time.strftime("%Y-%m-%d %H:%M", time.localtime(newest))
                        if newest else meta.get("created", ""),
        })
    return result


def match_name(base_dir, requested):
    """Resolve a spoken project name against ``vault/projects`` only.

    Exact case-insensitive and punctuation-normalised matches are safe. A
    partial match is accepted only when unique; ambiguity is returned to the
    caller instead of opening a plausible but wrong workspace.
    """
    wanted = " ".join(str(requested or "").split()).strip()
    if not wanted:
        raise ProjectError("Which project?")
    names = [row["name"] for row in listing(base_dir)]
    folded = wanted.casefold()
    exact = [name for name in names if name.casefold() == folded]
    if exact:
        return exact[0]

    def normal(value):
        return re.sub(r"[^a-z0-9]+", "", str(value).casefold())

    compact = normal(wanted)
    normal_matches = [name for name in names if normal(name) == compact]
    if len(normal_matches) == 1:
        return normal_matches[0]
    partial = [name for name in names
               if folded in name.casefold() or compact in normal(name)]
    if len(partial) == 1:
        return partial[0]
    if len(partial) > 1:
        raise ProjectError(
            f"'{wanted}' matches more than one vault project: "
            + ", ".join(partial[:8]) + ".")
    raise ProjectError(
        f"There is no project called '{wanted}' in vault/projects.")


def describe(base_dir):
    projects = listing(base_dir)
    if not projects:
        return ("No projects yet. Create one with CREATE_PROJECT and it appears "
                "on the Code page of the Control Center.")
    active = get_active(base_dir)
    lines = []
    for project in projects:
        mark = "  <- open" if project["name"] == active else ""
        languages = ", ".join(project["languages"]) or "empty"
        lines.append(f"- {project['name']}: {project['files']} files "
                     f"({languages}), last touched {project['modified']}{mark}")
        if project["description"]:
            lines.append(f"    {project['description']}")
    return "\n".join(lines)


# ------------------------------------------------------------------ #
# Which project is open
# ------------------------------------------------------------------ #
def get_active(base_dir):
    try:
        with open(os.path.join(base_dir, ACTIVE_FILE), "r", encoding="utf-8") as handle:
            name = json.load(handle).get("project", "")
    except (FileNotFoundError, json.JSONDecodeError, OSError, AttributeError):
        return ""
    try:
        return name if os.path.isdir(project_path(base_dir, name)) else ""
    except ProjectError:
        return ""


def set_active(base_dir, name):
    path = os.path.join(base_dir, ACTIVE_FILE)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump({"project": str(name or ""),
                   "opened": time.strftime("%Y-%m-%d %H:%M")}, handle, indent=2)
    return name


def open_project(base_dir, name):
    try:
        name = match_name(base_dir, name)
    except ProjectError as exc:
        return f"[Error] {exc}"
    path = project_path(base_dir, name)
    set_active(base_dir, os.path.basename(path))
    return (f"[Success] Opened '{os.path.basename(path)}'.\n\n"
            + tree(base_dir, os.path.basename(path)))


# ------------------------------------------------------------------ #
# Files
# ------------------------------------------------------------------ #
def _skip(name, is_dir):
    if name.startswith(".") and name not in (".env.example", ".gitignore"):
        return True
    if is_dir:
        return name in SKIP_DIRS
    return name.lower().endswith(SKIP_SUFFIXES)


def _walk(root, limit=MAX_TREE_ENTRIES):
    """Yield (relative, absolute, is_dir), skipping generated noise."""
    count = 0
    for current, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if not _skip(d, True))
        relative_dir = os.path.relpath(current, root)
        if relative_dir != ".":
            yield relative_dir.replace("\\", "/"), current, True
            count += 1
        for name in sorted(files):
            if _skip(name, False) or name == ".vanta-project.json":
                continue
            absolute = os.path.join(current, name)
            relative = os.path.relpath(absolute, root).replace("\\", "/")
            yield relative, absolute, False
            count += 1
            if count >= limit:
                return


def file_rows(base_dir, project):
    """Flat, sorted list of the project's files for the UI and the model."""
    root = project_path(base_dir, project)
    if not os.path.isdir(root):
        raise ProjectError(f"There is no project called '{project}'.")
    rows = []
    for relative, absolute, is_dir in _walk(root):
        if is_dir:
            continue
        try:
            size = os.path.getsize(absolute)
        except OSError:
            size = 0
        rows.append({"path": relative, "absolute": absolute, "size": size})
    rows.sort(key=lambda row: (row["path"].count("/"), row["path"].lower()))
    return rows


def tree(base_dir, project, limit=MAX_TREE_ENTRIES):
    """An indented tree, which is what a model reads most reliably."""
    rows = file_rows(base_dir, project)
    if not rows:
        return f"{project}/  (empty)"
    lines = [f"{project}/"]
    # Folders are derived from the paths rather than walked separately. A tree
    # that lists src/main.py without ever showing src/ reads as a flat bag of
    # files, and the model then guesses where things live.
    shown = set()
    for row in rows[:limit]:
        parts = row["path"].split("/")
        for depth in range(len(parts) - 1):
            folder = "/".join(parts[:depth + 1])
            if folder not in shown:
                shown.add(folder)
                lines.append("  " * (depth + 1) + parts[depth] + "/")
        lines.append("  " * len(parts) + parts[-1] + f"  ({_human(row['size'])})")
    if len(rows) > limit:
        lines.append(f"  ... and {len(rows) - limit} more")
    return "\n".join(lines)


def _human(size):
    value = float(size or 0)
    for unit in ("B", "KB", "MB"):
        if value < 1024 or unit == "MB":
            return f"{value:.0f}{unit}"
        value /= 1024
    return f"{value:.0f}MB"


def read_file(base_dir, project, relative, max_bytes=MAX_READ_BYTES):
    root, target = resolve(base_dir, project, relative)
    del root
    if not os.path.exists(target):
        raise ProjectError(f"There is no file at {relative} in {project}.")
    if os.path.isdir(target):
        raise ProjectError(f"{relative} is a directory, not a file.")
    size = os.path.getsize(target)
    with open(target, "rb") as handle:
        raw = handle.read(max_bytes + 1)
    if b"\x00" in raw[:4096]:
        raise ProjectError(
            f"{relative} is a binary file ({_human(size)}); there is nothing "
            "useful to read as text.")
    text = raw[:max_bytes].decode("utf-8", errors="replace")
    truncated = size > max_bytes
    return text, {"size": size, "truncated": truncated, "path": relative}


def write_file(base_dir, project, relative, content, overwrite=False):
    root, target = resolve(base_dir, project, relative)
    del root
    body = str(content if content is not None else "")
    if len(body.encode("utf-8")) > MAX_WRITE_BYTES:
        raise ProjectError(f"That is over {_human(MAX_WRITE_BYTES)}; write it "
                           "in pieces or use RUN_COMMAND.")
    existed = os.path.exists(target)
    if existed and not overwrite:
        raise ProjectError(
            f"{relative} already exists in {project}. Read it first; if "
            'replacing it is right, reissue with overwrite="true".')
    os.makedirs(os.path.dirname(target), exist_ok=True)
    if existed:
        _backup(base_dir, project, target)
    with open(target, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(body if body.endswith("\n") else body + "\n")
    lines = body.count("\n") + 1
    return (f"[Success] {'Replaced' if existed else 'Wrote'} {relative} in "
            f"{project} ({lines} lines).")


def edit_file(base_dir, project, relative, find, replace, count=1):
    """Replace exact text in a file. Refuses when the match is not unique."""
    root, target = resolve(base_dir, project, relative)
    del root
    if not os.path.exists(target):
        raise ProjectError(f"There is no file at {relative} in {project}.")
    with open(target, "r", encoding="utf-8", errors="replace") as handle:
        original = handle.read()
    needle = str(find or "")
    if not needle:
        raise ProjectError("EDIT_PROJECT_FILE needs the exact text to replace.")
    occurrences = original.count(needle)
    if occurrences == 0:
        raise ProjectError(
            f"That text does not appear in {relative}. Read the file and copy "
            "the lines exactly, including indentation.")

    # Work out the limit before the ambiguity check, not after: count="all" is
    # documented, and parsing it with int() only in the later branch meant the
    # check itself raised ValueError on the one value it exists to allow.
    if str(count).strip().lower() in ("all", "-1", "*"):
        limit = -1
    else:
        try:
            limit = max(1, int(count or 1))
        except (TypeError, ValueError):
            limit = 1

    if occurrences > 1 and limit == 1:
        raise ProjectError(
            f"That text appears {occurrences} times in {relative}, so replacing "
            'it would be ambiguous. Include more surrounding lines, or pass '
            'count="all" if every occurrence should change.')
    _backup(base_dir, project, target)
    replacement = str(replace if replace is not None else "")
    updated = original.replace(needle, replacement, limit)
    with open(target, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(updated)
    changed = occurrences if limit == -1 else min(occurrences, limit)
    return f"[Success] Replaced {changed} occurrence(s) in {relative}."


def delete_file(base_dir, project, relative, confirmed=False):
    root, target = resolve(base_dir, project, relative)
    del root
    if not os.path.exists(target):
        raise ProjectError(f"There is no {relative} in {project}.")
    if not confirmed:
        return (f"[Approval Required] Deleting {relative} from {project} cannot "
                "be undone from here. Confirm to proceed.")
    _backup(base_dir, project, target)
    if os.path.isdir(target):
        shutil.rmtree(target)
    else:
        os.remove(target)
    return f"[Success] Deleted {relative} from {project}."


def _backup(base_dir, project, target):
    """Keep the previous contents beside the projects folder, not inside it.

    A model editing its way through a codebase gets things wrong, and an undo
    that lived in the project tree would itself be committed, listed and read
    back as source. This used to walk two directories up from the file, so a
    backup of README.md landed outside the project and a backup of
    src/main.py landed inside it -- the location depended on how deep the file
    happened to be. It is anchored to the projects root now.
    """
    try:
        backups = os.path.join(base_dir, "vault", "project-backups", str(project))
        os.makedirs(backups, exist_ok=True)
        stamp = time.strftime("%Y%m%d_%H%M%S")
        name = os.path.basename(target)
        shutil.copy2(target, os.path.join(backups, f"{stamp}_{name}"))
    except Exception:
        pass    # a missing backup must never block the edit itself


def search(base_dir, project, pattern, limit=60):
    """Grep the project. The first thing anyone does in an unfamiliar tree."""
    try:
        needle = re.compile(pattern, re.IGNORECASE)
    except re.error as exc:
        raise ProjectError(f"That is not a valid search pattern: {exc}")
    hits = []
    for row in file_rows(base_dir, project):
        try:
            with open(row["absolute"], "r", encoding="utf-8", errors="replace") as handle:
                for number, line in enumerate(handle, start=1):
                    if needle.search(line):
                        hits.append(f"{row['path']}:{number}: {line.strip()[:160]}")
                        if len(hits) >= limit:
                            return hits
        except OSError:
            continue
    return hits


def find_files(base_dir, project, glob_pattern, limit=80):
    rows = file_rows(base_dir, project)
    pattern = str(glob_pattern or "*")
    matched = [row["path"] for row in rows
               if fnmatch.fnmatch(row["path"], pattern)
               or fnmatch.fnmatch(row["path"].rsplit("/", 1)[-1], pattern)]
    return matched[:limit]


# ------------------------------------------------------------------ #
# What the model is told
# ------------------------------------------------------------------ #
def context_block(base_dir, max_files=120):
    """The open project, as a block appended to the prompt.

    Without this the coder has to ask what exists before it can do anything,
    which turns every request into two round-trips and a guess.
    """
    active = get_active(base_dir)
    if not active:
        return ""
    try:
        rows = file_rows(base_dir, active)
    except ProjectError:
        return ""
    meta = _read_meta(project_path(base_dir, active))
    lines = [f"OPEN PROJECT: {active}"]
    if meta.get("description"):
        lines.append(meta["description"])
    lines.append(f"Root: {project_path(base_dir, active)}")
    lines.append(f"{len(rows)} files. Paths below are what the project file "
                 "actions expect -- relative to the project root, never absolute.")
    for row in rows[:max_files]:
        lines.append(f"  {row['path']}")
    if len(rows) > max_files:
        lines.append(f"  ... and {len(rows) - max_files} more")

    # Filenames say what exists; they do not say where anything is. A model
    # given only the list either asks to read several files before it can do
    # anything or edits one it has not read. The structural map is parsed from
    # the source, so every line number in it is real.
    try:
        import code_intel
        structure = code_intel.structure_block(project_path(base_dir, active))
        if structure:
            lines.append("")
            lines.append(structure)
    except Exception as exc:
        lines.append(f"(structure unavailable: {exc})")

    # Build-type detection and verification commands belong to the project,
    # not to a phrase list in the prompt.  This is intentionally additive to
    # the Python AST map above: it recognises React, React Native and Blender
    # while stating honestly which languages do not yet have an AST parser.
    try:
        import build_intelligence
        lines.extend(("", build_intelligence.context_block(
            project_path(base_dir, active))))
    except Exception as exc:
        lines.append(f"(build profile unavailable: {exc})")

    # Executable-project adapters keep semantic state that a filename tree
    # cannot express. Publish only the compact manifest summary here: the
    # immutable scripts and .blend artifact stay on disk and are read through
    # their dedicated actions when more detail is needed.
    try:
        import blender_projects
        runtime_context = blender_projects.context_block(base_dir, active)
        if runtime_context:
            lines.extend(("", runtime_context))
    except Exception as exc:
        lines.append(f"(project runtime context unavailable: {exc})")

    return "\n".join(lines)
