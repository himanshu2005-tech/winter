# -*- coding: utf-8 -*-
"""
system_controls.py -- direct control of this machine, with no model in the loop.

The Control Center used to send every button press through the agent as English
("turn the volume down"), which meant a slider took a planner round-trip and
several seconds to move a hardware setting the OS changes in microseconds. A
control surface that lags behind the hand is not a control surface. Everything
here talks to Windows directly and returns in milliseconds.

Two rules hold throughout:

Nothing here blocks forever. Anything that shells out gets a timeout, because a
hung `netsh` would freeze the Tk main loop and take the whole UI with it.

Nothing here pretends. Where Windows genuinely will not let a non-elevated
process do something -- toggling the Bluetooth radio, disabling an adapter --
this reports the real state and opens the Settings page, rather than showing a
switch that silently does nothing.
"""

import ctypes
import os
import subprocess
import threading
import time

_CREATE_NO_WINDOW = 0x08000000
_thread_state = threading.local()


def _run(args, timeout=6):
    """Shell out without flashing a console window, and never hang."""
    try:
        result = subprocess.run(
            args, capture_output=True, text=True, timeout=timeout,
            creationflags=_CREATE_NO_WINDOW,
            encoding="utf-8", errors="replace",
        )
        return result.returncode, (result.stdout or ""), (result.stderr or "")
    except subprocess.TimeoutExpired:
        return 1, "", f"timed out after {timeout}s"
    except Exception as exc:
        return 1, "", str(exc)


def _powershell(script, timeout=8):
    return _run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                timeout=timeout)


# ------------------------------------------------------------------ #
# Audio -- the real endpoint volume, not keypress nudges
#
# pycaw is the usual way to reach IAudioEndpointVolume, but it is not
# installed here and adding a dependency for three COM calls is not worth it;
# comtypes is already present and pycaw is a thin wrapper over exactly this.
# Keystroke volume (VK_VOLUME_UP) only moves in fixed steps and cannot report
# the current level at all, so a slider is impossible to build on it.
# ------------------------------------------------------------------ #

def _endpoint_volume():
    """Cached per-thread IAudioEndpointVolume, with COM initialised once."""
    existing = getattr(_thread_state, "volume", None)
    if existing is not None:
        return existing

    from ctypes import HRESULT, POINTER, c_float, c_uint, c_void_p
    from ctypes.wintypes import BOOL, DWORD
    import comtypes
    from comtypes import GUID, COMMETHOD, IUnknown

    if not getattr(_thread_state, "com_ready", False):
        try:
            comtypes.CoInitialize()
        except Exception:
            pass
        _thread_state.com_ready = True

    class IAudioEndpointVolume(IUnknown):
        _iid_ = GUID("{5CDF2C82-841E-4546-9722-0CF74078229A}")
        _methods_ = (
            COMMETHOD([], HRESULT, "RegisterControlChangeNotify",
                      (["in"], c_void_p, "pNotify")),
            COMMETHOD([], HRESULT, "UnregisterControlChangeNotify",
                      (["in"], c_void_p, "pNotify")),
            COMMETHOD([], HRESULT, "GetChannelCount",
                      (["out"], POINTER(c_uint), "pnChannelCount")),
            COMMETHOD([], HRESULT, "SetMasterVolumeLevel",
                      (["in"], c_float, "fLevelDB"),
                      (["in"], POINTER(GUID), "pguidEventContext")),
            COMMETHOD([], HRESULT, "SetMasterVolumeLevelScalar",
                      (["in"], c_float, "fLevel"),
                      (["in"], POINTER(GUID), "pguidEventContext")),
            COMMETHOD([], HRESULT, "GetMasterVolumeLevel",
                      (["out"], POINTER(c_float), "pfLevelDB")),
            COMMETHOD([], HRESULT, "GetMasterVolumeLevelScalar",
                      (["out"], POINTER(c_float), "pfLevel")),
            COMMETHOD([], HRESULT, "SetChannelVolumeLevel",
                      (["in"], c_uint, "nChannel"), (["in"], c_float, "fLevelDB"),
                      (["in"], POINTER(GUID), "pguidEventContext")),
            COMMETHOD([], HRESULT, "SetChannelVolumeLevelScalar",
                      (["in"], c_uint, "nChannel"), (["in"], c_float, "fLevel"),
                      (["in"], POINTER(GUID), "pguidEventContext")),
            COMMETHOD([], HRESULT, "GetChannelVolumeLevel",
                      (["in"], c_uint, "nChannel"),
                      (["out"], POINTER(c_float), "pfLevelDB")),
            COMMETHOD([], HRESULT, "GetChannelVolumeLevelScalar",
                      (["in"], c_uint, "nChannel"),
                      (["out"], POINTER(c_float), "pfLevel")),
            COMMETHOD([], HRESULT, "SetMute",
                      (["in"], BOOL, "bMute"),
                      (["in"], POINTER(GUID), "pguidEventContext")),
            COMMETHOD([], HRESULT, "GetMute", (["out"], POINTER(BOOL), "pbMute")),
        )

    class IMMDevice(IUnknown):
        _iid_ = GUID("{D666063F-1587-4E43-81F1-B948E807363F}")
        _methods_ = (
            COMMETHOD([], HRESULT, "Activate",
                      (["in"], POINTER(GUID), "iid"), (["in"], DWORD, "dwClsCtx"),
                      (["in"], c_void_p, "pActivationParams"),
                      (["out"], POINTER(POINTER(IUnknown)), "ppInterface")),
        )

    class IMMDeviceEnumerator(IUnknown):
        _iid_ = GUID("{A95664D2-9614-4F35-A746-DE8DB63617E6}")
        _methods_ = (
            COMMETHOD([], HRESULT, "NotImpl1"),
            COMMETHOD([], HRESULT, "GetDefaultAudioEndpoint",
                      (["in"], DWORD, "dataFlow"), (["in"], DWORD, "role"),
                      (["out"], POINTER(POINTER(IMMDevice)), "ppDevice")),
        )

    enumerator = comtypes.CoCreateInstance(
        GUID("{BCDE0395-E52F-467C-8E3D-C4579291692E}"),
        IMMDeviceEnumerator, comtypes.CLSCTX_INPROC_SERVER)
    device = enumerator.GetDefaultAudioEndpoint(0, 1)  # eRender, eMultimedia
    interface = device.Activate(IAudioEndpointVolume._iid_,
                                comtypes.CLSCTX_INPROC_SERVER, None)
    _thread_state.volume = interface.QueryInterface(IAudioEndpointVolume)
    return _thread_state.volume


def get_volume():
    """(percent, muted) for the default output device, or (None, None)."""
    try:
        endpoint = _endpoint_volume()
        return (int(round(endpoint.GetMasterVolumeLevelScalar() * 100)),
                bool(endpoint.GetMute()))
    except Exception:
        return None, None


def set_volume(percent):
    try:
        level = max(0, min(100, int(percent)))
        _endpoint_volume().SetMasterVolumeLevelScalar(level / 100.0, None)
        return f"[Success] Volume {level}%."
    except Exception as exc:
        return f"[Error] Could not set volume: {exc}"


def set_mute(muted):
    try:
        _endpoint_volume().SetMute(bool(muted), None)
        return f"[Success] {'Muted' if muted else 'Unmuted'}."
    except Exception as exc:
        return f"[Error] Could not change mute: {exc}"


def toggle_mute():
    _, muted = get_volume()
    if muted is None:
        return "[Error] No audio endpoint available."
    return set_mute(not muted)


# ------------------------------------------------------------------ #
# Display
# ------------------------------------------------------------------ #

def get_brightness():
    """Panel brightness as a percentage, or None on an external monitor.

    Only laptop panels expose WmiMonitorBrightness; a display over HDMI
    reports nothing, which is a missing capability rather than an error.
    """
    code, out, _ = _powershell(
        "(Get-CimInstance -Namespace root/WMI -ClassName WmiMonitorBrightness "
        "-ErrorAction SilentlyContinue).CurrentBrightness")
    if code != 0:
        return None
    try:
        return int(str(out).strip().splitlines()[0])
    except (ValueError, IndexError):
        return None


def set_brightness(percent):
    try:
        level = max(0, min(100, int(percent)))
    except (TypeError, ValueError):
        return "[Error] Brightness must be a number from 0 to 100."
    code, _, err = _powershell(
        "(Get-WmiObject -Namespace root/WMI -Class WmiMonitorBrightnessMethods)"
        f".WmiSetBrightness(1,{level})")
    if code != 0:
        detail = (err or "").strip().splitlines()
        return f"[Error] Could not set brightness: {detail[0] if detail else 'WMI refused'}"
    return f"[Success] Brightness {level}%."


# ------------------------------------------------------------------ #
# Media transport
# ------------------------------------------------------------------ #

_MEDIA_KEYS = {"playpause": 0xB3, "next": 0xB0, "previous": 0xB1, "stop": 0xB2}


def media(action):
    """Send a media key to whichever app currently owns transport."""
    code = _MEDIA_KEYS.get(str(action or "").lower())
    if code is None:
        return f"[Error] Unknown media action '{action}'."
    user32 = ctypes.windll.user32
    user32.keybd_event(code, 0, 0, 0)
    user32.keybd_event(code, 0, 2, 0)  # KEYEVENTF_KEYUP
    return f"[Success] Media: {action}."


# ------------------------------------------------------------------ #
# Network
# ------------------------------------------------------------------ #

def wifi_status():
    """(connected, ssid, signal_percent). Reads, never changes."""
    code, out, _ = _run(["netsh", "wlan", "show", "interfaces"], timeout=6)
    if code != 0:
        return False, None, None
    ssid, signal, state = None, None, ""
    for line in out.splitlines():
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key, value = key.strip().lower(), value.strip()
        if key == "state":
            state = value.lower()
        elif key == "ssid":
            ssid = value
        elif key == "signal":
            try:
                signal = int(value.rstrip("%"))
            except ValueError:
                pass
    return state == "connected", ssid, signal


def wifi_disconnect():
    code, _, err = _run(["netsh", "wlan", "disconnect"], timeout=8)
    if code == 0:
        return "[Success] Wi-Fi disconnected."
    return f"[Error] Could not disconnect: {err.strip() or 'netsh refused'}"


def wifi_connect(ssid):
    name = str(ssid or "").strip()
    if not name:
        return "[Error] Which network?"
    code, out, err = _run(["netsh", "wlan", "connect", f"name={name}"], timeout=12)
    if code == 0:
        return f"[Success] Connecting to {name}."
    return f"[Error] {(err or out).strip() or 'No saved profile for that network.'}"


def known_networks(limit=12):
    code, out, _ = _run(["netsh", "wlan", "show", "profiles"], timeout=6)
    if code != 0:
        return []
    names = []
    for line in out.splitlines():
        if "All User Profile" in line and ":" in line:
            names.append(line.split(":", 1)[1].strip())
    return names[:limit]


def bluetooth_status():
    """(present, enabled). Toggling the radio needs elevation, so this reads."""
    code, out, _ = _powershell(
        "(Get-PnpDevice -Class Bluetooth -ErrorAction SilentlyContinue | "
        "Where-Object { $_.FriendlyName -notlike '*Enumerator*' } | "
        "Select-Object -First 1 -ExpandProperty Status)", timeout=10)
    if code != 0:
        return False, False
    status = str(out).strip().upper()
    if not status:
        return False, False
    return True, status == "OK"


def open_settings(page="bluetooth"):
    """Hand off to the real Settings page for what elevation blocks."""
    try:
        os.startfile(f"ms-settings:{page}")
        return f"[Success] Opened Settings: {page}."
    except Exception as exc:
        return f"[Error] Could not open Settings: {exc}"


# ------------------------------------------------------------------ #
# Power
# ------------------------------------------------------------------ #

def lock():
    ctypes.windll.user32.LockWorkStation()
    return "[Success] Locked."


def sleep():
    # False, False, False = suspend rather than hibernate, and do not force
    # apps to close, so anything with unsaved work can object.
    ctypes.windll.powrprof.SetSuspendState(False, False, False)
    return "[Success] Sleeping."


def shutdown(confirmed=False, restart=False):
    """Irreversible for unsaved work, so it refuses without confirmation."""
    what = "restart" if restart else "shut down"
    if not confirmed:
        return (f"[Approval Required] This will {what} the machine and close "
                "everything running. Confirm to proceed.")
    code, _, err = _run(["shutdown", "/r" if restart else "/s", "/t", "0"], timeout=8)
    if code == 0:
        return f"[Success] Machine will {what} now."
    return f"[Error] Could not {what}: {err.strip()}"


def sign_out(confirmed=False):
    if not confirmed:
        return ("[Approval Required] This will sign out and close everything "
                "running. Confirm to proceed.")
    code, _, err = _run(["shutdown", "/l"], timeout=8)
    return "[Success] Signing out." if code == 0 else f"[Error] {err.strip()}"


# ------------------------------------------------------------------ #
# Live machine state
# ------------------------------------------------------------------ #

_net_previous = {"time": 0.0, "sent": 0, "received": 0}


def snapshot():
    """One cheap read of everything the dashboard shows live.

    Deliberately excludes brightness, Wi-Fi and Bluetooth: those shell out and
    cost hundreds of milliseconds, far too slow for a per-second refresh.
    """
    import psutil

    data = {}
    data["cpu"] = psutil.cpu_percent(interval=None)
    memory = psutil.virtual_memory()
    data["ram"] = memory.percent
    data["ram_used_gb"] = memory.used / (1024 ** 3)
    data["ram_total_gb"] = memory.total / (1024 ** 3)

    try:
        disk = psutil.disk_usage(os.path.abspath(os.sep))
        data["disk"] = disk.percent
        data["disk_free_gb"] = disk.free / (1024 ** 3)
    except Exception:
        data["disk"] = None

    battery = getattr(psutil, "sensors_battery", lambda: None)()
    if battery is not None:
        data["battery"] = battery.percent
        data["charging"] = bool(battery.power_plugged)
        secs = battery.secsleft
        unlimited = getattr(psutil, "POWER_TIME_UNLIMITED", -1)
        unknown = getattr(psutil, "POWER_TIME_UNKNOWN", -2)
        data["battery_left"] = (None if secs in (unlimited, unknown) or secs < 0
                                else secs)
    else:
        data["battery"] = None

    counters = psutil.net_io_counters()
    now = time.time()
    elapsed = now - _net_previous["time"]
    if _net_previous["time"] and 0 < elapsed < 30:
        data["net_up"] = max(0.0, (counters.bytes_sent - _net_previous["sent"]) / elapsed)
        data["net_down"] = max(0.0, (counters.bytes_recv - _net_previous["received"]) / elapsed)
    else:
        data["net_up"] = data["net_down"] = 0.0
    _net_previous.update(time=now, sent=counters.bytes_sent,
                         received=counters.bytes_recv)

    data["uptime"] = time.time() - psutil.boot_time()
    return data


def top_processes(limit=12, order="memory"):
    """The heaviest processes, merged by name.

    Per-PID rows are the wrong unit: thirty Chrome helpers say nothing, while
    "Chrome, 30 processes, 4.2 GB" is the fact worth acting on.
    """
    import psutil

    merged = {}
    for process in psutil.process_iter(["name", "memory_info", "cpu_percent", "pid"]):
        try:
            info = process.info
            name = info.get("name") or "?"
            memory = getattr(info.get("memory_info"), "rss", 0) or 0
            entry = merged.setdefault(name, {"name": name, "memory": 0, "cpu": 0.0,
                                             "count": 0, "pid": info.get("pid"),
                                             "peak": 0})
            entry["memory"] += memory
            entry["cpu"] += info.get("cpu_percent") or 0.0
            entry["count"] += 1
            # Keep the hungriest PID so ending it targets something meaningful.
            if memory > entry["peak"]:
                entry["peak"] = memory
                entry["pid"] = info.get("pid")
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    key = "cpu" if str(order).lower().startswith("c") else "memory"
    return sorted(merged.values(), key=lambda row: row[key], reverse=True)[:limit]


def end_process(name="", pid=None, confirmed=False):
    """Close every process sharing a name. Unsaved work dies, so: confirm."""
    import psutil

    if not confirmed:
        return ("[Approval Required] Ending a process discards anything it has "
                "not saved. Confirm to proceed.")
    if not name and pid is None:
        return "[Error] Nothing named to end."
    killed, refused = 0, 0
    for process in psutil.process_iter(["name", "pid"]):
        try:
            if pid is not None and process.info["pid"] != int(pid):
                continue
            if name and str(process.info["name"] or "").lower() != str(name).lower():
                continue
            process.terminate()
            killed += 1
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            refused += 1
    if not killed:
        return f"[Error] Nothing ended{' (access denied)' if refused else ''}."
    plural = "es" if killed > 1 else ""
    tail = f"; {refused} refused (system-owned)." if refused else "."
    return f"[Success] Ended {killed} process{plural}{tail}"


def empty_recycle_bin(confirmed=False):
    if not confirmed:
        return "[Approval Required] Emptying the Recycle Bin cannot be undone."
    try:
        # SHERB_NOCONFIRMATION | SHERB_NOPROGRESSUI | SHERB_NOSOUND
        ctypes.windll.shell32.SHEmptyRecycleBinW(None, None, 7)
        return "[Success] Recycle Bin emptied."
    except Exception as exc:
        return f"[Error] {exc}"


def human_bytes(value):
    value = float(value or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"


def human_duration(seconds):
    seconds = int(max(0, seconds or 0))
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes = rest // 60
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"
