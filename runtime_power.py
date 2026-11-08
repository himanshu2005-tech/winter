"""Process-wide ACTIVE/STANDBY lifecycle for VANTA.

Standby is not an operating mode. Modes describe how VANTA helps; this state
decides whether the intelligence pipeline is running at all. Only local wake
input and the alarm scheduler remain live while this flag is set.
"""
from __future__ import annotations

import threading
import time

_standby = threading.Event()
_lock = threading.RLock()
_changed_at = 0.0
_until = 0.0
_generation = 0
_monitor = False

def is_standby():
    return _standby.is_set()


def enter(duration_seconds=None, monitor=False):
    """Enter standby, optionally until a monotonic deadline.

    Returns whether standby was newly entered. Re-entering with a duration is
    still useful: it replaces the old deadline without producing a second
    transition or a second spoken acknowledgement.
    """
    global _changed_at, _until, _generation, _monitor
    with _lock:
        changed = not _standby.is_set()
        _standby.set()
        try:
            duration = float(duration_seconds or 0.0)
        except (TypeError, ValueError):
            duration = 0.0
        _until = time.monotonic() + duration if duration > 0 else 0.0
        _monitor = bool(monitor)
        _generation += 1
        _changed_at = time.time()
        return changed


def resume():
    global _changed_at, _until, _generation, _monitor
    with _lock:
        changed = _standby.is_set()
        _standby.clear()
        _until = 0.0
        _monitor = False
        _generation += 1
        _changed_at = time.time()
    if changed:
        try:
            import authentication_gate
            if authentication_gate.configured():
                authentication_gate.require_async("standby ended")
        except Exception as exc:
            print(f"[Standby] Identity gate unavailable: {type(exc).__name__}.")
    return changed


def generation():
    with _lock:
        return _generation


def remaining_seconds():
    """Seconds until timed standby ends, or None for indefinite/not active."""
    with _lock:
        if not _standby.is_set() or not _until:
            return None
        return max(0.0, _until - time.monotonic())


def monitor_enabled():
    """Whether the active standby session is armed for presence monitoring."""
    with _lock:
        return bool(_standby.is_set() and _monitor)


def expire_if_due(expected_generation):
    """Atomically leave a timed standby if this is still the same session."""
    global _changed_at, _until, _generation, _monitor
    with _lock:
        if (not _standby.is_set() or _generation != expected_generation
                or not _until or time.monotonic() < _until):
            return False
        _standby.clear()
        _until = 0.0
        _monitor = False
        _generation += 1
        _changed_at = time.time()
    try:
        import authentication_gate
        if authentication_gate.configured():
            authentication_gate.require_async("standby ended")
    except Exception as exc:
        print(f"[Standby] Identity gate unavailable: {type(exc).__name__}.")
    return True


def background_allowed():
    if is_standby():
        return False
    try:
        import authentication_gate
        return not authentication_gate.awaiting()
    except Exception:
        return True
