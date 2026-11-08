# -*- coding: utf-8 -*-
"""
knowledge_gaps.py — what VANTA knows it does not know.

Health mode is the clearest case: it can adopt a gentler tone, but it has no
sleep, no activity, no history, so it has nothing to actually reason over. The
same is true anywhere Himanshu has simply never mentioned something. Until now
the only outcome was a confident-sounding answer built on nothing.

A gap is a named piece of missing information, recorded the moment VANTA
notices it is missing, with the reason it was needed. Gaps are asked about at a
sensible moment rather than the instant they appear — interrupting a task to
collect background is how an assistant becomes a form. When Himanshu answers,
the answer becomes a real fact with the question as its evidence, and the gap
closes.

Nothing here decides *what* matters. The model names the gap; this module only
remembers it, rations the asking, and closes the loop.
"""

import datetime
import json
import os
import re
import threading
import uuid

_LOCK = threading.RLock()
MAX_OPEN = 40


def _path(base_dir):
    path = os.path.join(base_dir, "vault", "system", "knowledge_gaps.json")
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
        json.dump(rows[-200:], handle, ensure_ascii=False, indent=2)
    os.replace(temp, path)


def _norm(text):
    return " ".join(re.findall(r"[a-z0-9]+", str(text or "").casefold()))


def note(base_dir, topic, question, why="", predicate="", subject="Himanshu"):
    """Record something VANTA needs and does not have."""
    topic = " ".join(str(topic or "").split())[:80]
    question = " ".join(str(question or "").split())[:240]
    if not topic or not question:
        return "[Error] A gap needs a topic and the question to ask."
    with _LOCK:
        rows = _load(base_dir)
        for row in rows:
            if row.get("status") == "open" and _norm(row.get("topic")) == _norm(topic):
                return f"[Success] Already tracking that gap: {row['topic']}."
        if len([r for r in rows if r.get("status") == "open"]) >= MAX_OPEN:
            return ("[Error] Too many open gaps already. Ask about the existing ones "
                    "before recording more.")
        rows.append({
            "id": uuid.uuid4().hex[:8],
            "topic": topic,
            "question": question,
            "why": " ".join(str(why or "").split())[:200],
            "subject": subject or "Himanshu",
            "predicate": str(predicate or "").strip().upper(),
            "status": "open",
            "created": datetime.datetime.now().isoformat(timespec="seconds"),
            "asked_at": None,
            "answer": None,
        })
        _save(base_dir, rows)
    return f"[Success] Noted that I don't know: {topic}. I'll ask when it fits."


def open_gaps(base_dir, limit=20):
    return [r for r in _load(base_dir) if r.get("status") == "open"][:limit]


def listing(base_dir):
    rows = _load(base_dir)
    open_rows = [r for r in rows if r.get("status") == "open"]
    filled = [r for r in rows if r.get("status") == "filled"]
    if not rows:
        return "There's nothing I've noticed I'm missing."
    lines = [f"Open gaps ({len(open_rows)}):"]
    for row in open_rows:
        asked = " · already asked" if row.get("asked_at") else ""
        lines.append(f"- [{row['id']}] {row['topic']}{asked}")
        lines.append(f"    would ask: \"{row['question']}\"")
        if row.get("why"):
            lines.append(f"    needed because: {row['why']}")
    if filled:
        lines.append(f"\nFilled recently ({len(filled)}):")
        for row in filled[-5:]:
            lines.append(f"- {row['topic']}: {str(row.get('answer',''))[:70]}")
    return "\n".join(lines)


def next_question(base_dir):
    """The gap most worth asking about now, respecting the question ledger."""
    candidates = [r for r in open_gaps(base_dir, limit=MAX_OPEN) if not r.get("asked_at")]
    if not candidates:
        return None
    try:
        from proactive_learning import may_ask
        for row in candidates:
            if may_ask(base_dir, row["question"]):
                return row
        return None
    except Exception:
        return candidates[0]


def mark_asked(base_dir, gap_id):
    with _LOCK:
        rows = _load(base_dir)
        for row in rows:
            if row["id"] == gap_id:
                row["asked_at"] = datetime.datetime.now().isoformat(timespec="seconds")
        _save(base_dir, rows)


def fill(base_dir, identifier, answer, store_fact=True):
    """Close a gap with Himanshu's answer and keep it as a fact he can check."""
    wanted = _norm(identifier)
    answer = " ".join(str(answer or "").split())[:400]
    if not answer:
        return "[Error] An answer is required to fill a gap."
    with _LOCK:
        rows = _load(base_dir)
        target = None
        for row in rows:
            if row.get("status") != "open":
                continue
            if row["id"].casefold() == wanted or _norm(row.get("topic")) == wanted:
                target = row
                break
        if target is None:
            return f"[Error] No open gap matching '{identifier}'. Use LIST_GAPS."
        target["status"] = "filled"
        target["answer"] = answer
        target["filled_at"] = datetime.datetime.now().isoformat(timespec="seconds")
        _save(base_dir, rows)

    stored = ""
    if store_fact:
        try:
            from memory.graph_store import add_edge
            predicate = target.get("predicate") or "HAS_DETAIL"
            add_edge(
                target.get("subject") or "Himanshu", predicate, answer,
                confidence=1.0, source_id=f"asked:{target['id']}",
                fact_class="explicit_user_fact", evidence_type="user_stated",
                # The question is the evidence: it records exactly what he was
                # answering, so the fact can be checked rather than trusted.
                evidence_quote=f"Q: {target['question']}  A: {answer}",
                evidence_ref=f"gap:{target['id']}",
            )
            stored = f" Recorded as {predicate}."
        except Exception as exc:
            stored = f" (could not store it as a fact: {exc})"
    return f"[Success] Filled '{target['topic']}'.{stored}"
