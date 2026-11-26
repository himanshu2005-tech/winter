# -*- coding: utf-8 -*-
"""
routines.py — recurring work, not recurring noise.

An alarm says a sentence at a time. A routine is different: it is a standing
instruction that runs through the full agent, with tools, whenever it comes
due. "Every weekday at 08:00, check my calendar and tell me the first thing
that needs me" is a routine. "Remind me at 7" is an alarm.

Schedules are written the way Himanshu would say them rather than as cron
fields, because he is speaking to VANTA, not editing a crontab:

    daily 08:00            weekdays 09:30         weekends 11:00
    weekly mon 18:00       monthly 1 09:00        every 45m
    hourly

A routine advances past missed historical slots, so a week asleep never causes
a catch-up storm.  A routine that becomes due while VANTA is busy is still
placed on the normal proactive queue, whose runtime-level admission control can
defer it safely; due work must not disappear merely because a turn was active.
"""

import datetime
import json
import os
import re
import threading
import time
import uuid

_DAYS = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}
_lock = threading.RLock()
_WATCH_ID = re.compile(r"\bwatch-[0-9a-f]{8,64}\b", re.IGNORECASE)


def _path(base_dir):
    return os.path.join(base_dir, "vault", "system", "routines.json")


def _load(base_dir):
    try:
        with open(_path(base_dir), "r", encoding="utf-8") as handle:
            return json.load(handle)
    except Exception:
        return []


def _save(base_dir, items):
    target = _path(base_dir)
    os.makedirs(os.path.dirname(target), exist_ok=True)
    temporary = target + ".tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(items, handle, indent=2)
    os.replace(temporary, target)


def parse_schedule(text):
    """Turn a spoken schedule into a structure. Returns (spec, error)."""
    raw = " ".join(str(text or "").lower().split())
    if not raw:
        return None, "A schedule is required, for example 'weekdays 08:00'."

    match = re.fullmatch(r"every\s+(\d+)\s*(m|min|mins|minutes|h|hr|hrs|hours)", raw)
    if match:
        count = int(match.group(1))
        minutes = count * (60 if match.group(2).startswith("h") else 1)
        if minutes < 1:
            return None, "An interval must be at least one minute."
        return {"kind": "interval", "minutes": minutes}, None

    if raw == "hourly":
        return {"kind": "interval", "minutes": 60}, None

    match = re.fullmatch(r"(daily|weekdays|weekends)\s+(\d{1,2}):(\d{2})", raw)
    if match:
        hour, minute = int(match.group(2)), int(match.group(3))
        if not (0 <= hour < 24 and 0 <= minute < 60):
            return None, f"'{raw}' is not a valid time."
        return {"kind": match.group(1), "hour": hour, "minute": minute}, None

    match = re.fullmatch(r"weekly\s+([a-z]{3})\w*\s+(\d{1,2}):(\d{2})", raw)
    if match:
        day = _DAYS.get(match.group(1))
        if day is None:
            return None, f"'{match.group(1)}' is not a day of the week."
        return {"kind": "weekly", "weekday": day,
                "hour": int(match.group(2)), "minute": int(match.group(3))}, None

    match = re.fullmatch(r"monthly\s+(\d{1,2})\s+(\d{1,2}):(\d{2})", raw)
    if match:
        return {"kind": "monthly", "day": max(1, min(28, int(match.group(1)))),
                "hour": int(match.group(2)), "minute": int(match.group(3))}, None

    return None, (
        f"I could not read the schedule '{text}'. Use one of: 'daily 08:00', "
        "'weekdays 09:30', 'weekends 11:00', 'weekly mon 18:00', "
        "'monthly 1 09:00', 'every 45m', or 'hourly'."
    )


def next_run(spec, after=None):
    """The next datetime this schedule is due, strictly after `after`."""
    now = after or datetime.datetime.now()
    kind = spec.get("kind")

    if kind == "interval":
        return now + datetime.timedelta(minutes=int(spec.get("minutes", 60)))

    hour, minute = int(spec.get("hour", 9)), int(spec.get("minute", 0))
    candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate <= now:
        candidate += datetime.timedelta(days=1)

    if kind == "daily":
        return candidate
    if kind == "weekdays":
        while candidate.weekday() > 4:
            candidate += datetime.timedelta(days=1)
        return candidate
    if kind == "weekends":
        while candidate.weekday() < 5:
            candidate += datetime.timedelta(days=1)
        return candidate
    if kind == "weekly":
        wanted = int(spec.get("weekday", 0))
        while candidate.weekday() != wanted:
            candidate += datetime.timedelta(days=1)
        return candidate
    if kind == "monthly":
        day = int(spec.get("day", 1))
        candidate = now.replace(day=min(day, 28), hour=hour, minute=minute,
                                second=0, microsecond=0)
        if candidate <= now:
            month = candidate.month + 1
            year = candidate.year + (1 if month > 12 else 0)
            candidate = candidate.replace(year=year, month=1 if month > 12 else month)
        return candidate
    return now + datetime.timedelta(hours=1)


def describe(spec):
    kind = spec.get("kind")
    if kind == "interval":
        minutes = int(spec.get("minutes", 60))
        return f"every {minutes // 60}h" if minutes % 60 == 0 and minutes >= 60 else f"every {minutes}m"
    at = f"{int(spec.get('hour', 0)):02d}:{int(spec.get('minute', 0)):02d}"
    if kind == "weekly":
        day = [d for d, i in _DAYS.items() if i == spec.get("weekday")]
        return f"every {day[0] if day else 'mon'} at {at}"
    if kind == "monthly":
        return f"on day {spec.get('day', 1)} of each month at {at}"
    return f"{kind} at {at}"


def _expiry(value):
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        return datetime.date.fromisoformat(raw).isoformat()
    except ValueError as exc:
        raise ValueError("expires_on must be YYYY-MM-DD") from exc


def add(base_dir, name, prompt, schedule, expires_on=""):
    spec, error = parse_schedule(schedule)
    if error:
        return f"[Error] {error}"
    if not str(prompt or "").strip():
        return "[Error] A routine needs an instruction to run."
    try:
        expiry = _expiry(expires_on)
    except ValueError as exc:
        return f"[Error] {exc}"
    with _lock:
        items = _load(base_dir)
        clean = str(name or "").strip() or str(prompt)[:40]
        for existing in items:
            if existing.get("name", "").casefold() == clean.casefold():
                return f"[Error] A routine called '{clean}' already exists. Delete it first."
            if (str(existing.get("prompt", "")).casefold()
                    == str(prompt).strip().casefold()
                    and existing.get("schedule") == spec
                    and existing.get("enabled", True)):
                return (f"[Success] Already scheduled as routine "
                        f"'{existing.get('name', clean)}'.")
        due = next_run(spec)
        items.append({
            "id": uuid.uuid4().hex[:8],
            "name": clean,
            "prompt": str(prompt).strip(),
            "schedule": spec,
            "schedule_text": " ".join(str(schedule).split()),
            "enabled": True,
            "created": datetime.datetime.now().isoformat(timespec="seconds"),
            "next_run": due.isoformat(timespec="seconds"),
            "last_run": None,
            "runs": 0,
            "expires_on": expiry,
        })
        _save(base_dir, items)
    expiry_note = f" It expires after {expiry}." if expiry else ""
    return (f"[Success] Routine '{clean}' scheduled {describe(spec)}. "
            f"First run {due.strftime('%a %d %b %H:%M')}.{expiry_note}")


def listing(base_dir):
    items = _load(base_dir)
    if not items:
        return "No routines are scheduled."
    lines = [f"Routines ({len(items)}):"]
    for item in items:
        state = "" if item.get("enabled", True) else "  [paused]"
        due = str(item.get("next_run", ""))[:16].replace("T", " ")
        expiry_line = (f"    expires after {item['expires_on']}" 
                       if item.get("expires_on") else "")
        lines.append(f"- [{item['id']}] {item['name']} — {item.get('schedule_text','')}{state}")
        lines.append(f"    next {due} · run {item.get('runs', 0)}x · \"{item['prompt'][:70]}\"")
        if expiry_line:
            lines.append(expiry_line)
    return "\n".join(lines)


def remove(base_dir, identifier):
    wanted = str(identifier or "").strip().casefold()
    if not wanted:
        return "[Error] Give the routine's id or name."
    with _lock:
        items = _load(base_dir)
        keep = [i for i in items
                if i["id"].casefold() != wanted and i["name"].casefold() != wanted]
        if len(keep) == len(items):
            return f"[Error] No routine matching '{identifier}'. Use LIST_ROUTINES."
        _save(base_dir, keep)
    return f"[Success] Deleted routine '{identifier}'."


def set_enabled(base_dir, identifier, enabled):
    wanted = str(identifier or "").strip().casefold()
    with _lock:
        items = _load(base_dir)
        touched = None
        for item in items:
            if item["id"].casefold() == wanted or item["name"].casefold() == wanted:
                item["enabled"] = bool(enabled)
                if enabled:
                    item["next_run"] = next_run(item["schedule"]).isoformat(timespec="seconds")
                touched = item
        if not touched:
            return f"[Error] No routine matching '{identifier}'."
        _save(base_dir, items)
    return f"[Success] Routine '{touched['name']}' {'resumed' if enabled else 'paused'}."


def due_now(base_dir, now=None):
    """Routines that should fire, with their next_run already advanced."""
    now = now or datetime.datetime.now()
    fired = []
    with _lock:
        items = _load(base_dir)
        changed = False
        active_items = []
        for item in items:
            try:
                expired = bool(
                    item.get("expires_on") and
                    now.date() > datetime.date.fromisoformat(item["expires_on"])
                )
            except ValueError:
                expired = False
            if expired:
                changed = True
                print(f"[Routines] Expired '{item.get('name', item.get('id'))}'.")
            else:
                active_items.append(item)
        items = active_items
        for item in items:
            if not item.get("enabled", True):
                continue
            # Watches own their polling schedule. Some older plans also made a
            # daily LLM routine that named those watch IDs. Once every named
            # watch is paused/cancelled, that wrapper has no work and must not
            # wake Himanshu merely to report predictable cancellation errors.
            watch_ids = _WATCH_ID.findall(str(item.get("prompt", "")))
            if watch_ids:
                try:
                    import watches
                    dependencies = watches.dependency_state(watch_ids)
                    if dependencies["all_inactive"]:
                        item["enabled"] = False
                        item["disabled_reason"] = "all referenced watches are inactive"
                        item["disabled_at"] = now.isoformat(timespec="seconds")
                        changed = True
                        print(f"[Routines] Paused '{item.get('name', item.get('id'))}'; all referenced watches are inactive.")
                        continue
                except Exception as exc:
                    # Fail open for availability, but never infer that a watch
                    # is cancelled just because Neo4j could not be reached.
                    print(f"[Routines] Could not validate watch dependencies: {exc}")
            try:
                due = datetime.datetime.fromisoformat(item.get("next_run"))
            except Exception:
                due = now
            if due > now:
                continue
            fired.append(dict(item))
            item["last_run"] = now.isoformat(timespec="seconds")
            item["runs"] = int(item.get("runs", 0)) + 1
            # Advance from now, not from the missed slot: a machine that was
            # asleep for a week must not fire seven catch-up briefings.
            item["next_run"] = next_run(item["schedule"], now).isoformat(timespec="seconds")
            changed = True
        if changed:
            _save(base_dir, items)
    return fired


def dispatch_due(base_dir, input_queue, is_busy=None, now=None):
    """Put every newly due routine on the runtime's durable ingress queue.

    Busy state is diagnostic only.  The conversation runtime owns deferral and
    ordering; dropping the item here after ``due_now`` advanced its schedule
    permanently lost the work.
    """
    count = 0
    for item in due_now(base_dir, now=now):
        if is_busy is not None and is_busy():
            print(f"[Routines] '{item['name']}' queued while VANTA is busy.")
        else:
            print(f"[Routines] Firing '{item['name']}'.")
        input_queue.put((
            "proactive",
            f"[SCHEDULED ROUTINE: {item['name']}] {item['prompt']}",
        ))
        count += 1
    return count


def worker(base_dir, input_queue, is_busy=None, poll_seconds=30):
    """Fire due routines into the agent as proactive turns."""
    print("[Routines] Scheduler started.")
    while True:
        try:
            dispatch_due(base_dir, input_queue, is_busy=is_busy)
        except Exception as exc:
            print(f"[Routines] Scheduler error: {exc}")
        time.sleep(max(10, int(poll_seconds)))
