# -*- coding: utf-8 -*-
"""Short-lived, receipt-backed artifacts produced during the current session.

Conversational memory is not an artifact store.  A sentence such as "I saved
it" can be wrong, while a path returned by a successful action is evidence.
This module keeps that evidence available for follow-ups such as "move it to
the desktop" without asking the model to reconstruct a path from prose.
"""

import os
import threading
from collections import deque


_lock = threading.RLock()
_items = deque(maxlen=24)


def record(path, kind="file", source_action="", metadata=None):
    absolute = os.path.abspath(os.path.expandvars(os.path.expanduser(str(path))))
    item = {
        "path": absolute,
        "kind": str(kind or "file"),
        "source_action": str(source_action or ""),
        "metadata": dict(metadata or {}),
    }
    with _lock:
        _items.append(item)
    return dict(item)


def latest(kind=""):
    with _lock:
        candidates = list(reversed(_items))
    for item in candidates:
        if kind and item["kind"] != kind:
            continue
        if os.path.exists(item["path"]):
            return dict(item)
    return None


def moved(old_path, new_path):
    old = os.path.normcase(os.path.abspath(str(old_path)))
    with _lock:
        for item in reversed(_items):
            if os.path.normcase(item["path"]) == old:
                item["path"] = os.path.abspath(str(new_path))
                item["source_action"] = "MOVE_FILE"
                return dict(item)
    return record(new_path, source_action="MOVE_FILE")


def desktop_directory():
    """Windows' configured Desktop folder, including OneDrive redirection."""
    try:
        import ctypes
        buffer = ctypes.create_unicode_buffer(32768)
        # CSIDL_DESKTOPDIRECTORY.  Unlike ~/Desktop this follows shell-folder
        # redirection and therefore points at the desktop the user can see.
        result = ctypes.windll.shell32.SHGetFolderPathW(
            None, 0x0010, None, 0, buffer)
        if result == 0 and buffer.value:
            return os.path.abspath(buffer.value)
    except Exception:
        pass
    return os.path.abspath(os.path.join(os.path.expanduser("~"), "Desktop"))


def grounding_text():
    item = latest()
    if not item:
        return ""
    return ("[VERIFIED CURRENT ARTIFACT]\n"
            f"kind={item['kind']}\npath={item['path']}\n"
            f"created_by={item['source_action'] or 'unknown'}\n"
            "Use this exact path when the current request refers to the latest "
            "file, screenshot, image, result, 'it', or 'that'.")

