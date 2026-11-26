"""
alarms.py - VANTA Smart Alarm System (Dual-Store SQLite Scheduler)
"""
import os
import json
import time
import datetime
import threading
import uuid
from memory.state_db import execute_read, execute_write

_ALARM_FILE = None
_lock = threading.Lock()

# Config Flag for Fallback
USE_JSON_FALLBACK = os.getenv("USE_JSON_FALLBACK", "false").lower() == "true"


def _coerce_datetime(value):
    """Return an aware Python datetime from legacy or Neo4j temporal values.

    Alarm nodes created by older code may contain ISO strings, while the
    Neo4j driver can return its own DateTime object (with ``to_native``) or an
    ordinary ``datetime``.  Keep the scheduler compatible with all three so a
    valid temporal value is never mistaken for database downtime.
    """
    if value is None:
        raise ValueError("alarm fire_at is missing")

    if isinstance(value, datetime.datetime):
        result = value
    else:
        to_native = getattr(value, "to_native", None)
        if callable(to_native):
            result = to_native()
            if not isinstance(result, datetime.datetime):
                raise TypeError(
                    f"alarm fire_at.to_native() returned {type(result).__name__}")
        else:
            text = str(value).strip()
            if not text:
                raise ValueError("alarm fire_at is empty")
            result = datetime.datetime.fromisoformat(
                text.replace("Z", "+00:00"))

    if result.tzinfo is None:
        result = result.replace(tzinfo=datetime.datetime.now().astimezone().tzinfo)
    return result


def _looks_like_store_outage(error):
    """Classify only connection/availability failures as Neo4j outages."""
    name = type(error).__name__.lower()
    text = str(error).lower()
    markers = (
        "serviceunavailable", "sessionexpired", "connection", "connect",
        "routing", "bolt", "neo4j is temporarily unavailable",
        "failed to establish", "actively refused", "database unavailable",
    )
    return any(marker in name or marker in text for marker in markers)

def _get_alarm_file(base_dir=None):
    global _ALARM_FILE
    if _ALARM_FILE is None:
        if base_dir:
            _ALARM_FILE = os.path.join(base_dir, "vault", "alarms.json")
        else:
            _ALARM_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "vault", "alarms.json")
    os.makedirs(os.path.dirname(_ALARM_FILE), exist_ok=True)
    return _ALARM_FILE

def _load(base_dir=None):
    path = _get_alarm_file(base_dir)
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as f:
        try:
            return json.load(f)
        except Exception:
            return []

def _save(alarms_list, base_dir=None):
    path = _get_alarm_file(base_dir)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(alarms_list, f, indent=2)

# --- SQLite Scheduler Methods ---

def set_alarm(time_str, label, recurrence="none", created_by="user_stated", source=None, base_dir=None):
    if USE_JSON_FALLBACK:
        return _set_alarm_json(time_str, label, base_dir)
        
    try:
        # Time normalization. Default today, if passed, tomorrow.
        now = datetime.datetime.now().astimezone()
        if "AM" in time_str.upper() or "PM" in time_str.upper():
            t = datetime.datetime.strptime(time_str.strip(), "%I:%M %p")
        else:
            t = datetime.datetime.strptime(time_str.strip(), "%H:%M")
            
        fire_at = now.replace(hour=t.hour, minute=t.minute, second=0, microsecond=0)
        if fire_at <= now:
            fire_at += datetime.timedelta(days=1)
            
        fire_iso = fire_at.isoformat()
    except Exception:
        return f"Could not parse time: {time_str}"

    alarm_id = str(uuid.uuid4())
    
    # Write-verify: insert then select back
    execute_write(
        "INSERT INTO alarms (id, label, fire_at, recurrence, enabled, created_by, source, status) VALUES (?, ?, ?, ?, 1, ?, ?, 'pending')",
        (alarm_id, label, fire_iso, recurrence, created_by, source)
    )
    
    verify = execute_read("SELECT id, fire_at FROM alarms WHERE id = ?", (alarm_id,))
    if not verify:
        return f"Failed to verify alarm creation in SQLite."
        
    display = fire_at.strftime("%I:%M %p").lstrip("0")
    return f"Alarm set for {display}: {label}"

def _set_alarm_json(time_str, label, base_dir=None):
    try:
        if "AM" in time_str.upper() or "PM" in time_str.upper():
            t = datetime.datetime.strptime(time_str.strip(), "%I:%M %p")
        else:
            t = datetime.datetime.strptime(time_str.strip(), "%H:%M")
        normalized = t.strftime("%H:%M")
    except Exception:
        return f"Could not parse time: {time_str}"

    with _lock:
        alarms_list = _load(base_dir)
        alarm_id = str(int(time.time()))
        alarms_list.append({"id": alarm_id, "time": normalized, "label": label, "fired_date": None})
        _save(alarms_list, base_dir)

    display = t.strftime("%I:%M %p").lstrip("0")
    return f"Alarm set for {display}: {label}"

def delete_alarm(alarm_id, base_dir=None):
    if USE_JSON_FALLBACK:
        with _lock:
            alarms_list = _load(base_dir)
            new_list = [a for a in alarms_list if a.get("id") != alarm_id]
            if len(new_list) == len(alarms_list):
                return f"No alarm found with ID {alarm_id}"
            _save(new_list, base_dir)
        return f"Alarm {alarm_id} deleted."
        
    execute_write("UPDATE alarms SET status = 'dismissed' WHERE id = ?", (alarm_id,))
    return f"Alarm {alarm_id} deleted."

def list_alarms(base_dir=None):
    if USE_JSON_FALLBACK:
        alarms_list = _load(base_dir)
        if not alarms_list:
            return "No alarms set."
        lines = []
        for a in alarms_list:
            try:
                t = datetime.datetime.strptime(a["time"], "%H:%M")
                display = t.strftime("%I:%M %p").lstrip("0")
            except Exception:
                display = a["time"]
            lines.append(f"  - [{a['id']}] {display}: {a['label']}")
        return "Current alarms:\n" + "\n".join(lines)
        
    rows = execute_read("SELECT id, fire_at, label, recurrence FROM alarms WHERE status = 'pending' AND enabled = 1 ORDER BY fire_at ASC")
    recent = execute_read(
        "SELECT id, fire_at, label, status FROM alarms "
        "WHERE status IN ('fired', 'missed') ORDER BY fire_at DESC LIMIT 10")
    if not rows and not recent:
        return "No alarms set and no recent alarm activity."
    lines = []
    for row in rows:
        try:
            dt = _coerce_datetime(row[1])
            display = dt.strftime("%Y-%m-%d %I:%M %p")
        except (TypeError, ValueError):
            display = "invalid time"
        lines.append(f"  - [{row[0]}] {display}: {row[2]} (Recurrence: {row[3]})")
    sections = ["Current alarms:\n" + "\n".join(lines)] if lines else ["Current alarms: none."]
    if recent:
        history = []
        for row in recent:
            try:
                fire_dt = _coerce_datetime(row[1])
                fire_str = fire_dt.strftime("%Y-%m-%d %I:%M %p")
            except Exception:
                fire_str = str(row[1])
            # Older stores may still return the pre-status three-column row.
            # Keep that data readable while the compatibility layer now
            # returns the requested status as the fourth column.
            status_str = row[3] if len(row) > 3 else "unknown status"
            history.append(
                f"  - [{row[0]}] {fire_str}: {row[2]} ({status_str})")
        sections.append("Recent alarm activity:\n" + "\n".join(history))
    return "\n".join(sections)

def _advance_recurrence(dt, recurrence):
    """Next fire time for a repeating alarm.

    Only "daily" was handled; every other recurrence returned None, which
    retires the alarm after one fire. A weekly alarm silently became a
    one-shot.
    """
    kind = str(recurrence or "none").strip().lower()
    if kind in {"", "none", "once"}:
        return None
    if kind == "hourly":
        return dt + datetime.timedelta(hours=1)
    if kind == "daily":
        return dt + datetime.timedelta(days=1)
    if kind == "weekly":
        return dt + datetime.timedelta(weeks=1)
    if kind in {"weekdays", "weekday"}:
        nxt = dt + datetime.timedelta(days=1)
        while nxt.weekday() > 4:
            nxt += datetime.timedelta(days=1)
        return nxt
    if kind in {"weekends", "weekend"}:
        nxt = dt + datetime.timedelta(days=1)
        while nxt.weekday() < 5:
            nxt += datetime.timedelta(days=1)
        return nxt
    if kind == "monthly":
        month = dt.month + 1
        year = dt.year + (1 if month > 12 else 0)
        return dt.replace(year=year, month=1 if month > 12 else month,
                          day=min(dt.day, 28))
    return None

def alarm_worker(speak_callback, base_dir=None):
    print("[Alarm] Neo4j scheduler worker started.")
    
    # 1. Startup Rebuild & Missed Alarm Policy
    # Startup reconciliation is done by the normal loop too.  Keeping it in
    # that guarded path means a database outage during boot cannot kill this
    # worker before Neo4j has a chance to recover.
    
    last_error_log = 0.0
    while True:
        try:
            if USE_JSON_FALLBACK:
                now_dt = datetime.datetime.now()
                now_time = now_dt.strftime("%H:%M")
                today = now_dt.date().isoformat()
                with _lock:
                    alarms_list = _load(base_dir)
                    changed = False
                    for a in alarms_list:
                        if a["time"] == now_time and a.get("fired_date") != today:
                            label = a.get("label", "Alarm")
                            speak_callback(f"Sir, this is your alarm for {label}.")
                            a["fired_date"] = today
                            changed = True
                    if changed:
                        _save(alarms_list, base_dir)
            else:
                now = datetime.datetime.now().astimezone()
                pending = execute_read("SELECT id, fire_at, label, recurrence FROM alarms WHERE status = 'pending' AND enabled = 1")
                for row in pending:
                    a_id, fire_str, label, rec = row
                    try:
                        fire_dt = _coerce_datetime(fire_str)
                    except (TypeError, ValueError) as exc:
                        # Preserve malformed legacy data for audit, but do not
                        # let it prevent every valid alarm after it from being
                        # evaluated. "invalid" is a quarantine status, not a
                        # deletion.
                        execute_write(
                            "UPDATE alarms SET status = 'invalid' WHERE id = ?",
                            (a_id,))
                        print(f"[Alarm] Quarantined malformed alarm {a_id}: {exc}")
                        continue
                    
                    if now >= fire_dt:
                        diff = (now - fire_dt).total_seconds()
                        if diff > 900:
                            # Missed alarm during runtime loop? Unlikely, but possible if slept.
                            execute_write("UPDATE alarms SET status = 'missed' WHERE id = ?", (a_id,))
                            speak_callback(f"Sir, a background alarm was missed: {label}.")
                        else:
                            execute_write("UPDATE alarms SET status = 'fired' WHERE id = ?", (a_id,))
                            speak_callback(f"Sir, this is your alarm for {label}.")
                            
                        # Recurrence
                        if rec != "none":
                            next_dt = _advance_recurrence(fire_dt, rec)
                            if next_dt:
                                execute_write("INSERT INTO alarms (id, label, fire_at, recurrence, enabled, created_by, status) SELECT ?, label, ?, recurrence, enabled, created_by, 'pending' FROM alarms WHERE id = ?", (str(uuid.uuid4()), next_dt.isoformat(), a_id))
                                
        except Exception as e:
            now_mono = time.monotonic()
            if now_mono - last_error_log >= 30.0:
                if _looks_like_store_outage(e):
                    print(f"[Alarm] Neo4j unavailable; pending alarms remain durable "
                          f"and will resume automatically: {e}")
                else:
                    print(f"[Alarm] Scheduler data error; no alarm was discarded: {e}")
                last_error_log = now_mono
        time.sleep(1)

def get_upcoming_today(base_dir=None):
    if USE_JSON_FALLBACK:
        alarms_list = _load(base_dir)
        today = datetime.date.today().isoformat()
        now_time = datetime.datetime.now().strftime("%H:%M")
        return [a for a in alarms_list if a.get("fired_date") != today and a["time"] >= now_time]
        
    now = datetime.datetime.now().astimezone()
    # Find pending alarms where the date matches today
    today_prefix = now.date().isoformat()
    rows = execute_read("SELECT id, fire_at, label FROM alarms WHERE status = 'pending' AND enabled = 1 AND fire_at LIKE ?", (f"{today_prefix}%",))
    
    upcoming = []
    for row in rows:
        try:
            fire_dt = _coerce_datetime(row[1])
        except (TypeError, ValueError):
            continue
        if fire_dt >= now:
            upcoming.append({"id": row[0], "time": fire_dt.strftime("%H:%M"), "label": row[2]})
    return upcoming
