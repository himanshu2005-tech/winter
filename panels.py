# -*- coding: utf-8 -*-
"""
panels.py -- Control Center panels that VANTA composes for itself.

The dashboard's built-in pages are fixed because they answer fixed questions:
what is this machine doing, what did I just say, what is in the vault. What
VANTA needs on top of that changes weekly -- an exam countdown in November, a
research progress board while a topic is being learned, a checklist during a
build. Hardcoding those would mean editing the UI every time, and letting the
model write Tk code would mean an unreviewable crash surface in the one window
that has to stay up.

So a panel is *data*: a title and a list of typed items, stored as JSON and
rendered by the Control Center. VANTA can create, edit and delete them freely
and they survive restarts. The bound on what it can do to the window is the
item vocabulary below, which is small enough to render safely and wide enough
that the model is not fighting it.

Everything here is defensive about shape. The specs arrive from a language
model, which means fields will sometimes be missing, doubled, or the wrong
type, and a dashboard that raises on a malformed panel takes the whole UI down
with it.
"""

import json
import os
import re
import time
import uuid

MAX_PANELS = 16
MAX_ITEMS = 24
MAX_TEXT = 600

# The rendering vocabulary. A kind not in here is dropped rather than guessed
# at, because a panel that renders something other than what the model asked
# for is worse than one that renders less.
ITEM_KINDS = ("stat", "progress", "text", "bullets", "kv", "button", "link",
              "divider", "note")

ACCENTS = {
    "blue": "#0a84ff", "green": "#30d158", "yellow": "#ffd60a",
    "orange": "#ff9f0a", "red": "#ff453a", "purple": "#bf5af2",
    "teal": "#64d2ff", "grey": "#8e8e93", "gray": "#8e8e93",
}


def _store_path(base_dir):
    return os.path.join(base_dir, "vault", "system", "panels.json")


def _clean(value, limit=MAX_TEXT):
    text = " ".join(str(value if value is not None else "").split())
    return text[:limit]


def _ratio(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number > 1.0:                 # accept percentages as well as fractions
        number /= 100.0
    return max(0.0, min(1.0, number))


def _split(value):
    """Split a comma or pipe list, tolerating either separator."""
    raw = str(value or "")
    parts = raw.split("|") if "|" in raw else raw.split(",")
    return [p.strip() for p in parts if p.strip()]


def normalise_item(item):
    """Coerce one item into something renderable, or return None."""
    if isinstance(item, str):
        item = {"kind": "text", "text": item}
    if not isinstance(item, dict):
        return None

    kind = str(item.get("kind") or item.get("type") or "text").lower().strip()
    if kind in ("bullet", "list", "points"):
        kind = "bullets"
    elif kind in ("metric", "value"):
        kind = "stat"
    elif kind in ("bar", "meter"):
        kind = "progress"
    elif kind in ("action", "command"):
        kind = "button"
    elif kind in ("rule", "separator", "hr"):
        kind = "divider"
    if kind not in ITEM_KINDS:
        return None

    clean = {"kind": kind}
    if kind == "divider":
        return clean

    if kind in ("stat", "progress"):
        clean["label"] = _clean(item.get("label") or item.get("name"), 60)
        clean["value"] = _clean(item.get("value"), 40)
        clean["detail"] = _clean(item.get("detail") or item.get("note"), 80)
        clean["ratio"] = _ratio(item.get("ratio", item.get("percent")))
        if kind == "progress" and clean["ratio"] is None:
            clean["ratio"] = 0.0
        if not clean["label"] and not clean["value"]:
            return None

    elif kind in ("text", "note"):
        clean["text"] = _clean(item.get("text") or item.get("body"))
        if not clean["text"]:
            return None

    elif kind == "bullets":
        source = item.get("items") or item.get("points") or item.get("text")
        entries = source if isinstance(source, list) else _split(source)
        clean["items"] = [_clean(entry, 160) for entry in entries][:12]
        if not clean["items"]:
            return None

    elif kind == "kv":
        rows = []
        source = item.get("rows") or item.get("pairs")
        if isinstance(source, dict):
            source = list(source.items())
        for row in (source or []):
            if isinstance(row, (list, tuple)) and len(row) >= 2:
                rows.append([_clean(row[0], 60), _clean(row[1], 90)])
            elif isinstance(row, str) and ":" in row:
                key, _, value = row.partition(":")
                rows.append([_clean(key, 60), _clean(value, 90)])
        clean["rows"] = rows[:10]
        if not clean["rows"]:
            return None

    elif kind == "button":
        clean["label"] = _clean(item.get("label") or item.get("text"), 40)
        # The command is English sent back to the agent, never code. A panel
        # button is a shortcut for something you could have said out loud.
        clean["command"] = _clean(item.get("command") or item.get("say"), 300)
        if not clean["label"] or not clean["command"]:
            return None

    elif kind == "link":
        url = _clean(item.get("url") or item.get("href"), 400)
        if not re.match(r"^(https?://|file:///|[A-Za-z]:\\\\)", url):
            return None
        clean["label"] = _clean(item.get("label") or url, 60)
        clean["url"] = url

    return clean


def normalise_panel(spec):
    """Coerce a whole panel spec, raising ValueError only if unusable."""
    if not isinstance(spec, dict):
        raise ValueError("A panel must be an object.")
    title = _clean(spec.get("title") or spec.get("name"), 60)
    if not title:
        raise ValueError("A panel needs a title.")

    raw_items = spec.get("items")
    if isinstance(raw_items, str):
        try:
            raw_items = json.loads(raw_items)
        except Exception:
            raw_items = [{"kind": "text", "text": raw_items}]
    if isinstance(raw_items, dict):
        raw_items = [raw_items]

    items = []
    for entry in (raw_items or []):
        item = normalise_item(entry)
        if item:
            items.append(item)
        if len(items) >= MAX_ITEMS:
            break

    accent = str(spec.get("accent") or spec.get("color") or "blue").lower()
    return {
        "id": _clean(spec.get("id"), 40) or f"pnl_{uuid.uuid4().hex[:8]}",
        "title": title,
        "note": _clean(spec.get("note") or spec.get("subtitle"), 120),
        "accent": accent if accent in ACCENTS else "blue",
        "pinned": bool(spec.get("pinned", False)),
        "items": items,
        "created": spec.get("created") or time.strftime("%Y-%m-%d %H:%M"),
        "updated": time.strftime("%Y-%m-%d %H:%M"),
    }


def load(base_dir):
    path = _store_path(base_dir)
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return []
    panels = data.get("panels") if isinstance(data, dict) else data
    result = []
    for entry in (panels or []):
        try:
            result.append(normalise_panel(entry))
        except ValueError:
            continue  # a corrupt panel must not hide the rest
    # Pinned first, then most recently touched.
    result.sort(key=lambda p: (not p["pinned"], p["updated"]), reverse=False)
    return result


def save(base_dir, panels):
    path = _store_path(base_dir)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    payload = {"panels": panels[:MAX_PANELS],
               "saved": time.strftime("%Y-%m-%d %H:%M:%S")}
    temporary = path + ".tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, ensure_ascii=False)
    os.replace(temporary, path)     # never leave a half-written panel file
    return panels


def add(base_dir, spec):
    panels = load(base_dir)
    panel = normalise_panel(spec)
    existing = next((p for p in panels if p["title"].lower() == panel["title"].lower()), None)
    if existing:
        # Same title means "replace this board", not "make a second one".
        panel["id"] = existing["id"]
        panel["created"] = existing["created"]
        panels = [panel if p["id"] == existing["id"] else p for p in panels]
        action = "Replaced"
    else:
        if len(panels) >= MAX_PANELS:
            raise ValueError(
                f"There are already {MAX_PANELS} panels. Remove one first.")
        panels.append(panel)
        action = "Added"
    save(base_dir, panels)
    return panel, action


def update(base_dir, panel_id, changes):
    panels = load(base_dir)
    target = _find(panels, panel_id)
    if not target:
        raise ValueError(f"No panel called '{panel_id}'.")
    merged = dict(target)
    for key in ("title", "note", "accent", "pinned"):
        if key in changes and changes[key] not in (None, ""):
            merged[key] = changes[key]
    if changes.get("items") is not None:
        merged["items"] = changes["items"]
    panel = normalise_panel(merged)
    panel["id"] = target["id"]
    panel["created"] = target["created"]
    save(base_dir, [panel if p["id"] == target["id"] else p for p in panels])
    return panel


def set_value(base_dir, panel_id, label, value=None, detail=None, ratio=None):
    """Update one stat or progress item in place, by its label.

    This is what lets a panel be a live readout rather than a snapshot: the
    researcher updates its own progress bar every few minutes without
    rewriting the whole board.
    """
    panels = load(base_dir)
    target = _find(panels, panel_id)
    if not target:
        raise ValueError(f"No panel called '{panel_id}'.")
    wanted = _clean(label, 60).lower()
    touched = False
    for item in target["items"]:
        if item["kind"] not in ("stat", "progress"):
            continue
        if item.get("label", "").lower() != wanted:
            continue
        if value is not None:
            item["value"] = _clean(value, 40)
        if detail is not None:
            item["detail"] = _clean(detail, 80)
        if ratio is not None:
            item["ratio"] = _ratio(ratio)
        touched = True
    if not touched:
        raise ValueError(f"'{label}' is not a stat on panel '{target['title']}'.")
    target["updated"] = time.strftime("%Y-%m-%d %H:%M")
    save(base_dir, panels)
    return target


def remove(base_dir, panel_id):
    panels = load(base_dir)
    target = _find(panels, panel_id)
    if not target:
        raise ValueError(f"No panel called '{panel_id}'.")
    save(base_dir, [p for p in panels if p["id"] != target["id"]])
    return target


def _find(panels, panel_id):
    wanted = _clean(panel_id, 60).lower()
    if not wanted:
        return None
    for panel in panels:
        if panel["id"].lower() == wanted:
            return panel
    for panel in panels:
        if panel["title"].lower() == wanted:
            return panel
    return None


def describe(base_dir):
    """A one-block summary for the model, so it knows what it already built."""
    panels = load(base_dir)
    if not panels:
        return "No custom panels yet."
    lines = []
    for panel in panels:
        kinds = ", ".join(sorted({item["kind"] for item in panel["items"]}))
        pin = " (pinned)" if panel["pinned"] else ""
        lines.append(f"- {panel['title']} [{panel['id']}]{pin}: "
                     f"{len(panel['items'])} items ({kinds or 'empty'})")
    return "\n".join(lines)
