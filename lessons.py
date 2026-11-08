# -*- coding: utf-8 -*-
"""
lessons.py -- what VANTA keeps from what it teaches.

A lesson used to be pixels. The board was wiped, the session ended, and the
next time the same ground came up it was researched again from nothing. That is
an odd thing for a system with a knowledge graph sitting next to it.

Every page of a lesson now goes to the same consolidator the researcher uses,
on the research path, so the extracted relationships land as domain facts
rather than as claims about Himanshu. The result is that teaching and
researching fill the same graph: explaining attention writes what attention
relates to, and a later question about transformers can be answered from what
was already taught rather than searched again.

Ingestion is deliberately best-effort. A graph that is down must never stop a
lesson mid-sentence, so every failure here is logged and swallowed.
"""

import os
import time

STATE_FILE = ("vault", "system", "lessons.json")
MIN_INGEST_CHARS = 80


def ingest_page(base_dir, topic, page_number, text, ui_queue=None):
    """Send one finished page to the graph. Returns a short receipt."""
    body = " ".join(str(text or "").split())
    if len(body) < MIN_INGEST_CHARS:
        return ""       # a title and one bullet is not knowledge worth keeping
    try:
        from memory.consolidator import queue_for_consolidation
        queued = queue_for_consolidation(
            f"Subject: {topic}\n\n{body[:8000]}",
            source_id=f"lesson:{str(topic)[:48]}:p{page_number}",
            mode="research")
    except Exception as exc:
        print(f"[Lesson] Could not queue page {page_number} for the graph: {exc}")
        return ""
    if not queued:
        return ""
    note = f"[Lesson] Page {page_number} of '{topic}' queued for the graph."
    if ui_queue is not None:
        try:
            ui_queue.put(("info", note))
        except Exception:
            pass
    return note


def record(base_dir, topic, pages):
    """Note that a lesson happened, so VANTA knows what it has taught."""
    import json

    path = os.path.join(base_dir, *STATE_FILE)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        try:
            with open(path, encoding="utf-8") as handle:
                data = json.load(handle)
        except (FileNotFoundError, json.JSONDecodeError):
            data = {"lessons": []}
        data.setdefault("lessons", []).insert(0, {
            "topic": str(topic)[:120], "pages": int(pages),
            "taught": time.strftime("%Y-%m-%d %H:%M"),
        })
        data["lessons"] = data["lessons"][:80]
        temporary = path + ".tmp"
        with open(temporary, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, ensure_ascii=False)
        os.replace(temporary, path)
    except Exception as exc:
        print(f"[Lesson] Could not record the lesson: {exc}")


def taught_before(base_dir, limit=12):
    """Subjects already covered, so a lesson can build on the last one."""
    import json

    try:
        with open(os.path.join(base_dir, *STATE_FILE), encoding="utf-8") as handle:
            lessons = json.load(handle).get("lessons", [])
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return []
    return lessons[:limit]
