"""Supported Sign in with ChatGPT flow for VANTA.

Credentials live in Windows Credential Manager through the existing keyring
adapter. The non-secret, per-installation host id lives under vault/system.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import threading
import time
import uuid
import webbrowser
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

import requests
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives import hashes

from security.keyring_manager import (
    delete_secret_chunked, get_secret_chunked, set_secret_chunked,
)

ISSUER = "https://auth.openai.com"
RESOURCE = "https://api.openai.com/v1"
AUTHORIZE_URL = f"{ISSUER}/api/accounts/authorize"
TOKEN_URL = f"{ISSUER}/api/accounts/oauth/token"
SCOPES = "openid profile email offline_access resource.invoke chatgpt.tokens.use.direct"
SERVICE = "VANTA.ChatGPT"
PROFILE_KEY = "active_profile"
APP_NAME = "VANTA"
_LOCK = threading.RLock()
_JWKS_CACHE = {"expires": 0.0, "keys": []}
_MODEL_CACHE = {"expires": 0.0, "models": [], "subject": ""}


class ChatGPTAuthError(RuntimeError):
    pass


def _b64url_decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _json_part(value: str) -> dict:
    return json.loads(_b64url_decode(value).decode("utf-8"))


def _host_path(base_dir) -> Path:
    return Path(base_dir) / "vault" / "system" / "chatgpt_host.json"


def host_id(base_dir) -> str:
    path = _host_path(base_dir)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        value = str(data.get("ext_agent_host_id", ""))
        if value.startswith("urn:uuid:"):
            uuid.UUID(value.removeprefix("urn:uuid:"))
            return value
    except Exception:
        pass
    value = f"urn:uuid:{uuid.uuid4()}"
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps({"ext_agent_host_id": value}, indent=2), encoding="utf-8")
    os.replace(temp, path)
    return value


def load_profile() -> dict | None:
    try:
        raw = get_secret_chunked(SERVICE, PROFILE_KEY)
        data = json.loads(raw) if raw else None
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _save_profile(profile: dict) -> None:
    set_secret_chunked(SERVICE, PROFILE_KEY, json.dumps(profile, separators=(",", ":")))


def clear_tokens(*, keep_registration=True) -> None:
    profile = load_profile()
    if keep_registration and profile and profile.get("client_id"):
        kept = {key: profile.get(key) for key in (
            "client_id", "email", "subject", "issuer", "ext_agent_host_id")
            if profile.get(key)}
        _save_profile(kept)
    else:
        delete_secret_chunked(SERVICE, PROFILE_KEY)


def _jwks() -> list:
    with _LOCK:
        if _JWKS_CACHE["keys"] and time.time() < _JWKS_CACHE["expires"]:
            return _JWKS_CACHE["keys"]
        discovery = requests.get(
            f"{ISSUER}/.well-known/openid-configuration", timeout=15)
        discovery.raise_for_status()
        config = discovery.json()
        if config.get("issuer") != ISSUER:
            raise ChatGPTAuthError("OpenAI identity issuer did not match.")
        response = requests.get(config["jwks_uri"], timeout=15)
        response.raise_for_status()
        keys = response.json().get("keys", [])
        _JWKS_CACHE.update(keys=keys, expires=time.time() + 3600)
        return keys


def validate_id_token(token: str, client_id: str, nonce: str) -> dict:
    try:
        encoded_header, encoded_payload, encoded_signature = token.split(".")
        header, claims = _json_part(encoded_header), _json_part(encoded_payload)
    except Exception as exc:
        raise ChatGPTAuthError("OpenAI returned a malformed identity token.") from exc
    if header.get("alg") != "RS256":
        raise ChatGPTAuthError("OpenAI identity token used an unsupported algorithm.")
    key = next((item for item in _jwks() if item.get("kid") == header.get("kid")), None)
    if not key:
        # Key rotation can happen between cache fills.
        with _LOCK:
            _JWKS_CACHE["expires"] = 0
        key = next((item for item in _jwks() if item.get("kid") == header.get("kid")), None)
    if not key:
        raise ChatGPTAuthError("OpenAI identity signing key was unavailable.")
    public_key = rsa.RSAPublicNumbers(
        int.from_bytes(_b64url_decode(key["e"]), "big"),
        int.from_bytes(_b64url_decode(key["n"]), "big"),
    ).public_key()
    try:
        public_key.verify(
            _b64url_decode(encoded_signature),
            f"{encoded_header}.{encoded_payload}".encode("ascii"),
            padding.PKCS1v15(), hashes.SHA256())
    except Exception as exc:
        raise ChatGPTAuthError("OpenAI identity token signature was invalid.") from exc
    audience = claims.get("aud", [])
    if isinstance(audience, str):
        audience = [audience]
    now = int(time.time())
    if claims.get("iss") != ISSUER or client_id not in audience:
        raise ChatGPTAuthError("OpenAI identity token was issued for another client.")
    if int(claims.get("exp", 0)) <= now or int(claims.get("iat", now + 1)) > now + 60:
        raise ChatGPTAuthError("OpenAI identity token was expired or not yet valid.")
    if not secrets.compare_digest(str(claims.get("nonce", "")), nonce):
        raise ChatGPTAuthError("OpenAI identity nonce did not match.")
    if not claims.get("sub"):
        raise ChatGPTAuthError("OpenAI identity token had no subject.")
    return claims


def _token_request(data: dict) -> dict:
    response = requests.post(TOKEN_URL, data=data, timeout=30)
    try:
        payload = response.json()
    except Exception:
        payload = {}
    if not response.ok:
        code = payload.get("error") or payload.get("code") or response.status_code
        detail = payload.get("error_description") or payload.get("detail") or "token exchange failed"
        raise ChatGPTAuthError(f"OpenAI authentication failed ({code}): {detail}")
    return payload


def _callback_server():
    result, arrived = {}, threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            parsed = urlparse(self.path)
            if parsed.path != "/auth/callback":
                self.send_error(404)
                return
            result.update({k: v[0] for k, v in parse_qs(parsed.query).items() if v})
            body = ("<html><body style='font-family:Segoe UI;background:#111;color:#eee;"
                    "display:grid;place-items:center;height:100vh'><div><h2>Authorization received.</h2>"
                    "<p>VANTA is verifying the connection. You can close this tab.</p></div></body></html>")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body.encode("utf-8"))))
            self.end_headers()
            self.wfile.write(body.encode("utf-8"))
            arrived.set()

        def log_message(self, *_args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.timeout = 1
    redirect = f"http://127.0.0.1:{server.server_port}/auth/callback"
    return server, redirect, result, arrived


def authenticate(base_dir, timeout=180) -> str:
    """Open the official browser consent flow and store a validated profile."""
    with _LOCK:
        existing = load_profile() or {}
        verifier = secrets.token_urlsafe(64)
        challenge = base64.urlsafe_b64encode(
            hashlib.sha256(verifier.encode("ascii")).digest()).decode("ascii").rstrip("=")
        state, nonce = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        server, redirect, callback, arrived = _callback_server()
        client_id = existing.get("client_id") or "dynamic_agent_client"
        params = {
            "client_id": client_id, "ext_agent_host_id": host_id(base_dir),
            "response_type": "code", "redirect_uri": redirect,
            "scope": SCOPES, "resource": RESOURCE, "state": state,
            "nonce": nonce, "code_challenge_method": "S256",
            "code_challenge": challenge,
        }
        if client_id == "dynamic_agent_client":
            params["agent_name_hint"] = APP_NAME
        else:
            if existing.get("id_token"):
                params["id_token_hint"] = existing["id_token"]
            elif existing.get("email"):
                params["login_hint"] = existing["email"]
        webbrowser.open(f"{AUTHORIZE_URL}?{urlencode(params)}", new=2)
        deadline = time.time() + timeout
        try:
            while not arrived.is_set() and time.time() < deadline:
                server.handle_request()
        finally:
            server.server_close()
        if not arrived.is_set():
            raise ChatGPTAuthError("ChatGPT authentication timed out. Please try again.")
        if not secrets.compare_digest(callback.get("state", ""), state):
            raise ChatGPTAuthError("ChatGPT authentication state did not match.")
        if callback.get("error"):
            raise ChatGPTAuthError(f"ChatGPT authorization was not completed: {callback['error']}")
        issued = callback.get("client_id") or (client_id if client_id != "dynamic_agent_client" else "")
        if not issued or (client_id != "dynamic_agent_client" and issued != client_id):
            raise ChatGPTAuthError("ChatGPT did not return the expected client registration.")
        tokens = _token_request({
            "grant_type": "authorization_code", "client_id": issued,
            "code": callback.get("code", ""), "code_verifier": verifier,
            "redirect_uri": redirect, "resource": RESOURCE,
        })
        claims = validate_id_token(tokens.get("id_token", ""), issued, nonce)
        if existing.get("subject") and existing.get("subject") != claims.get("sub"):
            raise ChatGPTAuthError("The returned ChatGPT account did not match the saved registration.")
        scopes = str(tokens.get("scope", "")).split()
        if "chatgpt.tokens.use.direct" not in scopes:
            raise ChatGPTAuthError("ChatGPT plan usage was not approved for VANTA.")
        saved_at = time.time()
        profile = {
            "email": claims.get("email", ""), "issuer": claims.get("iss"),
            "subject": claims.get("sub"), "client_id": issued,
            "ext_agent_host_id": host_id(base_dir), "id_token": tokens.get("id_token"),
            "access_token": tokens.get("access_token"),
            "refresh_token": tokens.get("refresh_token"),
            "token_type": tokens.get("token_type", "Bearer"),
            "expires_in": int(tokens.get("expires_in", 3600)),
            "earliest_refresh_at": tokens.get("earliest_refresh_at"),
            "scopes": scopes, "saved_at": saved_at,
            "saved_at_iso": datetime.now(timezone.utc).isoformat(),
        }
        _save_profile(profile)
        return f"ChatGPT is connected{(' as ' + profile['email']) if profile['email'] else ''}."


def _refresh(profile: dict) -> dict:
    earliest = profile.get("earliest_refresh_at")
    if earliest:
        try:
            wait_until = float(earliest)
            if wait_until > time.time():
                raise ChatGPTAuthError("ChatGPT asked VANTA to wait before refreshing.")
        except (TypeError, ValueError):
            pass
    tokens = _token_request({
        "grant_type": "refresh_token", "client_id": profile["client_id"],
        "refresh_token": profile["refresh_token"], "resource": RESOURCE,
    })
    scopes = str(tokens.get("scope") or " ".join(profile.get("scopes", []))).split()
    if "chatgpt.tokens.use.direct" not in scopes:
        raise ChatGPTAuthError("ChatGPT plan permission is no longer available.")
    updated = dict(profile)
    updated.update({
        "access_token": tokens.get("access_token"),
        "refresh_token": tokens.get("refresh_token"),
        "id_token": tokens.get("id_token") or profile.get("id_token"),
        "token_type": tokens.get("token_type", "Bearer"),
        "expires_in": int(tokens.get("expires_in", 3600)),
        "earliest_refresh_at": tokens.get("earliest_refresh_at"),
        "scopes": scopes, "saved_at": time.time(),
        "saved_at_iso": datetime.now(timezone.utc).isoformat(),
    })
    _save_profile(updated)
    return updated


def access_token(*, refresh=True) -> str | None:
    with _LOCK:
        profile = load_profile()
        if not profile or "chatgpt.tokens.use.direct" not in profile.get("scopes", []):
            return None
        expires_at = float(profile.get("saved_at", 0)) + int(profile.get("expires_in", 0))
        if expires_at > time.time() + 120:
            return profile.get("access_token")
        if not refresh or not profile.get("refresh_token") or not profile.get("client_id"):
            return None
        try:
            return _refresh(profile).get("access_token")
        except ChatGPTAuthError as exc:
            text = str(exc).lower()
            if any(code in text for code in (
                    "invalid_grant", "invalid_refresh_token", "token_expired",
                    "refresh_token_expired", "refresh_token_invalidated", "refresh_token_reused")):
                clear_tokens(keep_registration=True)
            raise


def status(*, refresh=True) -> dict:
    profile = load_profile() or {}
    try:
        token = access_token(refresh=refresh)
    except Exception as exc:
        return {"available": False, "reason": str(exc), "email": profile.get("email", "")}
    return {
        "available": bool(token),
        "reason": "connected" if token else "authentication_required",
        "email": profile.get("email", ""),
    }


def list_models() -> list[dict]:
    profile = load_profile() or {}
    subject = str(profile.get("subject", ""))
    with _LOCK:
        if (_MODEL_CACHE["models"] and _MODEL_CACHE["subject"] == subject
                and time.time() < _MODEL_CACHE["expires"]):
            return list(_MODEL_CACHE["models"])
    token = access_token(refresh=True)
    if not token:
        return []
    response = requests.get(
        f"{RESOURCE}/models", headers={"Authorization": f"Bearer {token}"}, timeout=20)
    response.raise_for_status()
    payload = response.json()
    models = [
        {"slug": row.get("slug"), "display_name": row.get("display_name")}
        for row in payload.get("models", [])
        if row.get("visibility") == "list" and row.get("slug")
    ]
    with _LOCK:
        _MODEL_CACHE.update(models=models, subject=subject, expires=time.time() + 3600)
    return models


def preferred_model() -> str:
    configured = os.getenv("CHATGPT_MODEL", "").strip()
    models = list_models()
    if not models:
        raise ChatGPTAuthError("ChatGPT returned no usable models for this account.")
    available = [row["slug"] for row in models]
    excluded = {
        item.strip().casefold() for item in
        os.getenv("CHATGPT_EXCLUDED_MODELS", "gpt-6-astra").split(",")
        if item.strip()
    }
    if configured:
        if configured.casefold() in excluded:
            raise ChatGPTAuthError(
                f"Configured ChatGPT model {configured!r} is explicitly excluded.")
        if configured not in available:
            raise ChatGPTAuthError(
                f"Configured ChatGPT model {configured!r} is not available to this account.")
        return configured

    # Prefer a stronger account-supported model without trusting catalog order.
    # An explicit CHATGPT_MODEL remains authoritative and is validated above.
    preference = ("gpt-6.1-sol", "gpt-6-sol", "gpt-5.6-sol",
                  "gpt-6.1-terra", "gpt-5.6-terra")
    for candidate in preference:
        if candidate in available and candidate.casefold() not in excluded:
            return candidate
    for candidate in available:
        if candidate.casefold() not in excluded:
            return candidate
    raise ChatGPTAuthError(
        "Every ChatGPT model available to this account is excluded; using AIRouter instead.")
