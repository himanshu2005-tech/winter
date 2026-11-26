# -*- coding: utf-8 -*-
"""
console_window.py -- the dashboard as a window of its own, drawn in HTML.

The Tk dashboard drew every card, label and bar out of widgets: fixed pixel
sizes, no real typography, no smooth motion, and a rebuild whenever anything
changed. This serves the same information as a page -- the Control Center's
Home, Control, Chat, Activity, Output and Brain -- to a chromeless window, with
live values updated in place so nothing under the pointer is ever rebuilt.

THE BRIDGE
----------
The window is a view. State flows in through ConsoleBridge (the UI thread
records what happened: a user message, an answer, a state change; a worker
samples the machine once a second) and flows out as a small, closed set of
operations -- send a command, switch mode, set volume, lock -- each of which is
checked against an allowlist and runs through the same code the Tk buttons use.
Nothing here can run an arbitrary action; a command is only ever text handed to
the normal agent pipeline, exactly as if typed.

Same local-server hardening as the report window (loopback, per-run token in a
header, Host check, no cache, no framing).
"""

import json
import subprocess
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import report_html
import report_window as rw
from console_shell import SHELL as CONSOLE_SHELL

BASE_DIR = Path(__file__).resolve().parent
PROFILE_DIR = BASE_DIR / "vault" / "console_profile"

ALLOWED_OPS = frozenset({"command", "mode", "volume", "mute", "brightness", "lock",
                         "sleep", "screenshot", "launch", "brain", "close"})
SCRIPT_SUFFIXES = (".bat", ".cmd", ".ps1", ".vbs", ".vbe", ".js", ".jse", ".wsf", ".hta",
                   ".msi", ".reg", ".scr", ".com", ".cpl")
MAX_ACTIVITY = 120
MAX_CHAT = 60


def _headline(text):
    """First line worth reading, without Markdown punctuation."""
    import re
    for line in str(text).splitlines():
        plain = re.sub(r"[#*_`>|]+", " ", line)
        plain = " ".join(plain.split())
        if len(plain) > 3 and not re.fullmatch(r"[-:\s]+", plain):
            return plain[:140]
    return ""


class ConsoleBridge:
    """Everything the console shows, in one JSON-able dict, plus what it may do."""

    def __init__(self, base_dir=BASE_DIR, sinks=None):
        self.base_dir = str(base_dir)
        self.sinks = dict(sinks or {})
        self._lock = threading.RLock()
        self.data = {"state": "idle", "mode": "", "mode_label": "", "modes": [],
                     "metrics": {}, "audio": {}, "display": {}, "network": {},
                     "processes": [], "chat": [], "activity": [], "panels": [],
                     "latest_html": "", "headline": ""}
        self._slow_at = 0.0
        self.load_modes()

    def load_modes(self):
        """The mode list is cheap, so it is known before the window first paints."""
        try:
            from control_center import _known_modes, MODE_BLURBS
            modes = [{"id": m, "label": m.capitalize(), "blurb": MODE_BLURBS.get(m, "")}
                     for m in _known_modes()]
        except Exception:
            modes = []
        with self._lock:
            self.data["modes"] = modes
        self._load_current_mode()

    def _load_current_mode(self):
        if self.data["mode"]:
            return
        try:
            import mode_manager
            current = mode_manager.current_mode()
            self.set_mode(getattr(current, "value", current))
        except Exception:
            pass

    # ---- recorded from the UI thread ------------------------------------ #
    def set_state(self, state, mode_label=None):
        with self._lock:
            self.data["state"] = str(state or "idle")
            if mode_label is not None:
                self.data["mode_label"] = str(mode_label)

    def set_mode(self, mode, label=""):
        with self._lock:
            self.data["mode"] = str(mode or "").strip().lower()
            self.data["mode_label"] = str(label or mode or "").title()

    def user_said(self, text):
        text = str(text or "").strip()[:2000]
        with self._lock:
            last = self.data["chat"][-1] if self.data["chat"] else None
            # A typed command is recorded when sent and again when the agent
            # echoes it as the user's turn; show it once.
            if not text or (last and last["role"] == "you" and last["text"] == text):
                return
        self._chat({"role": "you", "text": text})

    def goto(self, page):
        """Ask an open window to move to a page."""
        with self._lock:
            n = (self.data.get("goto") or {}).get("n", 0) + 1
            self.data["goto"] = {"page": str(page), "n": n}

    def answered(self, text):
        text = str(text or "").strip()
        if not text:
            return
        html = report_html.render_report(text, "")["html"]
        self._chat({"role": "vanta", "text": text[:4000], "html": html})
        with self._lock:
            self.data["latest_html"] = html
            self.data["headline"] = _headline(text)

    def show_report(self, markdown, title=""):
        page = report_html.render_report(markdown, title or "")
        with self._lock:
            self.data["latest_html"] = page["html"]

    def activity(self, text):
        text = " ".join(str(text or "").split())
        if not text:
            return
        with self._lock:
            rows = self.data["activity"]
            rows.append({"t": time.time(), "text": text[:500]})
            del rows[:-MAX_ACTIVITY]

    def _chat(self, row):
        with self._lock:
            rows = self.data["chat"]
            rows.append(row)
            del rows[:-MAX_CHAT]

    # ---- sampled by the worker ------------------------------------------ #
    def sample(self, slow=False):
        try:
            import system_controls as sc
        except Exception:
            return
        try:
            import camera_awareness
            identity = camera_awareness.trust_status()
        except Exception:
            identity = "unknown"
        with self._lock:
            self.data["identity"] = identity
        try:
            snap = sc.snapshot()
            snap["net_down_h"] = sc.human_bytes(snap.get("net_down", 0))
            snap["net_up_h"] = sc.human_bytes(snap.get("net_up", 0))
            with self._lock:
                self.data["metrics"] = snap
        except Exception:
            pass
        if not slow and time.time() - self._slow_at < 12 and self.data["processes"]:
            return
        self._slow_at = time.time()
        try:
            volume, muted = sc.get_volume()
        except Exception:
            volume, muted = None, None
        audio = {"volume": volume, "muted": bool(muted)}
        display = {}
        try:
            display["brightness"] = sc.get_brightness()
        except Exception:
            display["brightness"] = None
        network = {}
        try:
            connected, ssid, signal = sc.wifi_status()
            network["wifi"] = ssid if connected and ssid else "Not connected"
            network["wifi_detail"] = f"{signal}% signal" if connected and signal else ""
            present, enabled = sc.bluetooth_status()
            network["bluetooth"] = "On" if enabled else ("Off" if present else "Not present")
        except Exception:
            pass
        processes = []
        try:
            for row in sc.top_processes(9):
                label = str(row.get("name", ""))[:40]
                if row.get("count", 1) > 1:
                    label += f"  ×{row['count']}"
                processes.append({"name": label, "mem": sc.human_bytes(row.get("memory", 0))})
        except Exception:
            pass
        self._load_current_mode()
        with self._lock:
            self.data["audio"] = audio
            self.data["display"] = display
            self.data["network"] = network
            self.data["processes"] = processes
            self.data["panels"] = self._panels()

    def _panels(self):
        try:
            import panels as store
            loaded = store.load(self.base_dir)
        except Exception:
            return []
        out = []
        for panel in loaded[:8]:
            rows = []
            for item in panel.get("items", []):
                kind = item.get("kind")
                if kind in ("stat", "progress") and item.get("label"):
                    rows.append([item["label"], item.get("value") or
                                 (f"{item['ratio'] * 100:.0f}%" if item.get("ratio") is not None else "")])
                elif kind == "kv":
                    rows.extend([[k, v] for k, v in item.get("rows", [])])
                elif kind == "bullets":
                    rows.extend([["•", e] for e in item.get("items", [])[:6]])
                elif kind in ("text", "note") and item.get("text"):
                    rows.append(["", item["text"]])
            out.append({"title": panel.get("title", ""), "note": panel.get("note", ""), "rows": rows[:10]})
        return out

    def snapshot(self):
        with self._lock:
            return json.loads(json.dumps(self.data, default=str))

    # ---- what the page may ask for -------------------------------------- #
    def control(self, op, args):
        """Run an allowlisted operation. Returns (ok, message)."""
        if op not in ALLOWED_OPS or not isinstance(args, dict):
            return False, "not an allowed operation"
        try:
            if op == "command":
                text = str(args.get("text") or "").strip()[:600]
                if not text:
                    return False, "empty"
                self.user_said(text)
                return self._sink("command", text)
            if op == "mode":
                name = str(args.get("name") or "").strip().lower()
                if name not in {m["id"] for m in self.data.get("modes", [])}:
                    return False, "unknown mode"
                return self._sink("command", f"{name} mode")
            if op == "volume":
                level = max(0, min(100, int(args.get("level"))))
                return self._machine("set_volume", level)
            if op == "brightness":
                level = max(0, min(100, int(args.get("level"))))
                return self._machine("set_brightness", level)
            if op == "mute":
                with self._lock:
                    muted = not self.data["audio"].get("muted", False)
                    self.data["audio"]["muted"] = muted
                return self._machine("set_mute", muted)
            if op in ("lock", "sleep"):
                return self._machine(op)
            if op == "screenshot":
                return self._sink("screenshot")
            if op == "launch":
                target = str(args.get("target") or "").strip()[:300]
                if not target:
                    return False, "empty"
                if target.lower().endswith(SCRIPT_SUFFIXES):
                    return False, "scripts are not opened from here"
                return self._sink("launch", target)
            if op == "brain":
                return self._sink("brain")
            if op == "close":
                return self._sink("close")
        except (TypeError, ValueError):
            return False, "bad arguments"
        return False, "unhandled"

    def _sink(self, name, *args):
        function = self.sinks.get(name)
        if function is None:
            return False, f"{name} is unavailable"
        threading.Thread(target=function, args=args, daemon=True,
                         name=f"vanta-console-{name}").start()
        return True, "ok"

    def _machine(self, function, *args):
        def run():
            try:
                import system_controls as sc
                getattr(sc, function)(*args)
            except Exception as exc:
                self.activity(f"[Error] {function}: {exc}")
        threading.Thread(target=run, daemon=True, name="vanta-console-machine").start()
        return True, "ok"


class ConsoleServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, bridge, token=None):
        import secrets
        super().__init__(("127.0.0.1", 0), _ConsoleHandler)
        self.bridge = bridge
        self.token = token or secrets.token_urlsafe(24)
        self.port = self.server_address[1]
        self._version = 0
        self._last = ""
        self._lock = threading.Lock()

    def refresh(self):
        """Bump the version only when the visible state really changed."""
        snapshot = self.bridge.snapshot()
        encoded = json.dumps(snapshot, sort_keys=True, default=str)
        with self._lock:
            if encoded != self._last:
                self._last = encoded
                self._version += 1
            return self._version, snapshot

    def current(self):
        with self._lock:
            version = self._version
        return version, self.bridge.snapshot()

    def allowed_hosts(self):
        return {f"127.0.0.1:{self.port}", f"localhost:{self.port}"}


class _ConsoleHandler(rw._Handler):
    server_version = "VantaConsole/1"

    def do_GET(self):
        if not self._host_ok():
            return self._send(421, {"error": "unexpected host"})
        parsed = urlparse(self.path)
        if parsed.path in ("/", "/index.html", "/console"):
            if not self._token_ok((parse_qs(parsed.query).get("t") or [""])[0]):
                return self._send(403, {"error": "missing or wrong token"})
            return self._send(200, CONSOLE_SHELL.encode("utf-8"), "text/html")
        if parsed.path == "/api/console":
            if not self._token_ok(self.headers.get("X-Report-Token")):
                return self._send(403, {"error": "missing or wrong token"})
            version, state = self.server.current()
            return self._send(200, {"version": version, "state": state})
        return self._send(404, {"error": "not found"})

    def do_POST(self):
        if not self._host_ok():
            self._discard_request_body()
            return self._send(421, {"error": "unexpected host"})
        if urlparse(self.path).path != "/api/control":
            self._discard_request_body()
            return self._send(404, {"error": "not found"})
        if not self._token_ok(self.headers.get("X-Report-Token")):
            self._discard_request_body()
            return self._send(403, {"error": "missing or wrong token"})
        try:
            length = min(int(self.headers.get("Content-Length") or 0), 4096)
            body = json.loads(self.rfile.read(length) or b"{}")
            op, args = str(body.get("op") or ""), body.get("args") or {}
        except (ValueError, TypeError, AttributeError):
            return self._send(400, {"error": "bad request"})
        ok, message = self.server.bridge.control(op, args)
        return self._send(200 if ok else 400, {"ok": ok, "message": message})

    do_PUT = do_DELETE = do_PATCH = lambda self: self._send(405, {"error": "not allowed"})


# --------------------------------------------------------------------------- #
# Lifecycle
# --------------------------------------------------------------------------- #
_state = {"server": None, "bridge": None, "proc": None, "worker": None}
_lock = threading.Lock()


def bridge():
    """The singleton bridge. The UI records into it whether or not a window is open."""
    with _lock:
        if _state["bridge"] is None:
            _state["bridge"] = ConsoleBridge()
        return _state["bridge"]


def _window_open():
    proc = _state["proc"]
    return proc is not None and proc.poll() is None


def window_geometry(screen_w, screen_h):
    width = max(900, min(1240, int(screen_w * 0.66)))
    height = max(620, min(820, int(screen_h * 0.82)))
    width, height = min(width, screen_w - 24), min(height, screen_h - 56)
    return width, height, max(0, (screen_w - width) // 2), max(8, (screen_h - height) // 2 - 12)


def _worker(server):
    while _state["server"] is server:
        try:
            if _window_open():
                server.bridge.sample()
                server.refresh()
        except Exception:
            pass
        time.sleep(1.0)


def open_window(page="Home", launch=True):
    """Start the server if needed and show the console. Returns info."""
    with _lock:
        server = _state["server"]
        if server is None:
            if _state["bridge"] is None:
                _state["bridge"] = ConsoleBridge()
            server = ConsoleServer(_state["bridge"])
            threading.Thread(target=server.serve_forever, daemon=True,
                             name="vanta-console-server").start()
            _state["server"] = server
            threading.Thread(target=_worker, args=(server,), daemon=True,
                             name="vanta-console-sampler").start()
        url = f"http://127.0.0.1:{server.port}/console?t={server.token}&page={page}"
        launched = False
        binary = rw.browser_binary()
        if launch and not _window_open() and binary:
            width, height, x, y = window_geometry(*rw._screen_size())
            PROFILE_DIR.mkdir(parents=True, exist_ok=True)
            _state["proc"] = subprocess.Popen(
                [binary, f"--app={url}", f"--user-data-dir={PROFILE_DIR}",
                 f"--window-size={width},{height}", f"--window-position={x},{y}",
                 "--no-first-run", "--no-default-browser-check", "--disable-sync",
                 "--disable-signin-promo", "--edge-skip-first-run",
                 "--disable-features=Translate,msEdgeSidebarV2,msEdgeSignIn,msImplicitSignin"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            launched = True
    if launched:
        # First paint should already have data in it.
        server.bridge.sample(slow=True)
        server.refresh()
    elif _window_open():
        server.bridge.goto(page)
        server.refresh()
    return {"url": url, "launched": launched, "available": bool(binary)}


def close_window():
    with _lock:
        proc = _state["proc"]
        if proc is not None and proc.poll() is None:
            proc.terminate()
        _state["proc"] = None


def toggle(page="Home"):
    """Open the console, or close it if it is already open. Returns 'opened'/'closed'/None."""
    if _window_open():
        close_window()
        return "closed"
    info = open_window(page)
    return "opened" if info["launched"] else None


def shutdown():
    with _lock:
        server = _state["server"]
        _state["server"] = None
        if server is not None:
            server.shutdown()
            server.server_close()
        proc = _state["proc"]
        if proc is not None and proc.poll() is None:
            proc.terminate()
        _state.update(proc=None, worker=None)
