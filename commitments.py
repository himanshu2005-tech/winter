# -*- coding: utf-8 -*-
"""
commitments.py — VANTA's structured obligation store.

Right now everything VANTA learns goes into ChromaDB as unstructured text, and
proactivity is a 15-minute timer that says "look at the screen, say something if
it matters." That is not awareness, that is a nag. Nothing in the system can
answer "what does Himanshu owe the world in the next 12 hours, and is he on track?"

A commitment is anything with a hard time attached that has consequences if missed.
Once they are structured, anticipation becomes arithmetic instead of vibes.

    from commitments import add, due_soon, monitor_worker

    add(base_dir, {
        "what": "Flight 6E-234 to Delhi",
        "hard_time": "2026-09-18T12:00:00",
        "location": "RGIA Shamshabad",
        "prep_minutes": 120,          # airport buffer
        "travel_minutes": 75,         # padded, computed by the planner
        "origin": "home",
    })
"""

import json
import os
import datetime
import time
import uuid
import threading

_FILENAME = os.path.join("vault", "memory", "commitments.json")
_lock = threading.RLock()


def _path(base_dir):
    p = os.path.join(base_dir, _FILENAME)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    return p


def _load(base_dir):
    try:
        with open(_path(base_dir), "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def _save(base_dir, items):
    target = _path(base_dir)
    temporary = target + ".tmp"
    with open(temporary, "w", encoding="utf-8") as f:
        json.dump(items, f, indent=2)
    os.replace(temporary, target)


def _parse(ts):
    if not ts:
        return None
    try:
        return datetime.datetime.fromisoformat(ts.replace("Z", ""))
    except Exception:
        return None


def leave_by(item):
    """The actual actionable time — when he must physically start moving.
    This back-computation is the whole point of the structure."""
    hard = _parse(item.get("hard_time"))
    if not hard:
        return None
    total = int(item.get("prep_minutes", 0)) + int(item.get("travel_minutes", 0))
    if total:
        total += 15  # getting out the door
    return hard - datetime.timedelta(minutes=total)


def add(base_dir, item):
    item = dict(item or {})
    if not str(item.get("what") or "").strip():
        return "[Error] A commitment needs a description."
    if not _parse(item.get("hard_time")):
        return "[Error] A commitment needs a valid ISO date and time."
    with _lock:
        items = _load(base_dir)
        item.setdefault("id", uuid.uuid4().hex[:8])
        item.setdefault("status", "active")
        item.setdefault("created", datetime.datetime.now().isoformat())
        item.setdefault("fired", [])
        # Don't duplicate the same thing at the same time.  The background
        # retention worker and the foreground action may both observe the same
        # explicit statement, so idempotency is part of the contract.
        for existing in items:
            if (existing.get("what", "").casefold()
                    == str(item.get("what", "")).casefold()
                    and existing.get("hard_time") == item.get("hard_time")
                    and existing.get("status") == "active"):
                return f"Already tracking: {item.get('what')}"
        items.append(item)
        _save(base_dir, items)
    lb = leave_by(item)
    return (f"Tracking '{item.get('what')}' at {item.get('hard_time')}"
            + (f" — leave by {lb.strftime('%H:%M')}" if lb else ""))


def complete(base_dir, cid):
    with _lock:
        items = _load(base_dir)
        for it in items:
            if it.get("id") == cid:
                it["status"] = "done"
        _save(base_dir, items)
    return f"Commitment {cid} closed."


def due_soon(base_dir, hours=24):
    """Formatted for injection into the model's context every turn."""
    now = datetime.datetime.now()
    horizon = now + datetime.timedelta(hours=hours)
    out = []
    for it in _load(base_dir):
        if it.get("status") != "active":
            continue
        hard = _parse(it.get("hard_time"))
        if not hard or hard > horizon or hard < now - datetime.timedelta(hours=2):
            continue
        lb = leave_by(it)
        line = f"- [{it['id']}] {it.get('what')} at {hard.strftime('%a %H:%M')}"
        if it.get("location"):
            line += f" ({it['location']})"
        if lb:
            mins = int((lb - now).total_seconds() / 60)
            line += f" | must leave by {lb.strftime('%H:%M')}"
            line += f" — that is in {mins} min" if mins > 0 else " — THAT TIME HAS PASSED"
        out.append(line)
    return "\n".join(out) if out else "No tracked commitments in the next 24h."


# ------------------------------------------------------------------ #
# MONITOR
#
# Fires at specific, meaningful offsets rather than on a wall-clock
# timer. Each offset fires once per commitment, ever. This is what makes
# it feel like something is actually watching rather than periodically
# waking up confused.
# ------------------------------------------------------------------ #

# (minutes_before_leave_by, urgency tag)
_TRIGGERS = [
    (720, "horizon"),   # 12h out: is everything prepared?
    (120, "prep"),      # 2h before you must leave
    (30,  "warn"),
    (10,  "urgent"),
    (0,   "now"),
]

_TRIGGER_BRIEF = {
    "horizon": "Check whether anything still needs doing before this (documents, packing, "
               "printing, charging). Mention only what is genuinely outstanding.",
    "prep": "He should start wrapping up whatever he is doing. One sentence.",
    "warn": "Remind him of the leave-by time. One short sentence.",
    "urgent": "He needs to move now. One short sentence, no elaboration.",
    "now": "This is the leave-by time. Say it plainly, once.",
}


def monitor_worker(base_dir, input_queue, poll_seconds=60):
    """Thread target. Replaces the 15-minute situational-awareness pulse."""
    print("[Commitments] Monitor active.")
    while True:
        try:
            import runtime_power
            if not runtime_power.background_allowed():
                time.sleep(poll_seconds)
                continue
            now = datetime.datetime.now()
            items = _load(base_dir)
            changed = False

            for it in items:
                if it.get("status") != "active":
                    continue
                anchor = leave_by(it) or _parse(it.get("hard_time"))
                if not anchor:
                    continue

                mins_out = (anchor - now).total_seconds() / 60.0

                if mins_out < -30:
                    it["status"] = "passed"
                    changed = True
                    continue

                fired = it.setdefault("fired", [])
                for offset, tag in _TRIGGERS:
                    if tag in fired:
                        continue
                    # fire when we cross into the window, not before it
                    if mins_out <= offset and mins_out > offset - (poll_seconds / 60.0) - 2:
                        fired.append(tag)
                        changed = True
                        msg = (
                            f"[SYSTEM EVENT: COMMITMENT {tag.upper()}] "
                            f"'{it.get('what')}' at {it.get('hard_time')}. "
                            f"Leave-by time is {anchor.strftime('%H:%M')}, which is "
                            f"{int(mins_out)} minutes from now. "
                            f"{_TRIGGER_BRIEF.get(tag, '')} "
                            f"Speak in one or two short sentences. Do not list data. "
                            f"If nothing is actually needed from him, output <DONE /> and stay silent."
                        )
                        input_queue.put(msg)
                        break  # one trigger per poll per commitment

            if changed:
                _save(base_dir, items)

        except Exception as e:
            print(f"[Commitments] Monitor error: {e}")

        time.sleep(poll_seconds)


# ------------------------------------------------------------------ #
# Action handler — drop into actions.py's execute_actions()
#
#   elif action_type == 'TRACK_COMMITMENT':
#       import commitments
#       execute_actions._last_output = commitments.add(base_dir, {
#           "what": attrs.get('what', ''),
#           "hard_time": attrs.get('hard_time', ''),
#           "location": attrs.get('location', ''),
#           "prep_minutes": int(attrs.get('prep_minutes', 0) or 0),
#           "travel_minutes": int(attrs.get('travel_minutes', 0) or 0),
#       })
#
#   elif action_type == 'LIST_COMMITMENTS':
#       import commitments
#       execute_actions._last_output = commitments.due_soon(base_dir, hours=72)
#
#   elif action_type == 'COMPLETE_COMMITMENT':
#       import commitments
#       execute_actions._last_output = commitments.complete(base_dir, attrs.get('id',''))
# ------------------------------------------------------------------ #
