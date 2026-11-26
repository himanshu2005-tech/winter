# -*- coding: utf-8 -*-
"""Local system-awareness layer for VANTA.

It observes the workstation locally and preserves a small activity timeline.
Raw window titles remain local; planner context receives only the active app and
operational state unless a future user-controlled privacy setting enables more.
"""

import ctypes
import json
import os
from datetime import datetime


_STATE_RELATIVE = os.path.join("vault", "system", "system_awareness.json")


def _path(base_dir):
    return os.path.join(base_dir, _STATE_RELATIVE)


def _active_window():
    try:
        import win32gui
        import win32process
        hwnd = win32gui.GetForegroundWindow()
        title = win32gui.GetWindowText(hwnd).strip()
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        process = "unknown"
        try:
            import psutil
            process = psutil.Process(pid).name()
        except Exception:
            pass
        return {"process": process, "title": title}
    except Exception:
        return {"process": "unknown", "title": ""}


def _idle_seconds():
    class LASTINPUTINFO(ctypes.Structure):
        _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]
    try:
        info = LASTINPUTINFO()
        info.cbSize = ctypes.sizeof(info)
        ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info))
        return max(0, int((ctypes.windll.kernel32.GetTickCount64() - info.dwTime) / 1000))
    except Exception:
        return None


def snapshot():
    """Capture the current workstation state without reading file contents."""
    state = {
        "captured_at": datetime.now().isoformat(timespec="seconds"),
        "active_window": _active_window(),
        "idle_seconds": _idle_seconds(),
    }
    try:
        import psutil
        battery = psutil.sensors_battery()
        state["battery"] = {
            "percent": battery.percent if battery else None,
            "plugged": battery.power_plugged if battery else None,
        }
        state["cpu_percent"] = psutil.cpu_percent(interval=None)
        state["ram_percent"] = psutil.virtual_memory().percent
    except Exception:
        pass
    return state


def observe(base_dir):
    """Persist a compact local activity timeline, capped to recent transitions."""
    path = _path(base_dir)
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        data = {"version": 1, "latest": {}, "activity": []}

    current = snapshot()
    previous = data.get("latest", {})
    changed = current.get("active_window", {}).get("process") != previous.get("active_window", {}).get("process")
    if changed and current.get("active_window", {}).get("process") not in ("unknown", ""):
        data.setdefault("activity", []).append({
            "at": current["captured_at"],
            "process": current["active_window"]["process"],
            "title": current["active_window"].get("title", ""),
        })
    data["latest"] = current
    data["activity"] = data.get("activity", [])[-250:]
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    return current, changed


def context_for_agent(base_dir):
    """Share only an operational summary with the planner, not raw window titles."""
    try:
        with open(_path(base_dir), "r", encoding="utf-8") as f:
            latest = json.load(f).get("latest", {})
    except Exception:
        latest = snapshot()
    active = latest.get("active_window", {})
    battery = latest.get("battery", {})
    lines = ["--- LOCAL SYSTEM AWARENESS ---"]
    if active.get("process"):
        lines.append(f"Active application: {active.get('process')}")
    if latest.get("idle_seconds") is not None:
        lines.append(f"User idle: {latest['idle_seconds']} seconds")
    if battery.get("percent") is not None:
        lines.append(f"Battery: {battery['percent']}% ({'charging' if battery.get('plugged') else 'on battery'})")
    if latest.get("cpu_percent") is not None:
        lines.append(f"System load: CPU {latest['cpu_percent']}%, RAM {latest.get('ram_percent', 'unknown')}%")
    return "\n".join(lines)
