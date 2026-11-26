"""Windows presentation for VANTA-owned browser windows.

Browser automation and desktop vision used to have two unrelated ideas of
"the browser".  Selenium/Playwright could successfully drive VANTA's isolated
profile in the background while a later screenshot inspected Himanshu's
ordinary Firefox window.  A playback receipt then looked successful even
though the owned window was hidden and the visible window showed another tab.

This module bridges those layers without guessing from a title alone.  A
window is eligible only when its owning process is the browser process handed
to us by WebDriver, or its command line names VANTA's isolated profile.  The
page title is only a tie-breaker *inside that owned process set*.
"""

from __future__ import annotations

import os
from pathlib import Path


_BROWSER_PROCESSES = {
    "chrome.exe", "chromium.exe", "msedge.exe", "firefox.exe",
}


def _normal_path(value):
    try:
        return os.path.normcase(os.path.abspath(str(value or "")))
    except Exception:
        return os.path.normcase(str(value or ""))


def _normal_command(value):
    """Case/separator normalisation without treating a command as one path."""
    return os.path.normcase(str(value or "")).replace("/", os.sep)


def _owned_browser_pids(profile_path, hinted_pid=None):
    """Return browser PIDs provably tied to VANTA's isolated profile."""
    owned = set()
    try:
        hinted = int(hinted_pid or 0)
    except (TypeError, ValueError):
        hinted = 0
    if hinted > 0:
        owned.add(hinted)

    profile = _normal_path(profile_path)
    if not profile:
        return owned
    try:
        import psutil
        for process in psutil.process_iter(["pid", "name", "cmdline"]):
            try:
                name = str(process.info.get("name") or "").casefold()
                if name not in _BROWSER_PROCESSES:
                    continue
                command = " ".join(process.info.get("cmdline") or [])
                if profile in _normal_command(command):
                    owned.add(int(process.info["pid"]))
            except (psutil.AccessDenied, psutil.NoSuchProcess,
                    TypeError, ValueError):
                continue
    except Exception:
        # Presentation is evidence attached to an otherwise independent
        # browser action.  A diagnostic helper must never kill that action.
        pass
    return owned


def _windows():
    """Visible top-level windows as ``(hwnd, pid, title)`` tuples."""
    import win32gui
    import win32process

    rows = []

    def collect(hwnd, _):
        if not win32gui.IsWindowVisible(hwnd):
            return
        title = str(win32gui.GetWindowText(hwnd) or "").strip()
        if not title:
            return
        _thread, pid = win32process.GetWindowThreadProcessId(hwnd)
        rows.append((int(hwnd), int(pid), title))

    win32gui.EnumWindows(collect, None)
    return rows


def _pick_owned_window(rows, owned_pids, title_hint=""):
    """Choose one owned window; never fall back to another browser process."""
    candidates = [row for row in rows if int(row[1]) in set(owned_pids or ())]
    if not candidates:
        return None
    wanted = " ".join(str(title_hint or "").split()).casefold()
    if wanted:
        exact = [row for row in candidates
                 if " ".join(row[2].split()).casefold() == wanted]
        if exact:
            return exact[0]
        partial = [row for row in candidates
                   if wanted in " ".join(row[2].split()).casefold()
                   or " ".join(row[2].split()).casefold() in wanted]
        if partial:
            return partial[0]
    # A VANTA profile normally owns one top-level window. If it somehow owns
    # several, the first is still safer than a title match in Himanshu's
    # unrelated daily browser because every candidate is provenance-checked.
    return candidates[0]


def _activate_hwnd(hwnd, maximize=False):
    import win32api
    import win32con
    import win32gui
    import win32process

    flag = win32con.SW_MAXIMIZE if maximize else win32con.SW_RESTORE
    win32gui.ShowWindow(hwnd, flag)
    target_tid, _pid = win32process.GetWindowThreadProcessId(hwnd)
    current_tid = win32api.GetCurrentThreadId()
    foreground_hwnd = int(win32gui.GetForegroundWindow() or 0)
    foreground_tid = 0
    if foreground_hwnd:
        foreground_tid, _foreground_pid = (
            win32process.GetWindowThreadProcessId(foreground_hwnd))

    # Windows prevents background processes from stealing focus. Temporarily
    # joining both the current and foreground input queues to the target queue
    # makes this an explicit foreground hand-off instead of an unreliable
    # SetForegroundWindow wish. Every attachment is undone in finally.
    attached = []
    for source_tid in (current_tid, foreground_tid):
        if (source_tid and target_tid and source_tid != target_tid
                and (source_tid, target_tid) not in attached):
            try:
                win32process.AttachThreadInput(source_tid, target_tid, True)
                attached.append((source_tid, target_tid))
            except Exception:
                pass
    try:
        try:
            win32gui.SetWindowPos(
                hwnd, win32con.HWND_TOP, 0, 0, 0, 0,
                win32con.SWP_NOMOVE | win32con.SWP_NOSIZE |
                win32con.SWP_SHOWWINDOW)
        except Exception:
            pass
        try:
            win32gui.BringWindowToTop(hwnd)
        except Exception:
            pass
        win32gui.SetForegroundWindow(hwnd)
        # Give the shell a short bounded interval to complete activation.
        import time
        deadline = time.monotonic() + 1.0
        while (time.monotonic() < deadline
               and int(win32gui.GetForegroundWindow()) != int(hwnd)):
            time.sleep(0.025)
    finally:
        for source_tid, joined_tid in reversed(attached):
            try:
                win32process.AttachThreadInput(source_tid, joined_tid, False)
            except Exception:
                pass
    return int(win32gui.GetForegroundWindow()) == int(hwnd)


def present_owned_browser(profile_path: Path | str, title_hint="",
                          hinted_pid=None, maximize=False):
    """Restore and focus the exact browser window VANTA owns.

    The returned mapping is designed to be merged into a browser receipt.
    ``presented`` is true only after Windows reports that this exact HWND is
    foreground.  No title-only fallback is allowed: an unrelated Firefox tab
    must never satisfy a managed-browser presentation request.
    """
    if os.name != "nt":
        return {
            "presented": False,
            "presentation_error": "window activation is only available on Windows",
        }
    owned = _owned_browser_pids(profile_path, hinted_pid=hinted_pid)
    try:
        chosen = _pick_owned_window(_windows(), owned, title_hint=title_hint)
    except Exception as exc:
        return {"presented": False,
                "presentation_error": f"could not enumerate browser windows: {exc}"}
    if chosen is None:
        return {
            "presented": False,
            "presentation_error": (
                "no visible top-level window belongs to VANTA's managed "
                "browser profile"),
        }
    hwnd, pid, title = chosen
    try:
        foreground = _activate_hwnd(hwnd, maximize=bool(maximize))
    except Exception as exc:
        return {
            "presented": False,
            "owned_window": True,
            "window_title": title,
            "presentation_error": f"Windows refused to activate the owned window: {exc}",
        }
    return {
        "presented": bool(foreground),
        "owned_window": True,
        "foreground_verified": bool(foreground),
        "window_title": title,
        "window_pid": pid,
    }
