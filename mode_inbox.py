# -*- coding: utf-8 -*-
"""Durable interruption inbox for Sleep and Study modes."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import threading
import uuid

_LOCK = threading.RLock()
_ACTIVE_MODES = frozenset({"sleep", "study"})
_MAX_ITEMS = 500


def _path(base_dir):
    return os.path.join(base_dir, "vault", "system", "mode_inbox.json")


def _load(base_dir):
    try:
        with open(_path(base_dir), "r", encoding="utf-8") as handle:
            data = json.load(handle)
            return data if isinstance(data, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def _save(base_dir, data):
    path = _path(base_dir)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temporary = path + ".tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
    os.replace(temporary, path)


def begin(base_dir, mode):
    mode = str(mode or "").lower()
    if mode not in _ACTIVE_MODES:
        return
    with _LOCK:
        data = _load(base_dir)
        if data.get("active") and data.get("mode") == mode:
            return
        existing = list(data.get("items") or [])
        _save(base_dir, {
            "version": 1,
            "session_id": uuid.uuid4().hex,
            "mode": mode,
            "active": True,
            "started_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "items": existing[-_MAX_ITEMS:],
        })


def record(base_dir, kind, title, body="", source_id="", priority="normal",
           reason=""):
    kind = str(kind or "notification").lower()
    title = " ".join(str(title or "").split())
    body = " ".join(str(body or "").split())
    if not title and not body:
        return False
    with _LOCK:
        data = _load(base_dir)
        if not data.get("active") or data.get("mode") not in _ACTIVE_MODES:
            return False
        key_material = source_id or f"{kind}|{title}|{body}"
        item_id = hashlib.sha256(key_material.encode("utf-8")).hexdigest()[:24]
        items = list(data.get("items") or [])
        if any(item.get("id") == item_id for item in items):
            return False
        priority = str(priority or "normal").lower()
        if priority not in {"low", "normal", "important", "critical"}:
            priority = "normal"
        items.append({
            "id": item_id,
            "kind": kind,
            "title": title[:300],
            "body": body[:800],
            "source_id": str(source_id or "")[:200],
            "priority": priority,
            "priority_reason": " ".join(str(reason or "").split())[:300],
            "recorded_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        })
        data["items"] = items[-_MAX_ITEMS:]
        _save(base_dir, data)
        return True


def active_mode(base_dir):
    with _LOCK:
        data = _load(base_dir)
        return data.get("mode") if data.get("active") else ""


def drain(base_dir, mode=""):
    empty = {"count": 0, "emails": [], "notifications": [],
             "dashboard": "", "spoken": ""}
    with _LOCK:
        data = _load(base_dir)
        if not data.get("active"):
            return empty
        if mode and data.get("mode") != str(mode).lower():
            return empty
        items = list(data.get("items") or [])
        data["active"] = False
        data["delivered_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
        data["items"] = []
        _save(base_dir, data)

    emails = [item for item in items if item.get("kind") == "email"]
    notifications = [item for item in items if item.get("kind") != "email"]
    if not items:
        return {"count": 0, "emails": [], "notifications": [],
                "dashboard": "Nothing arrived while this mode was active.",
                "spoken": "Nothing new arrived while you were away."}

    lines = [f"While {data.get('mode')} mode was active:"]
    if emails:
        lines.append(f"Emails ({len(emails)}):")
        lines.extend(
            f"- {item['title']}"
            + (f" [{item.get('priority', 'normal').upper()}]"
               if item.get('priority') in {'important', 'critical'} else "")
            for item in emails)
    if notifications:
        lines.append(f"Other notifications ({len(notifications)}):")
        lines.extend(
            f"- {item['title']}" + (f": {item['body']}" if item.get("body") else "")
            for item in notifications)

    spoken_parts = [f"While you were away, {len(emails)} new email"
                    f"{'s' if len(emails) != 1 else ''} arrived."]
    spoken_parts.extend(item["title"] for item in emails)
    if notifications:
        spoken_parts.append(
            f"There {'were' if len(notifications) != 1 else 'was'} "
            f"{len(notifications)} other notification"
            f"{'s' if len(notifications) != 1 else ''}; "
            "the details are on the dashboard.")
    return {
        "count": len(items), "emails": emails, "notifications": notifications,
        "dashboard": "\n".join(lines), "spoken": " ".join(spoken_parts),
    }
