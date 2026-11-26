# -*- coding: utf-8 -*-
"""Suppress only simultaneous duplicate deliveries of one user utterance."""

import re
import threading
import time

WINDOW_SECONDS = 1.75
_lock = threading.RLock()
_recent = {}


def _key(text, source):
    normal = re.sub(r"\s+", " ", str(text or "").strip()).casefold()
    return str(source or "user").casefold(), normal


def accept(text, source="user", now=None):
    """Return False only for an identical near-simultaneous delivery."""
    key = _key(text, source)
    if not key[1]:
        return False
    moment = time.monotonic() if now is None else float(now)
    with _lock:
        previous = _recent.get(key)
        _recent[key] = moment
        stale_before = moment - max(10.0, WINDOW_SECONDS * 4)
        for old_key, seen_at in list(_recent.items()):
            if seen_at < stale_before:
                _recent.pop(old_key, None)
        return previous is None or moment - previous > WINDOW_SECONDS


def reset():
    """Test helper; runtime never needs to erase the guard."""
    with _lock:
        _recent.clear()
