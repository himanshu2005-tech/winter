"""Validation for VANTA's compact, declarative desktop widgets.

The model chooses content and presentation from a small visual grammar.  It
never supplies Python, HTML, JavaScript, colours, filesystem paths, or event
handlers, so a generated widget remains data rather than executable code.
"""
from __future__ import annotations

import time


KINDS = frozenset({
    "timer", "metric", "status", "progress", "checklist", "list",
    "table", "bar_chart", "timeline", "flow",
})


def _text(value, limit=120):
    return " ".join(str(value or "").split())[:limit]


def _number(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def normalize(raw):
    if not isinstance(raw, dict):
        raise ValueError("widget content must be one JSON object")
    kind = _text(raw.get("kind") or "status", 24).lower()
    if kind not in KINDS:
        raise ValueError("widget kind must be " + ", ".join(sorted(KINDS)))
    spec = {
        "id": _text(raw.get("id") or kind, 64),
        "kind": kind,
        "title": _text(raw.get("title") or kind.replace("_", " ").title(), 80),
        "subtitle": _text(raw.get("subtitle"), 180),
        "value": _text(raw.get("value"), 120),
        "unit": _text(raw.get("unit"), 24),
    }
    if kind == "timer":
        deadline = _number(raw.get("deadline"), 0)
        if not deadline:
            seconds = max(0, min(_number(raw.get("duration_seconds"), 0), 86400))
            if not seconds:
                raise ValueError("timer widget needs deadline or duration_seconds")
            deadline = time.time() + seconds
        spec["deadline"] = deadline
    if kind == "progress":
        progress = _number(raw.get("progress"), 0)
        spec["progress"] = max(0.0, min(1.0, progress / 100 if progress > 1 else progress))
    if kind in {"checklist", "list", "timeline", "flow"}:
        items = raw.get("items") or []
        if not isinstance(items, list):
            raise ValueError("widget items must be a JSON array")
        cleaned = []
        for item in items[:12]:
            if isinstance(item, dict):
                cleaned.append({
                    "label": _text(item.get("label") or item.get("text"), 140),
                    "detail": _text(item.get("detail"), 180),
                    "done": bool(item.get("done")),
                })
            else:
                cleaned.append({"label": _text(item, 140), "detail": "", "done": False})
        spec["items"] = [item for item in cleaned if item["label"]]
    if kind == "table":
        columns = raw.get("columns") or raw.get("headers") or []
        rows = raw.get("rows") or []
        if not isinstance(columns, list) or not isinstance(rows, list):
            raise ValueError("table columns and rows must be JSON arrays")
        spec["columns"] = [_text(cell, 48) for cell in columns[:6]]
        spec["rows"] = [
            [_text(cell, 72) for cell in (row if isinstance(row, list) else [row])[:6]]
            for row in rows[:10]
        ]
    if kind == "bar_chart":
        series = raw.get("series") or raw.get("items") or []
        if not isinstance(series, list):
            raise ValueError("bar_chart series must be a JSON array")
        spec["series"] = [
            {"label": _text(item.get("label"), 36),
             "value": _number(item.get("value"), 0)}
            for item in series[:10] if isinstance(item, dict)
        ]
    return spec

