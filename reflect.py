# -*- coding: utf-8 -*-
"""
reflect.py — VANTA derives its own defaults instead of you writing them.

The answer to "I'd have to hardcode 10,000 situations" is: don't write any of
them. Let it derive the first time, and remember what it derived. It will only
ever encounter a few hundred kinds of situation, and they'll be the ones that
are actually yours — including the ones no rule table would have guessed, like
the fact that you personally leave 40 minutes later than the textbook says.

This runs AFTER a task completes. It is cheap, it has no screenshot, and it is
the only part of the system that gets smarter over time.

    from reflect import reflect_on_task, relevant_precedents

    # before planning:
    precedents = relevant_precedents(BASE_DIR, user_text)   # -> str, inject into planner

    # after <DONE />:
    Thread(target=reflect_on_task, args=(BASE_DIR, user_text, plan, transcript, call_llm)).start()
"""

import json
import os
import datetime
import when

_FILE = os.path.join("vault", "memory", "derived_defaults.json")


def _path(base_dir):
    p = os.path.join(base_dir, _FILE)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    return p


def _load(base_dir):
    try:
        with open(_path(base_dir), "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def _save(base_dir, items):
    with open(_path(base_dir), "w", encoding="utf-8") as f:
        json.dump(items[-300:], f, indent=2)


REFLECT_PROMPT = """\
A task just finished. Your job is to extract what is worth remembering — not what
happened, but what you now know that you did not know before.

Be ruthless. Most tasks teach nothing. If nothing generalises, return an empty list;
a memory full of trivia is worse than an empty one.

Record something only if it would change how you handle a FUTURE, DIFFERENT task.
Good things to record:
- A quantity you had to look up and will need again (a travel time, a build duration,
  how long something of his actually takes)
- A correction he made to your reasoning — these are the most valuable, always keep them
- A preference revealed by what he accepted or rejected
- A method that worked when the obvious one failed, or an approach that reliably fails
- A fact about his environment, tools, accounts, or the shape of his week

Bad things to record: that a task succeeded, what the weather was, anything true of
everyone rather than of him, anything you would have guessed anyway.

Each entry gets a "trigger": the kind of future situation where this should resurface.
Write triggers broadly — "any time-bound commitment involving travel", not "flights".

WHAT HE ASKED:
{user_text}

WHAT YOU PLANNED:
{plan}

WHAT ACTUALLY HAPPENED:
{transcript}

Output ONLY JSON, no fences:
{{"learned": [{{"trigger": "...", "fact": "...", "confidence": "high|medium|low"}}]}}
"""


def reflect_on_task(base_dir, user_text, plan, transcript, call_llm_fn):
    try:
        plan_str = ""
        if plan is not None:
            plan_str = f"{getattr(plan, 'goal', '')}\nsteps: {getattr(plan, 'steps', [])}"
        if len(transcript) > 12000:
            transcript = transcript[:6000] + "\n...\n" + transcript[-6000:]

        raw = call_llm_fn([
            {"role": "system", "content": "You extract durable lessons. You output only JSON."},
            {"role": "user", "content": REFLECT_PROMPT.format(
                user_text=user_text, plan=plan_str, transcript=transcript)},
        ])

        from planner import _extract_json
        data = _extract_json(raw or "") or {}
        learned = data.get("learned") or []
        if not learned:
            print("[Reflect] Nothing worth keeping.")
            return

        items = _load(base_dir)
        now = datetime.datetime.now().isoformat()
        for entry in learned:
            if not entry.get("fact"):
                continue
            entry["learned_at"] = now
            entry["uses"] = 0
            # "The exam is tomorrow" is true for one day and wrong for every
            # day after, and these are injected into every later turn. Resolve
            # the day words against the moment the note is written, so the
            # note still says what it meant a week from now.
            entry["fact"] = when.anchor(entry["fact"], now)
            if entry.get("trigger"):
                entry["trigger"] = when.anchor(entry["trigger"], now)
            items.append(entry)
            print(f"[Reflect] Learned: {entry.get('fact')}")
        _save(base_dir, items)
    except Exception as e:
        print(f"[Reflect] Failed: {e}")


def relevant_precedents(base_dir, user_text, limit=8):
    """
    Naive keyword overlap. Swap for your ChromaDB embedding search once this
    file has a few hundred entries — but do NOT skip the step of injecting
    these into the planner, that is the whole mechanism.
    """
    items = _load(base_dir)
    if not items:
        return ""

    words = {w for w in user_text.lower().split() if len(w) > 3}
    scored = []
    for it in items:
        blob = (it.get("trigger", "") + " " + it.get("fact", "")).lower()
        overlap = sum(1 for w in words if w in blob)
        # corrections and high-confidence entries earn a place even on weak overlap
        if it.get("confidence") == "high":
            overlap += 1
        if overlap:
            scored.append((overlap, it))

    scored.sort(key=lambda x: -x[0])
    top = [it for _, it in scored[:limit]]
    if not top:
        return ""

    lines = ["THINGS YOU LEARNED PREVIOUSLY THAT MAY APPLY HERE:"]
    for it in top:
        # Stamp each one with the day it was concluded. Without it, a note
        # reading "his system is currently running smoothly" or "the exam is
        # on the 22nd, I should remind him about this upcoming event" arrives
        # in the present tense however old it is, and gets repeated as though
        # it were still true. The date is what lets it be judged.
        noted = when.day_name(when.parse(it.get("learned_at")) or
                              datetime.datetime.now().astimezone())
        lines.append(f"- [noted {noted}] ({it.get('trigger','')}) "
                     f"{it.get('fact','')}")
    lines.append("These are your own past conclusions, not rules, and each was "
                 "true on the day it was noted rather than necessarily now. If "
                 "the current situation differs, or a note has simply been "
                 "overtaken by the date, reason from scratch and say why you "
                 "departed from them.")
    return "\n".join(lines)