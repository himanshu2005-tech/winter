# -*- coding: utf-8 -*-
"""
brain_server.py -- serves the 3D knowledge-graph map to a browser on this machine.

The graph contains names, phone numbers and email addresses, so "a local web
page" is not harmless: any web page open in the same browser can send requests
to http://127.0.0.1. Three things stop that being a leak:

* the server binds to 127.0.0.1 only, never the network;
* every data request needs a per-run random token, sent in a header (a
  cross-site request cannot set custom headers without a CORS preflight, which
  this server never approves);
* the Host header must be the loopback name and the port in use, which defeats
  DNS-rebinding, where a hostile domain is pointed at 127.0.0.1.

The page itself carries the token in its address, and responses forbid being
framed, cached, or sending a referrer.
"""

import hmac
import json
import os
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlparse

import brain_map

LIB_VERSION = "3d-force-graph@1.73.4"
LIB_URLS = (
    f"https://cdn.jsdelivr.net/npm/{LIB_VERSION}/dist/3d-force-graph.min.js",
    f"https://unpkg.com/{LIB_VERSION}/dist/3d-force-graph.min.js",
)
GRAPH_CACHE_SECONDS = 8.0

_CSP = ("default-src 'none'; script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline'; connect-src 'self'; "
        "img-src 'self' data: blob:; frame-ancestors 'none'; base-uri 'none'; "
        "form-action 'none'")

_state = {"server": None, "thread": None, "info": None}
_lock = threading.Lock()


class BrainServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, base_dir, store_provider, token=None):
        super().__init__(("127.0.0.1", 0), _Handler)
        self.base_dir = base_dir
        self.store_provider = store_provider
        self.token = token or secrets.token_urlsafe(24)
        self.port = self.server_address[1]
        self._cache = {"at": 0.0, "graph": None}
        self._cache_lock = threading.Lock()

    def graph(self):
        with self._cache_lock:
            if (self._cache["graph"] is not None
                    and time.time() - self._cache["at"] < GRAPH_CACHE_SECONDS):
                return self._cache["graph"]
        data = brain_map.fetch_graph(self.store_provider())
        with self._cache_lock:
            self._cache.update(at=time.time(), graph=data)
        return data

    def allowed_hosts(self):
        return {f"127.0.0.1:{self.port}", f"localhost:{self.port}"}


class _Handler(BaseHTTPRequestHandler):
    server_version = "VantaBrain/1"

    def log_message(self, fmt, *args):  # quiet: the console is VANTA's
        return

    # -- plumbing ----------------------------------------------------- #
    def _send(self, status, body, content_type="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type + "; charset=utf-8"
                         if content_type.startswith("text/") or content_type == "application/json"
                         else content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", _CSP)
        self.send_header("X-Frame-Options", "DENY")
        self.end_headers()
        self.wfile.write(data)

    def _host_ok(self):
        return (self.headers.get("Host") or "").lower() in self.server.allowed_hosts()

    def _token_ok(self, supplied):
        return bool(supplied) and hmac.compare_digest(
            str(supplied), self.server.token)

    # -- routes ------------------------------------------------------- #
    def do_GET(self):
        if not self._host_ok():
            return self._send(421, {"error": "unexpected host"})
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        path = parsed.path

        # Page and script: the token is in the address (a script tag cannot
        # set a header).
        if path in ("/", "/index.html"):
            if not self._token_ok((query.get("t") or [""])[0]):
                return self._send(403, {"error": "missing or wrong token"})
            return self._send(200, _read_page(self.server.base_dir),
                              "text/html")
        if path == "/lib.js":
            if not self._token_ok((query.get("t") or [""])[0]):
                return self._send(403, {"error": "missing or wrong token"})
            library = ensure_library(self.server.base_dir)
            if not library:
                return self._send(404, {"error": "library unavailable"})
            with open(library, "rb") as handle:
                return self._send(200, handle.read(), "application/javascript")

        if not path.startswith("/api/"):
            return self._send(404, {"error": "not found"})
        if not self._token_ok(self.headers.get("X-Brain-Token")):
            return self._send(403, {"error": "missing or wrong token"})
        try:
            if path == "/api/graph":
                return self._send(200, self.server.graph())
            if path == "/api/version":
                return self._send(200, {"version": brain_map.fetch_version(
                    self.server.store_provider())})
            if path == "/api/node":
                key = (query.get("key") or [""])[0]
                if not key:
                    return self._send(400, {"error": "key required"})
                return self._send(200, brain_map.node_detail(
                    self.server.store_provider(), key))
            if path == "/api/search":
                return self._send(200, {"results": brain_map.search(
                    self.server.store_provider(), (query.get("q") or [""])[0])})
        except ConnectionError as exc:
            return self._send(503, {"error": str(exc)})
        except Exception as exc:
            return self._send(500, {"error": f"{type(exc).__name__}: {exc}"})
        return self._send(404, {"error": "not found"})

    def do_POST(self):
        self._send(405, {"error": "read-only"})

    do_PUT = do_DELETE = do_PATCH = do_POST


# ------------------------------------------------------------------ #
# Assets
# ------------------------------------------------------------------ #
def _read_page(base_dir):
    path = os.path.join(base_dir, "web", "brain_map.html")
    with open(path, "rb") as handle:
        return handle.read()


def ensure_library(base_dir):
    """The 3D rendering library, cached locally after the first download.

    Served from this machine thereafter so the map works offline and so the
    page never loads executable code from a third party at view time.
    """
    target = os.path.join(base_dir, "vault", "web", "3d-force-graph.min.js")
    if os.path.exists(target) and os.path.getsize(target) > 100_000:
        return target
    try:
        import requests
    except Exception:
        return None
    os.makedirs(os.path.dirname(target), exist_ok=True)
    for url in LIB_URLS:
        try:
            response = requests.get(url, timeout=25)
            if response.status_code == 200 and len(response.content) > 100_000 \
                    and LIB_VERSION.split("@")[1].encode() in response.content[:80]:
                with open(target, "wb") as handle:
                    handle.write(response.content)
                return target
        except Exception as exc:
            print(f"[BrainMap] Could not fetch {url}: {type(exc).__name__}")
    return None


# ------------------------------------------------------------------ #
# Lifecycle
# ------------------------------------------------------------------ #
def _default_store():
    from memory.neo4j_store import store
    return store


def ensure_running(base_dir, store_provider=None):
    """Start the server if it is not already up. Returns an info dict."""
    with _lock:
        server = _state["server"]
        if server is None:
            server = BrainServer(base_dir, store_provider or _default_store)
            thread = threading.Thread(target=server.serve_forever, daemon=True,
                                      name="vanta-brain-map")
            thread.start()
            _state.update(server=server, thread=thread)
        info = {"port": server.port, "token": server.token,
                "url": f"http://127.0.0.1:{server.port}/?t={server.token}"}
    # Prove it answers, from the outside, and learn the size. The receipt
    # claims only what this check observed.
    try:
        graph = server.graph()
        info["nodes"] = graph["counts"]["nodes"]
        info["edges"] = graph["counts"]["links"]
    except Exception as exc:
        info["nodes"] = None
        info["error"] = str(exc)
    info["library"] = bool(ensure_library(base_dir))
    return info


def open_in_browser(info, focus=""):
    import webbrowser
    address = info["url"] + (f"#focus={quote(str(focus))}" if focus else "")
    webbrowser.open(address)
    return f"http://127.0.0.1:{info['port']}/"


def shutdown():
    with _lock:
        server = _state["server"]
        if server is not None:
            server.shutdown()
            server.server_close()
            _state.update(server=None, thread=None)
