"""Authenticated phone/PWA gateway for the running VANTA process.

The gateway is deliberately a transport adapter, not another assistant.  It
feeds the existing ``input_queue`` and mirrors selected ``ui_queue`` events so
the desktop, voice and phone all share one conversation and one action policy.

It binds to loopback by default.  For an iPhone, expose that loopback listener
with Tailscale Serve so the browser still receives HTTPS; do not open this port
directly to the public internet.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import mimetypes
import os
import queue
import re
import secrets
import threading
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


GATEWAY_VERSION = "1.0"
DEFAULT_PORT = 8765
MAX_MESSAGE_CHARS = 8_000
MAX_UPLOAD_BYTES = 50 * 1024 * 1024
MAX_VOICE_BYTES = 15 * 1024 * 1024
SESSION_DAYS = 30

_base_dir: Path | None = None
_input_queue = None
_thread: threading.Thread | None = None
_loop: asyncio.AbstractEventLoop | None = None
_clients: set[Any] = set()
_clients_lock: asyncio.Lock | None = None
_events: deque[dict[str, Any]] = deque(maxlen=500)
_event_lock = threading.Lock()
_event_id = 0
_pair_attempts: dict[str, list[float]] = {}
_pair_lock = threading.Lock()
_downloads: dict[str, dict[str, Any]] = {}
_downloads_lock = threading.Lock()


VISIBLE_UI_EVENTS = {
    "agent_response", "user_query", "info", "artifacts", "show_result",
    "rich_result", "dashboard_document", "approval_request",
    "approval_settled", "mode_changed", "project_changed", "image_artifact",
    "idle", "listening", "thinking", "speaking", "working", "error",
}


class EventQueue(queue.Queue):
    """Normal Queue with a non-consuming observer for the phone channel."""

    def put(self, item, block=True, timeout=None):
        try:
            publish_ui_event(item)
        except Exception as exc:
            print(f"[Mobile] Could not mirror UI event: {type(exc).__name__}.")
        return super().put(item, block=block, timeout=timeout)


def configure(base_dir, input_queue):
    global _base_dir, _input_queue
    _base_dir = Path(base_dir).resolve()
    _input_queue = input_queue
    (_base_dir / "vault" / "mobile" / "inbox").mkdir(
        parents=True, exist_ok=True)


def enabled() -> bool:
    return os.getenv("VANTA_MOBILE_ENABLED", "true").strip().casefold() not in {
        "0", "false", "no", "off"
    }


def _session_path() -> Path:
    if _base_dir is None:
        raise RuntimeError("mobile gateway is not configured")
    return _base_dir / "vault" / "mobile" / "sessions.json"


def _atomic_json(path: Path, value: Any):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2),
                         encoding="utf-8")
    os.replace(temporary, path)


def _load_sessions() -> list[dict[str, Any]]:
    try:
        rows = json.loads(_session_path().read_text(encoding="utf-8"))
        return rows if isinstance(rows, list) else []
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return []


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _new_session(device_name: str) -> tuple[str, dict[str, Any]]:
    token = secrets.token_urlsafe(48)
    now = _utc_now()
    record = {
        "id": secrets.token_hex(12),
        "device_name": str(device_name or "Himanshu's iPhone")[:80],
        "token_hash": _token_hash(token),
        "created_at": now.isoformat(),
        "expires_at": (now + timedelta(days=SESSION_DAYS)).isoformat(),
        "last_seen_at": now.isoformat(),
        "revoked": False,
    }
    rows = [row for row in _load_sessions()
            if not row.get("revoked") and row.get("expires_at", "") > now.isoformat()]
    rows.append(record)
    _atomic_json(_session_path(), rows[-12:])
    return token, record


def _valid_token(token: str) -> dict[str, Any] | None:
    if not token:
        return None
    digest = _token_hash(token)
    now = _utc_now().isoformat()
    for row in _load_sessions():
        if (not row.get("revoked") and row.get("expires_at", "") > now
                and hmac.compare_digest(str(row.get("token_hash", "")), digest)):
            return row
    return None


def _clean(value: Any, depth=0) -> Any:
    """Bound event payloads and remove fields that are never phone content."""
    if depth > 5:
        return "[nested content omitted]"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        text = value.replace("\x00", "")
        # Environment-style secrets occasionally appear in diagnostic errors.
        text = re.sub(
            r"(?i)(api[_-]?key|token|password|secret)\s*[:=]\s*[^\s,;]+",
            r"\1=[redacted]", text)
        return text[:50_000]
    if isinstance(value, dict):
        blocked = {"token", "password", "secret", "api_key", "authorization"}
        return {str(key)[:100]: _clean(item, depth + 1)
                for key, item in list(value.items())[:100]
                if str(key).casefold() not in blocked}
    if isinstance(value, (list, tuple)):
        return [_clean(item, depth + 1) for item in list(value)[:100]]
    return str(value)[:2_000]


def _record_event(kind: str, payload: Any) -> dict[str, Any]:
    global _event_id
    with _event_lock:
        _event_id += 1
        event = {
            "id": _event_id,
            "at": _utc_now().isoformat(),
            "kind": str(kind),
            "payload": _clean(payload),
        }
        _events.append(event)
    return event


async def _broadcast(event: dict[str, Any]):
    if not _clients:
        return
    dead = []
    message = json.dumps(event, ensure_ascii=False)
    for client in list(_clients):
        try:
            await client.send_str(message)
        except Exception:
            dead.append(client)
    for client in dead:
        _clients.discard(client)


def publish(kind: str, payload: Any):
    event = _record_event(kind, payload)
    loop = _loop
    if loop and loop.is_running():
        asyncio.run_coroutine_threadsafe(_broadcast(event), loop)
    return event


def publish_ui_event(item):
    if not isinstance(item, tuple) or not item:
        return
    kind = str(item[0])
    if kind not in VISIBLE_UI_EVENTS:
        return
    payload = item[1] if len(item) > 1 else None
    if kind == "user_query" and isinstance(payload, str):
        payload = re.sub(r"^\s*\[Mobile\]:\s*", "", payload, flags=re.I)
    if kind == "image_artifact":
        path = payload.get("path") if isinstance(payload, dict) else payload
        if path and Path(str(path)).is_file():
            offer_file(path, title=(payload.get("title") if isinstance(payload, dict)
                                    else "VANTA file"))
            return
    publish(kind, payload)


def offer_file(path, title="VANTA file", expires_minutes=30):
    """Offer an existing local file to authenticated mobile clients."""
    resolved = Path(path).resolve()
    if not resolved.is_file():
        raise FileNotFoundError(resolved)
    token = secrets.token_urlsafe(24)
    expires = time.monotonic() + max(1, int(expires_minutes)) * 60
    with _downloads_lock:
        now = time.monotonic()
        for old_token, row in list(_downloads.items()):
            if row["expires"] <= now:
                _downloads.pop(old_token, None)
        _downloads[token] = {"path": resolved, "expires": expires}
    return publish("attachment", {
        "title": str(title or resolved.name)[:160],
        "name": resolved.name,
        "size": resolved.stat().st_size,
        "url": f"/api/downloads/{token}",
    })


def _allowed_origins() -> set[str]:
    configured = os.getenv("VANTA_MOBILE_ALLOWED_ORIGINS", "")
    values = {item.strip().rstrip("/") for item in configured.split(",")
              if item.strip()}
    values.update({
        "http://localhost:8081", "http://localhost:19006",
        "http://127.0.0.1:8081", "http://127.0.0.1:19006",
        "https://vanta-himanshu-2026.web.app",
        "https://vanta-himanshu-2026.firebaseapp.com",
    })
    return values


def _origin_allowed(origin: str) -> bool:
    if not origin:
        return True
    return origin.rstrip("/") in _allowed_origins()


def _bearer(request) -> str:
    value = request.headers.get("Authorization", "")
    if value.casefold().startswith("bearer "):
        return value[7:].strip()
    return ""


def _limited(remote: str) -> bool:
    now = time.monotonic()
    with _pair_lock:
        recent = [stamp for stamp in _pair_attempts.get(remote, [])
                  if now - stamp < 600]
        recent.append(now)
        _pair_attempts[remote] = recent
        return len(recent) > 5


def _safe_filename(name: str) -> str:
    value = Path(str(name or "mobile-file").replace("\\", "/")).name
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value).rstrip(". ")
    return (value or "mobile-file")[:180]


async def _create_app():
    from aiohttp import web

    @web.middleware
    async def cors_and_security(request, handler):
        origin = request.headers.get("Origin", "")
        if request.method == "OPTIONS":
            response = web.Response(status=204)
        elif origin and not _origin_allowed(origin):
            response = web.json_response({"error": "origin_not_allowed"}, status=403)
        else:
            response = await handler(request)
        if origin and _origin_allowed(origin):
            response.headers["Access-Control-Allow-Origin"] = origin
            response.headers["Vary"] = "Origin"
            response.headers["Access-Control-Allow-Headers"] = (
                "Authorization, Content-Type")
            response.headers["Access-Control-Allow-Methods"] = (
                "GET, POST, DELETE, OPTIONS")
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Cache-Control"] = "no-store"
        return response

    app = web.Application(middlewares=[cors_and_security],
                          client_max_size=MAX_UPLOAD_BYTES + 1024 * 1024)

    def authorized(request):
        return _valid_token(_bearer(request))

    async def health(_request):
        return web.json_response({
            "name": "VANTA", "gateway_version": GATEWAY_VERSION,
            "online": True, "pairing_required": True,
        })

    async def pair(request):
        remote = request.remote or "unknown"
        if _limited(remote):
            return web.json_response({"error": "too_many_attempts"}, status=429)
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "invalid_json"}, status=400)
        expected = os.getenv("AUTH_PASSWORD", "")
        supplied = str(body.get("password") or "")
        if not expected:
            return web.json_response({"error": "pairing_not_configured"}, status=503)
        if not hmac.compare_digest(expected.encode("utf-8"), supplied.encode("utf-8")):
            await asyncio.sleep(0.8)
            return web.json_response({"error": "invalid_password"}, status=401)
        token, record = _new_session(str(body.get("device_name") or "Himanshu's iPhone"))
        print(f"[Mobile] Paired device {record['device_name']} ({record['id']}).")
        return web.json_response({
            "token": token, "device_id": record["id"],
            "expires_at": record["expires_at"],
        })

    async def status(request):
        if not authorized(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        mode = "assistant"
        standby = False
        try:
            import mode_manager
            mode = mode_manager.current_mode().value
        except Exception:
            pass
        try:
            import runtime_power
            standby = runtime_power.is_standby()
        except Exception:
            pass
        return web.json_response({
            "online": True, "mode": mode, "standby": standby,
            "connected_clients": len(_clients),
        })

    async def history(request):
        if not authorized(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        try:
            after = int(request.query.get("after", "0"))
        except ValueError:
            after = 0
        with _event_lock:
            rows = [event for event in _events if event["id"] > after]
        return web.json_response({"events": rows[-250:]})

    async def messages(request):
        if not authorized(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "invalid_json"}, status=400)
        text = str(body.get("text") or "").strip()
        if not text or len(text) > MAX_MESSAGE_CHARS:
            return web.json_response({"error": "invalid_message"}, status=400)
        if _input_queue is None:
            return web.json_response({"error": "assistant_not_ready"}, status=503)
        _input_queue.put(f"[Mobile]: {text}")
        return web.json_response({"queued": True})

    async def approval(request):
        if not authorized(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        approval_id = str(request.match_info.get("approval_id") or "")[:120]
        try:
            body = await request.json()
        except Exception:
            body = {}
        decision = str(body.get("decision") or "").casefold()
        if decision not in {"approve", "reject"} or not approval_id:
            return web.json_response({"error": "invalid_decision"}, status=400)
        _input_queue.put(("approval", decision, approval_id))
        return web.json_response({"queued": True, "decision": decision})

    async def upload(request):
        if not authorized(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        reader = await request.multipart()
        field = await reader.next()
        if field is None or field.name != "file":
            return web.json_response({"error": "file_required"}, status=400)
        filename = _safe_filename(field.filename or "mobile-file")
        day = _utc_now().strftime("%Y-%m-%d")
        root = _base_dir / "vault" / "mobile" / "inbox" / day
        root.mkdir(parents=True, exist_ok=True)
        target = root / filename
        stem, suffix, number = target.stem, target.suffix, 2
        while target.exists() or target.with_suffix(target.suffix + ".part").exists():
            target = root / f"{stem}_{number}{suffix}"
            number += 1
        temporary = target.with_suffix(target.suffix + ".part")
        digest, size = hashlib.sha256(), 0
        try:
            with temporary.open("wb") as handle:
                while True:
                    chunk = await field.read_chunk(128 * 1024)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > MAX_UPLOAD_BYTES:
                        raise ValueError("file_too_large")
                    digest.update(chunk)
                    handle.write(chunk)
            os.replace(temporary, target)
        except Exception as exc:
            temporary.unlink(missing_ok=True)
            status_code = 413 if str(exc) == "file_too_large" else 500
            return web.json_response({"error": str(exc)}, status=status_code)
        mime = field.headers.get("Content-Type") or mimetypes.guess_type(target)[0]
        prompt = (
            f"[Mobile]: I sent you a file. Local path: {target.resolve()}. "
            f"Filename: {target.name}. MIME: {mime or 'application/octet-stream'}. "
            f"Size: {size} bytes. SHA-256: {digest.hexdigest()}.")
        _input_queue.put(prompt)
        publish("file_received", {"name": target.name, "size": size})
        return web.json_response({"received": True, "name": target.name,
                                  "size": size})

    async def voice(request):
        """Accept one user-initiated recording and transcribe in VANTA."""
        if not authorized(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        reader = await request.multipart()
        field = await reader.next()
        if field is None or field.name != "file":
            return web.json_response({"error": "file_required"}, status=400)
        suffix = Path(_safe_filename(field.filename or "voice.webm")).suffix
        if suffix.casefold() not in {".webm", ".m4a", ".mp4", ".wav", ".ogg", ".aac"}:
            suffix = ".webm"
        root = _base_dir / "vault" / "mobile" / "voice"
        root.mkdir(parents=True, exist_ok=True)
        target = root / f"voice-{secrets.token_hex(12)}{suffix}"
        size = 0
        try:
            with target.open("wb") as handle:
                while True:
                    chunk = await field.read_chunk(64 * 1024)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > MAX_VOICE_BYTES:
                        raise ValueError("voice_too_large")
                    handle.write(chunk)
        except Exception as exc:
            target.unlink(missing_ok=True)
            status_code = 413 if str(exc) == "voice_too_large" else 500
            return web.json_response({"error": str(exc)}, status=status_code)
        if size == 0:
            target.unlink(missing_ok=True)
            return web.json_response({"error": "empty_recording"}, status=400)
        _input_queue.put(("mobile_audio", str(target.resolve())))
        publish("voice_received", {"size": size})
        return web.json_response({"queued": True, "size": size})

    async def download(request):
        if not authorized(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        token = str(request.match_info.get("token") or "")
        with _downloads_lock:
            row = _downloads.get(token)
            if row and row["expires"] <= time.monotonic():
                _downloads.pop(token, None)
                row = None
        if not row or not row["path"].is_file():
            return web.json_response({"error": "file_unavailable"}, status=404)
        return web.FileResponse(
            row["path"],
            headers={"Content-Disposition": (
                f'attachment; filename="{_safe_filename(row["path"].name)}"')},
        )

    async def unlock(request):
        if not authorized(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        try:
            body = await request.json()
        except Exception:
            body = {}
        expected = os.getenv("AUTH_PASSWORD", "")
        supplied = str(body.get("password") or "")
        if not expected or not hmac.compare_digest(
                expected.encode("utf-8"), supplied.encode("utf-8")):
            return web.json_response({"error": "invalid_password"}, status=401)
        try:
            import authentication_gate
            authentication_gate.unlock("paired VANTA mobile app")
        except Exception:
            return web.json_response({"error": "unlock_unavailable"}, status=503)
        return web.json_response({"unlocked": True})

    async def events_socket(request):
        ws = web.WebSocketResponse(heartbeat=25, receive_timeout=45)
        await ws.prepare(request)
        authenticated = False
        try:
            message = await asyncio.wait_for(ws.receive(), timeout=8)
            if message.type == web.WSMsgType.TEXT:
                try:
                    payload = json.loads(message.data)
                except json.JSONDecodeError:
                    payload = {}
                authenticated = bool(_valid_token(str(payload.get("token") or "")))
            if not authenticated:
                await ws.send_json({"kind": "error", "payload": "unauthorized"})
                await ws.close(code=4001, message=b"unauthorized")
                return ws
            _clients.add(ws)
            await ws.send_json({"kind": "connected", "payload": {"online": True}})
            async for incoming in ws:
                if incoming.type == web.WSMsgType.TEXT:
                    try:
                        data = json.loads(incoming.data)
                    except json.JSONDecodeError:
                        continue
                    if data.get("type") == "ping":
                        await ws.send_json({"kind": "pong", "at": _utc_now().isoformat()})
        finally:
            _clients.discard(ws)
        return ws

    async def options(_request):
        return web.Response(status=204)

    app.router.add_get("/health", health)
    app.router.add_post("/api/pair", pair)
    app.router.add_get("/api/status", status)
    app.router.add_get("/api/events", history)
    app.router.add_get("/api/events/ws", events_socket)
    app.router.add_post("/api/messages", messages)
    app.router.add_post("/api/approvals/{approval_id}", approval)
    app.router.add_post("/api/files", upload)
    app.router.add_post("/api/voice", voice)
    app.router.add_get("/api/downloads/{token}", download)
    app.router.add_post("/api/unlock", unlock)
    app.router.add_route("OPTIONS", "/{tail:.*}", options)
    return app


def _serve():
    global _loop, _clients_lock
    from aiohttp import web
    _loop = asyncio.new_event_loop()
    asyncio.set_event_loop(_loop)
    _clients_lock = asyncio.Lock()
    app = _loop.run_until_complete(_create_app())
    host = os.getenv("VANTA_MOBILE_HOST", "127.0.0.1")
    port = int(os.getenv("VANTA_MOBILE_PORT", str(DEFAULT_PORT)))
    runner = web.AppRunner(app, access_log=None)
    _loop.run_until_complete(runner.setup())
    site = web.TCPSite(runner, host=host, port=port)
    _loop.run_until_complete(site.start())
    print(f"[Mobile] Gateway ready on {host}:{port}; use an HTTPS tunnel for iPhone access.")
    _loop.run_forever()


def start():
    global _thread
    if not enabled() or _thread and _thread.is_alive():
        return bool(_thread and _thread.is_alive())
    if _base_dir is None or _input_queue is None:
        raise RuntimeError("configure mobile gateway before starting it")
    _thread = threading.Thread(target=_serve, daemon=True,
                               name="vanta-mobile-gateway")
    _thread.start()
    return True
