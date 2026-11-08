"""Durable, non-repeating proactive knowledge-question ledger."""
from __future__ import annotations

from datetime import datetime, timedelta
import json
import os
import re
import threading

_LOCK = threading.RLock()
_STOP = {"the", "a", "an", "is", "are", "you", "your", "about", "what", "how", "why", "and", "or", "to", "of", "for", "it", "this", "that", "himanshu", "regarding"}


def _path(base_dir):
    path = os.path.join(base_dir, "vault", "system", "proactive_questions.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    return path


def _load(base_dir):
    try:
        with open(_path(base_dir), "r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _save(base_dir, rows):
    path = _path(base_dir)
    temp = path + ".tmp"
    with open(temp, "w", encoding="utf-8") as handle:
        json.dump(rows[-100:], handle, ensure_ascii=False, indent=2)
    os.replace(temp, path)


def _tokens(text):
    return {word for word in re.findall(r"[a-z0-9]+", str(text).casefold()) if len(word) > 2 and word not in _STOP}


def _similar(left, right):
    a, b = _tokens(left), _tokens(right)
    if not a or not b:
        return False
    return len(a & b) / len(a | b) >= 0.45


def may_ask(base_dir, question, now=None):
    """At most one proactive question per day and never a semantic repeat."""
    now = now or datetime.now()
    with _LOCK:
        rows = _load(base_dir)
    for row in rows:
        try:
            asked = datetime.fromisoformat(row.get("asked_at", ""))
        except Exception:
            continue
        if now - asked < timedelta(hours=24):
            return False, "A proactive question was already asked in the last 24 hours."
        if _similar(question, row.get("question", "")):
            return False, "That knowledge gap was already asked."
    return True, ""


def record_question(base_dir, question, now=None):
    now = now or datetime.now()
    with _LOCK:
        rows = _load(base_dir)
        rows.append({"question": " ".join(str(question).split()), "asked_at": now.isoformat(timespec="seconds"), "answer": None, "answered_at": None})
        _save(base_dir, rows)


def record_answer_if_pending(base_dir, answer, now=None):
    """Attach the next timely user response to the latest unanswered question."""
    cleaned = " ".join(str(answer or "").split())
    if not cleaned:
        return False
    now = now or datetime.now()
    with _LOCK:
        rows = _load(base_dir)
        for row in reversed(rows):
            if row.get("answer"):
                continue
            try:
                asked = datetime.fromisoformat(row.get("asked_at", ""))
            except Exception:
                return False
            if now - asked > timedelta(minutes=30):
                return False
            row["answer"] = cleaned[:1000]
            row["answered_at"] = now.isoformat(timespec="seconds")
            _save(base_dir, rows)
            return True
    return False


def context_summary(base_dir, limit=12):
    with _LOCK:
        rows = _load(base_dir)[-limit:]
    if not rows:
        return "No prior proactive knowledge questions."
    lines = ["--- PROACTIVE QUESTIONS ALREADY ASKED (never repeat these gaps) ---"]
    for row in rows:
        answer = row.get("answer") or "no answer"
        lines.append(f"- Q: {row.get('question', '')} | A: {answer}")
    return "\n".join(lines)