"""Vault-rooted file navigation, editing, and diff utilities for VANTA."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
import difflib
import hashlib
import os
import shutil
import tempfile

TEXT_EXTENSIONS = frozenset({
    ".txt", ".md", ".rst", ".py", ".pyw", ".js", ".jsx", ".ts", ".tsx",
    ".json", ".jsonl", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".csv",
    ".html", ".css", ".sql", ".ps1", ".bat", ".sh", ".log",
})
BLOCKED_DIRECTORIES = frozenset({
    ".git", ".venv", "venv", "node_modules", "dist", "build", "__pycache__",
    ".pytest_cache", ".mypy_cache", "models", "local_qwen_3b", "faces",
    ".vanta_backups",
})
SENSITIVE_NAME_PARTS = (
    ".env", "credential", "password", "passwd", "secret", "token", "api_key",
    "apikey", "private_key", "owner_face", "intruder", "telegram_chat_id",
)
MAX_EDIT_BYTES = 2 * 1024 * 1024


def vault_root(base_dir):
    root = (Path(base_dir).resolve() / "vault").resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


def configured_roots(base_dir):
    """Compatibility API: the Control Center is intentionally Vault-only."""
    return (vault_root(base_dir),)


def _inside(path: Path, roots):
    resolved = path.resolve()
    return any(resolved == root or root in resolved.parents for root in roots)


def _sensitive_name(path: Path):
    name = path.name.casefold()
    return any(part in name for part in SENSITIVE_NAME_PARTS)


def _resolve(root, relative="."):
    root = Path(root).resolve()
    candidate = (root / str(relative or ".")).resolve()
    if not _inside(candidate, (root,)):
        raise PermissionError("Path escapes the VANTA Vault.")
    return candidate


def is_safe_text_path(path, roots):
    candidate = Path(path).resolve()
    normalized_roots = tuple(Path(root).resolve() for root in roots)
    if not _inside(candidate, normalized_roots) or not candidate.is_file():
        return False
    if candidate.suffix.casefold() not in TEXT_EXTENSIONS or _sensitive_name(candidate):
        return False
    relative_parts = set()
    for root in normalized_roots:
        if candidate == root or root in candidate.parents:
            relative_parts = {part.casefold() for part in candidate.relative_to(root).parts[:-1]}
            break
    if relative_parts & BLOCKED_DIRECTORIES:
        return False
    try:
        return candidate.stat().st_size <= MAX_EDIT_BYTES
    except OSError:
        return False


def list_vault_directory(root, relative="."):
    root = Path(root).resolve()
    folder = _resolve(root, relative)
    if not folder.is_dir():
        raise NotADirectoryError(str(relative))
    rows = []
    if folder != root:
        rows.append({"kind": "parent", "name": "..", "relative": str(folder.parent.relative_to(root)) or "."})
    for item in sorted(folder.iterdir(), key=lambda p: (not p.is_dir(), p.name.casefold())):
        if item.name.casefold() in BLOCKED_DIRECTORIES or _sensitive_name(item):
            continue
        rel = str(item.relative_to(root))
        if item.is_dir():
            rows.append({"kind": "folder", "name": item.name, "relative": rel})
        elif is_safe_text_path(item, (root,)):
            rows.append({
                "kind": "file", "name": item.name, "relative": rel,
                "path": str(item), "size": item.stat().st_size,
            })
    return {"root": str(root), "current": str(folder.relative_to(root)) or ".", "entries": rows}


def content_hash(text):
    return hashlib.sha256(str(text).encode("utf-8")).hexdigest()


def read_vault_file(root, relative, max_chars=250000):
    root = Path(root).resolve()
    candidate = _resolve(root, relative)
    if not is_safe_text_path(candidate, (root,)):
        raise PermissionError("File is sensitive, binary, too large, or outside the Vault.")
    text = candidate.read_text(encoding="utf-8", errors="replace")
    if len(text) > max_chars:
        raise ValueError("File is too large for the Vault editor.")
    return {
        "path": str(candidate), "relative": str(candidate.relative_to(root)),
        "content": text, "hash": content_hash(text),
    }


def unified_diff(original, edited, relative="file"):
    return list(difflib.unified_diff(
        str(original).splitlines(), str(edited).splitlines(),
        fromfile=f"a/{relative}", tofile=f"b/{relative}", lineterm="",
    ))


def save_vault_file(root, relative, content, expected_hash):
    """Atomically save after an optimistic-concurrency check and backup."""
    root = Path(root).resolve()
    candidate = _resolve(root, relative)
    if not is_safe_text_path(candidate, (root,)):
        raise PermissionError("Only an existing safe Vault text file may be edited.")
    current = candidate.read_text(encoding="utf-8", errors="replace")
    if content_hash(current) != str(expected_hash or ""):
        raise RuntimeError("The file changed outside the editor. Reload before saving.")
    edited = str(content)
    if len(edited.encode("utf-8")) > MAX_EDIT_BYTES:
        raise ValueError("Edited file exceeds the Vault editor size limit.")
    if edited == current:
        return {"changed": False, "hash": content_hash(current), "backup": None}

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    backup = root / ".vanta_backups" / candidate.relative_to(root)
    backup = backup.with_name(backup.name + f".{stamp}.bak")
    backup.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(candidate, backup)

    fd, temp_name = tempfile.mkstemp(prefix=".vanta-edit-", suffix=candidate.suffix, dir=str(candidate.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(edited)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, candidate)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)
    return {"changed": True, "hash": content_hash(edited), "backup": str(backup)}


# Legacy compatibility used by older tests/tools.
def scan_text_files(roots, query="", limit=800):
    needle = str(query or "").casefold().strip()
    results = []
    for root in tuple(Path(item).resolve() for item in roots):
        if not root.exists():
            continue
        for current, dirs, files in os.walk(root):
            dirs[:] = [d for d in dirs if d.casefold() not in BLOCKED_DIRECTORIES]
            for filename in files:
                candidate = Path(current) / filename
                if not is_safe_text_path(candidate, (root,)):
                    continue
                display = str(candidate.relative_to(root))
                if needle and needle not in display.casefold():
                    continue
                results.append({"path": str(candidate), "display": display, "root": str(root)})
                if len(results) >= limit:
                    return sorted(results, key=lambda row: row["display"].casefold())
    return sorted(results, key=lambda row: row["display"].casefold())


def read_text_preview(path, roots, max_chars=120000):
    candidate = Path(path).resolve()
    if not is_safe_text_path(candidate, roots):
        raise PermissionError("This file is outside the Vault, sensitive, binary, or too large.")
    text = candidate.read_text(encoding="utf-8", errors="replace")
    return text[:max_chars] + ("\n\n[Preview truncated]" if len(text) > max_chars else "")