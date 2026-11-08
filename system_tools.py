# -*- coding: utf-8 -*-
"""
system_tools.py — machine state, screenshots, screen text, and timers.

Four everyday capabilities VANTA had no way to reach:

Machine state. "Is my laptop about to die" was unanswerable: the dashboard
showed battery and CPU, but nothing the model could read.

Screenshots as artifacts. Vision looked at the screen but could never keep or
send what it saw.

Screen text. Chrome does not expose page content to UIA -- LIST_CONTROLS inside
a browser returns Back, Reload and New Tab and nothing from the page itself.
OCR is the only way to read a website's text without driving the managed
browser, and the same is true of any app with a custom-drawn interface.

Timers. SET_ALARM needs a wall-clock time, so "remind me in twenty minutes"
required arithmetic the model frequently got wrong. A timer is expressed the
way it is spoken and converted here, where it cannot be got wrong.
"""

import datetime
import os
import re

_DURATION = re.compile(
    r"(?:(\d+)\s*(?:h|hr|hrs|hour|hours))?\s*"
    r"(?:(\d+)\s*(?:m|min|mins|minute|minutes))?\s*"
    r"(?:(\d+)\s*(?:s|sec|secs|second|seconds))?",
    re.IGNORECASE,
)

_SMALL_NUMBERS = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4,
    "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9,
    "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13,
    "fourteen": 14, "fifteen": 15, "sixteen": 16,
    "seventeen": 17, "eighteen": 18, "nineteen": 19,
}
_TENS = {"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50,
         "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90}


def _number_words(value):
    parts = str(value or "").replace("-", " ").split()
    if not parts or len(parts) > 2:
        return None
    if len(parts) == 1:
        return _SMALL_NUMBERS.get(parts[0], _TENS.get(parts[0]))
    if parts[0] in _TENS and parts[1] in _SMALL_NUMBERS:
        return _TENS[parts[0]] + _SMALL_NUMBERS[parts[1]]
    return None


def parse_duration(text):
    """'20m', '1h30', 'an hour and a half', '90' -> seconds. None if unreadable."""
    raw = " ".join(str(text or "").lower().split())
    if not raw:
        return None
    words = {"half an hour": 1800, "an hour": 3600, "a minute": 60,
             "a second": 1, "hour and a half": 5400, "quarter of an hour": 900}
    # Longest phrase first: "an hour" is a substring of "an hour and a half",
    # and matching the short one turns 90 minutes into 60.
    for phrase in sorted(words, key=len, reverse=True):
        if phrase in raw:
            return words[phrase]
    spoken = re.fullmatch(
        r"([a-z]+(?:[- ]+[a-z]+)?)\s*"
        r"(hours?|hrs?|minutes?|mins?|seconds?|secs?)", raw)
    if spoken:
        amount = _number_words(spoken.group(1))
        if amount is not None and amount > 0:
            unit = spoken.group(2)
            return amount * (3600 if unit.startswith(("h", "hr")) else
                             60 if unit.startswith(("m", "min")) else 1)
    match = _DURATION.fullmatch(raw.replace("and", "").strip())
    if match and any(match.groups()):
        hours, minutes, seconds = (int(g) if g else 0 for g in match.groups())
        total = hours * 3600 + minutes * 60 + seconds
        if total:
            return total
    # A bare number is minutes: nobody asks for a 20-second reminder.
    if raw.isdigit():
        return int(raw) * 60
    return None


def set_timer(base_dir, duration, label=""):
    """A relative reminder, converted to a real alarm."""
    seconds = parse_duration(duration)
    if not seconds:
        return (f"[Error] I could not read '{duration}' as a length of time. "
                "Try '20m', '1h30m', or '90s'.")
    if seconds > 24 * 3600:
        return "[Error] That is more than a day away; use SET_ALARM or ADD_ROUTINE instead."
    fires = datetime.datetime.now() + datetime.timedelta(seconds=seconds)
    try:
        import alarms
        result = alarms.set_alarm(fires.strftime("%H:%M"),
                                  label or f"Timer ({duration})", base_dir=base_dir)
    except Exception as exc:
        return f"[Error] Could not set the timer: {exc}"
    pretty = (f"{seconds // 3600}h {seconds % 3600 // 60}m" if seconds >= 3600
              else f"{seconds // 60}m" if seconds >= 60 else f"{seconds}s")
    return (f"[Success] Timer set for {pretty} — fires at {fires.strftime('%H:%M')}. "
            f"{result}")


def system_status():
    """Battery, CPU, memory, disk, uptime."""
    import psutil
    lines = []
    try:
        battery = psutil.sensors_battery()
        if battery is not None:
            state = "charging" if battery.power_plugged else "on battery"
            left = ""
            # psutil reports -1 unknown and -2 unlimited, and some drivers
            # return a nonsense sentinel; anything over two days is not a real
            # estimate and printing it makes VANTA look broken.
            remaining = battery.secsleft
            if (not battery.power_plugged and isinstance(remaining, int)
                    and 0 < remaining <= 48 * 3600):
                left = f", about {remaining // 3600}h {remaining % 3600 // 60}m left"
            lines.append(f"Battery: {battery.percent:.0f}% ({state}{left})")
            if battery.percent <= 20 and not battery.power_plugged:
                lines.append("  -> low and unplugged; worth mentioning to him.")
    except Exception:
        pass
    lines.append(f"CPU: {psutil.cpu_percent(interval=0.4):.0f}% across "
                 f"{psutil.cpu_count(logical=True)} threads")
    memory = psutil.virtual_memory()
    lines.append(f"Memory: {memory.percent:.0f}% used "
                 f"({memory.used / 1024**3:.1f} of {memory.total / 1024**3:.1f} GB)")
    for part in psutil.disk_partitions(all=False):
        try:
            usage = psutil.disk_usage(part.mountpoint)
            note = "  -> nearly full." if usage.percent >= 90 else ""
            lines.append(f"Disk {part.device.rstrip(chr(92))}: {usage.percent:.0f}% used "
                         f"({usage.free / 1024**3:.0f} GB free){note}")
        except (PermissionError, OSError):
            continue
    boot = datetime.datetime.fromtimestamp(psutil.boot_time())
    up = datetime.datetime.now() - boot
    lines.append(f"Uptime: {up.days}d {up.seconds // 3600}h {up.seconds % 3600 // 60}m")
    return "\n".join(lines)


def kill_process(target, confirmed=False):
    """Terminate a process by pid or name. Never VANTA itself."""
    import psutil
    wanted = str(target or "").strip()
    if not wanted:
        return "[Error] KILL_PROCESS needs a pid or a process name."
    if not confirmed:
        return (f"[Confirmation Required] Ending '{wanted}' can lose unsaved work. "
                "Reissue with confirmed=\"true\" if that is genuinely intended, or "
                "use CLOSE_WINDOW, which asks the application to close and lets it "
                "prompt about unsaved changes.")

    protected = {"system", "system idle process", "csrss.exe", "wininit.exe",
                 "winlogon.exe", "services.exe", "lsass.exe", "smss.exe",
                 "explorer.exe", "python.exe", "pythonw.exe"}
    own = {os.getpid(), os.getppid()}
    victims, refused = [], []
    for proc in psutil.process_iter(["pid", "name"]):
        try:
            info = proc.info
            name = (info.get("name") or "")
            if wanted.isdigit():
                if info["pid"] != int(wanted):
                    continue
            elif wanted.lower() not in name.lower():
                continue
            if info["pid"] in own:
                refused.append(f"{name} (that is VANTA itself)")
                continue
            if name.lower() in protected:
                refused.append(f"{name} (protected system process)")
                continue
            proc.terminate()
            victims.append(f"{name} [{info['pid']}]")
        except (psutil.NoSuchProcess, psutil.AccessDenied) as exc:
            refused.append(f"{wanted} ({type(exc).__name__})")
        except Exception:
            continue

    if not victims and not refused:
        return f"[Error] No running process matches '{wanted}'. Use LIST_PROCESSES."
    parts = []
    if victims:
        parts.append("[Success] Ended: " + ", ".join(victims) + ".")
    if refused:
        parts.append("Refused: " + "; ".join(refused) + ".")
    return " ".join(parts)


def take_screenshot(base_dir, path="", region=""):
    """Save what is on screen to a real file."""
    from PIL import ImageGrab
    try:
        box = None
        if region:
            parts = [int(float(p)) for p in re.split(r"[,\s]+", region.strip()) if p]
            if len(parts) == 4:
                box = (parts[0], parts[1], parts[0] + parts[2], parts[1] + parts[3]) \
                    if parts[2] > 0 and parts[3] > 0 and parts[2] < parts[0] else tuple(parts)
        image = ImageGrab.grab(bbox=box, all_screens=box is None)
        target = path.strip() if path else ""
        if not target:
            folder = os.path.join(base_dir, "captures")
            os.makedirs(folder, exist_ok=True)
            target = os.path.join(
                folder, f"screenshot_{datetime.datetime.now():%Y%m%d_%H%M%S}.png")
        elif not os.path.isabs(target):
            target = os.path.join(base_dir, "captures", target)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        image.save(target)
        return f"[Success] Screenshot saved to {target} ({image.width}x{image.height})."
    except Exception as exc:
        return f"[Error] Could not take a screenshot: {exc}"


_ocr_process = None
_ocr_lock = None


def _ocr_server():
    """A resident OCR process, started once and reused.

    Model load is over a minute; doing it per call made the capability
    unusable. Keeping one process alive costs that once. It still runs outside
    VANTA, so a native crash in Torch takes down the worker and nothing else —
    the next call simply starts a fresh one.
    """
    global _ocr_process, _ocr_lock
    import subprocess
    import sys
    import threading

    if _ocr_lock is None:
        _ocr_lock = threading.RLock()
    with _ocr_lock:
        if _ocr_process is not None and _ocr_process.poll() is None:
            return _ocr_process, None

        here = os.path.dirname(os.path.abspath(__file__))
        venv = os.path.join(here, ".venv", "Scripts", "python.exe")
        interpreter = venv if os.path.exists(venv) else sys.executable
        try:
            _ocr_process = subprocess.Popen(
                [interpreter, os.path.join(here, "ocr_worker.py"), "--serve"],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                text=True, bufsize=1,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except Exception as exc:
            return None, f"[Error] Could not start the OCR engine: {exc}"

        ready = _ocr_process.stdout.readline()
        if '"ready": true' not in ready.replace(" ", "").replace('"ready":true', '"ready": true'):
            try:
                import json as _json
                payload = _json.loads(ready or "{}")
                if not payload.get("ready"):
                    _ocr_process = None
                    return None, f"[Error] OCR engine failed to start: {payload.get('error', 'unknown')}"
            except Exception:
                _ocr_process = None
                return None, "[Error] The OCR engine did not start."
        return _ocr_process, None


def warm_ocr():
    """Load the OCR models in the background so the first read is not a minute."""
    import threading
    threading.Thread(target=_ocr_server, daemon=True, name="ocr-warm").start()


def _ocr_screen(region="", min_confidence=0.3, timeout=120):
    """Grab the screen and OCR it in a subprocess.

    Returns (items, offset, error). Each item carries its box in the captured
    image's pixels; the offset maps those onto the desktop.
    """
    import json
    import subprocess
    import sys
    import tempfile
    from PIL import ImageGrab

    box = None
    if region:
        parts = [int(float(p)) for p in re.split(r"[,\s]+", str(region).strip()) if p]
        if len(parts) == 4:
            box = tuple(parts)
    try:
        image = ImageGrab.grab(bbox=box, all_screens=box is None)
    except Exception as exc:
        return [], (0, 0), f"[Error] Could not capture the screen: {exc}"
    offset = (box[0], box[1]) if box else (0, 0)

    folder = tempfile.mkdtemp(prefix="vanta_ocr_")
    shot = os.path.join(folder, "screen.png")
    try:
        image.save(shot)
        process, error = _ocr_server()
        if error:
            return [], offset, error
        global _ocr_process
        try:
            process.stdin.write(json.dumps(
                {"path": shot, "min_confidence": min_confidence}) + "\n")
            process.stdin.flush()
            line = process.stdout.readline()
        except Exception as exc:
            _ocr_process = None
            return [], offset, f"[Error] The OCR engine stopped responding: {exc}"
        if not line:
            # The worker died mid-request; drop it so the next call restarts it.
            _ocr_process = None
            return [], offset, "[Error] The OCR engine exited unexpectedly."
        payload = json.loads(line)
        if not payload.get("ok"):
            return [], offset, f"[Error] OCR failed: {payload.get('error', 'unknown')}"
        return payload.get("items", []), offset, None
    except Exception as exc:
        return [], offset, f"[Error] Could not read the screen: {exc}"
    finally:
        import shutil
        shutil.rmtree(folder, ignore_errors=True)


def locate_text(needle, region="", min_confidence=0.35):
    """Find on-screen text and return its centre in SCREEN coordinates.

    This is how a small target gets hit reliably. A coordinate guessed from a
    downscaled screenshot carries that screenshot's scaling error; a box found
    by OCR is measured on the real pixels, so its centre is exact. It also
    reaches where UI Automation cannot -- Chrome exposes no page content, so a
    link or button inside a website is invisible to LIST_CONTROLS and perfectly
    readable here.
    """
    wanted = " ".join(str(needle or "").split()).casefold()
    if not wanted:
        return None, "[Error] LOCATE_TEXT needs the text to look for."

    items, offset, error = _ocr_screen(region, min_confidence)
    if error:
        return None, error

    matches = []
    for item in items:
        cleaned = " ".join(str(item.get("text", "")).split()).casefold()
        if not cleaned:
            continue
        exact = cleaned == wanted
        if not exact and wanted not in cleaned:
            continue
        left, top, right, bottom = item["box"]
        centre = (int(offset[0] + (left + right) / 2),
                  int(offset[1] + (top + bottom) / 2))
        matches.append((0 if exact else 1, -float(item.get("confidence", 0)),
                        centre, item.get("text", ""), right - left, bottom - top))

    if not matches:
        return None, (f"[Error] '{needle}' is not readable anywhere on screen. "
                      "Use READ_SCREEN_TEXT to see what is actually there.")
    matches.sort()
    _exact, _conf, centre, text, width, height = matches[0]
    detail = (f"Found '{text}' at screen {centre} (box {width}x{height})"
              + (f"; {len(matches)} matches, using the best" if len(matches) > 1 else ""))
    return centre, detail


def locate_text_box(needle, region="", min_confidence=0.35):
    """Find on-screen text and return its BOX in screen coordinates.

    locate_text returns a centre, which is what a click needs. Explaining
    something needs the extent: you cannot highlight a definition, underline a
    line or box a paragraph from a single point.
    """
    wanted = " ".join(str(needle or "").split()).casefold()
    if not wanted:
        return None, "[Error] Which text should I point at?"
    items, offset, error = _ocr_screen(region, min_confidence)
    if error:
        return None, error

    matches = []
    for item in items:
        cleaned = " ".join(str(item.get("text", "")).split()).casefold()
        if not cleaned or (cleaned != wanted and wanted not in cleaned):
            continue
        left, top, right, bottom = item["box"]
        matches.append((
            0 if cleaned == wanted else 1,
            -float(item.get("confidence", 0)),
            (int(offset[0] + left), int(offset[1] + top),
             int(offset[0] + right), int(offset[1] + bottom)),
            item.get("text", "")))

    if not matches:
        return None, (f"[Error] '{needle}' is not readable anywhere on screen. "
                      "Use READ_SCREEN_TEXT to see what is actually there.")
    matches.sort()
    _exact, _confidence, box, text = matches[0]
    detail = (f"Found '{text}' at {box}"
              + (f"; {len(matches)} matches, using the best"
                 if len(matches) > 1 else ""))
    return box, detail


def _foreground_rect():
    """The active window's screen rectangle and title, or (None, "").

    Screen reading answers "what does this say", and that question means the
    window being worked in. Returning None lets the caller fall back to the
    whole desktop rather than refuse to answer.
    """
    try:
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return None, ""
        rect = wintypes.RECT()
        if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            return None, ""
        length = user32.GetWindowTextLengthW(hwnd)
        buffer = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buffer, length + 1)
        return ((int(rect.left), int(rect.top), int(rect.right),
                 int(rect.bottom)), buffer.value or "")
    except Exception:
        return None, ""


#: Words that ask for the whole desktop rather than just the active window.
_WHOLE_SCREEN_WORDS = {"screen", "all", "desktop", "everything", "full"}

#: Below this a window rectangle is not worth cropping to; a minimised or
#: zero-size window would otherwise produce an empty answer.
_MIN_WINDOW_SPAN = 80


def read_screen_text(region="", min_confidence=0.4):
    """Everything readable in the active window, grouped into lines.

    The reason this exists: Chrome exposes none of a page's content to UI
    Automation, so LIST_CONTROLS inside a browser finds only the toolbar.
    Reading the pixels is the only way to answer "what does this page say"
    without driving the managed browser, and the same is true of any app that
    draws its own interface.

    Scoped to the foreground window, and the receipt names it. Reading the
    whole desktop answered a different question from the one asked: with
    Notepad in front holding the very text being asked about, this returned
    the editor behind it, led by its menu bar. Nothing in that was false,
    which is what made it misleading. Pass region="screen" for the desktop.
    """
    scope_label = ""
    requested = str(region or "").strip().lower()
    if requested in _WHOLE_SCREEN_WORDS:
        region = ""
        scope_label = "the whole screen"
    elif not region:
        box, title = _foreground_rect()
        wide_enough = box and (box[2] - box[0]) >= _MIN_WINDOW_SPAN
        tall_enough = box and (box[3] - box[1]) >= _MIN_WINDOW_SPAN
        if wide_enough and tall_enough:
            region = f"{box[0]},{box[1]},{box[2]},{box[3]}"
            scope_label = f"'{title}'" if title else "the active window"
        else:
            scope_label = "the whole screen"

    items, _offset, error = _ocr_screen(region, min_confidence)
    if error:
        return error
    if not items:
        where = f" in {scope_label}" if scope_label else ""
        return f"No readable text found{where}."

    # Group by row so a sentence split across boxes reads as one line.
    rows = {}
    for item in items:
        left, top, right, _bottom = item["box"]
        rows.setdefault(round(top / 14), []).append((left, item["text"]))
    lines = []
    for key in sorted(rows):
        lines.append(" ".join(text for _left, text in sorted(rows[key])))
    body = "\n".join(lines)
    where = f" in {scope_label}" if scope_label else " on screen"
    return f"Text{where} ({len(lines)} lines):\n{body[:6000]}"
