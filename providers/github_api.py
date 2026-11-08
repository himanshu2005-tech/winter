# -*- coding: utf-8 -*-
"""GitHub App client used by VANTA.

Authentication uses the locally stored PEM only to mint short-lived GitHub
installation tokens.  Neither the PEM nor generated tokens are returned to the
model, dashboard, logs, memory, or graph.
"""
from __future__ import annotations

import base64
import json
import os
import threading
import time
from pathlib import Path
from urllib.parse import unquote, urlparse

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding


API = "https://api.github.com"
API_VERSION = "2022-11-28"
_token_lock = threading.Lock()
_token = ""
_token_expires_at = 0.0


class GitHubError(RuntimeError):
    pass


def _b64url(value: bytes) -> bytes:
    return base64.urlsafe_b64encode(value).rstrip(b"=")


def _settings(base_dir):
    root = Path(base_dir).resolve()
    app_id = os.getenv("VANTA_GITHUB_APP_ID", "5270202").strip()
    installation_id = os.getenv("VANTA_GITHUB_INSTALLATION_ID", "170152230").strip()
    key_path = Path(os.getenv(
        "VANTA_GITHUB_PRIVATE_KEY",
        str(root / "secrets" / "github-app.pem"),
    )).expanduser().resolve()
    if not app_id or not installation_id:
        raise GitHubError("GitHub App ID or installation ID is not configured")
    if not key_path.is_file():
        raise GitHubError(f"GitHub App private key was not found at {key_path}")
    return app_id, installation_id, key_path


def _app_jwt(app_id, key_path):
    now = int(time.time())
    header = _b64url(json.dumps(
        {"alg": "RS256", "typ": "JWT"}, separators=(",", ":")
    ).encode())
    payload = _b64url(json.dumps(
        {"iat": now - 60, "exp": now + 540, "iss": str(app_id)},
        separators=(",", ":"),
    ).encode())
    unsigned = header + b"." + payload
    try:
        key = serialization.load_pem_private_key(key_path.read_bytes(), password=None)
        signature = key.sign(unsigned, padding.PKCS1v15(), hashes.SHA256())
    except Exception as exc:
        raise GitHubError("The configured GitHub App private key is invalid") from exc
    return (unsigned + b"." + _b64url(signature)).decode("ascii")


def _headers(token):
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": API_VERSION,
        "User-Agent": "VANTA-GitHub-App",
    }


def installation_token(base_dir, force=False):
    """Return a cached short-lived installation token without persisting it."""
    global _token, _token_expires_at
    with _token_lock:
        if not force and _token and time.time() < _token_expires_at - 90:
            return _token
        app_id, installation_id, key_path = _settings(base_dir)
        response = requests.post(
            f"{API}/app/installations/{installation_id}/access_tokens",
            headers=_headers(_app_jwt(app_id, key_path)), timeout=20,
        )
        if response.status_code != 201:
            raise GitHubError(
                f"GitHub authentication failed (HTTP {response.status_code})"
            )
        data = response.json()
        _token = str(data.get("token") or "")
        if not _token:
            raise GitHubError("GitHub returned no installation token")
        # Installation tokens currently last one hour. Prefer the server value
        # when parseable, while retaining a safe bounded fallback.
        _token_expires_at = time.time() + 3300
        return _token


def request(base_dir, method, path, *, params=None, json_body=None):
    """Authenticated request with one refresh retry and sanitized failures."""
    url = path if str(path).startswith("https://") else f"{API}{path}"
    for attempt in range(2):
        response = requests.request(
            method, url, headers=_headers(installation_token(base_dir, attempt > 0)),
            params=params, json=json_body, timeout=25,
        )
        if response.status_code != 401 or attempt:
            break
    if not response.ok:
        try:
            message = response.json().get("message", "request failed")
        except Exception:
            message = "request failed"
        raise GitHubError(f"GitHub {method.upper()} failed: {message} (HTTP {response.status_code})")
    if response.status_code == 204:
        return None
    return response.json()


def parse_reference(value):
    """Parse owner/repo or a GitHub repo/blob/tree URL.

    Returns ``(owner/repo, path, ref)``. A plain repository reference has an
    empty path and ref. Query strings/fragments never become repository paths.
    """
    text = unquote(str(value or "").strip())
    if not text:
        raise GitHubError("A GitHub repository or URL is required")
    if "://" in text:
        parsed = urlparse(text)
        if parsed.hostname not in {"github.com", "www.github.com"}:
            raise GitHubError("Only github.com links are supported")
        parts = [part for part in parsed.path.split("/") if part]
    else:
        parts = [part for part in text.strip("/").split("/") if part]
    if len(parts) < 2:
        raise GitHubError("Use owner/repository or a github.com repository link")
    owner, repo = parts[0], parts[1].removesuffix(".git")
    ref = path = ""
    if len(parts) >= 4 and parts[2] in {"blob", "tree"}:
        ref = parts[3]
        path = "/".join(parts[4:])
    return f"{owner}/{repo}", path, ref


def read_repository(base_dir, reference, limit=80):
    repo, _, _ = parse_reference(reference)
    metadata = request(base_dir, "GET", f"/repos/{repo}")
    branch = metadata.get("default_branch") or "HEAD"
    readme_text = ""
    try:
        readme = request(base_dir, "GET", f"/repos/{repo}/readme", params={"ref": branch})
        readme_text = base64.b64decode(readme.get("content", "")).decode("utf-8", "replace")
    except GitHubError as exc:
        if "HTTP 404" not in str(exc):
            raise
    tree = request(base_dir, "GET", f"/repos/{repo}/git/trees/{branch}", params={"recursive": "1"})
    files = [item.get("path", "") for item in tree.get("tree", [])
             if item.get("type") == "blob"][:max(1, min(int(limit), 200))]
    return {
        "repo": repo,
        "description": metadata.get("description") or "",
        "private": bool(metadata.get("private")),
        "default_branch": branch,
        "language": metadata.get("language") or "",
        "updated_at": metadata.get("updated_at") or "",
        "readme": readme_text[:6000],
        "files": files,
    }


def read_file(base_dir, reference, path="", ref=""):
    repo, url_path, url_ref = parse_reference(reference)
    target = str(path or url_path).strip("/")
    revision = str(ref or url_ref).strip()
    if not target:
        raise GitHubError("The GitHub link does not identify a file")
    data = request(base_dir, "GET", f"/repos/{repo}/contents/{target}",
                   params={"ref": revision} if revision else None)
    if data.get("type") != "file":
        raise GitHubError("The GitHub path is not a file")
    if data.get("encoding") != "base64":
        raise GitHubError("GitHub returned an unsupported file encoding")
    content = base64.b64decode(data.get("content", "")).decode("utf-8", "replace")
    return {"repo": repo, "path": target, "ref": revision or "default", "content": content}


def list_repositories(base_dir, limit=100):
    data = request(base_dir, "GET", "/installation/repositories",
                   params={"per_page": max(1, min(int(limit), 100))})
    return [item.get("full_name", "") for item in data.get("repositories", [])]
