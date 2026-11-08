# -*- coding: utf-8 -*-
"""
idle_gate.py -- when VANTA may think on its own, and how much.

Three background jobs share one rule, and having each invent its own is how a
system ends up researching in the middle of a sentence.

    curiosity        reads the web about what he cares about
    deduction        reasons over what the graph already holds
    entity linking   joins what his silos say about the same thing

All three spend model calls and, for the first, network and battery. None of
them is wanted while he is working, and none of them should ever be the reason
the machine is slow when he comes back.

WHEN IT MAY RUN
---------------
Only when nobody is using the machine, and the evidence for that is whatever
actually says so rather than a guess at the clock:

    resting   the screen has gone dark (screen_rest)
    standby   he put VANTA in standby
    sleep     he put it in sleep mode
    away      no keyboard or mouse for a long while

"Night" is deliberately not on that list. Someone awake at two in the morning
is not asleep, and treating the hour as a proxy for the person is how a
background job starts while he is mid-task.

HOW MUCH
--------
Budgets are per run and per day, and the daily one is persisted -- a counter
in memory resets whenever VANTA restarts, so a crash loop would otherwise
quietly spend the allowance again every boot.

WHEN IT MUST STOP
-----------------
Every job asks `interrupted()` between steps. The moment he touches the
machine, or the agent starts a turn, the answer is yes and the job abandons
what it was doing and saves what it has. A background process that makes him
wait for it has stopped being a background process.
"""

import datetime
import json
import os
import tempfile
import threading
import time

_LOCK = threading.RLock()
_FILENAME = os.path.join("vault", "memory", "background_budget.json")
_activity_probe = None

#: How long with no input before "away" counts as idle, independent of any
#: state he set explicitly. Long enough to rule out reading a page.
AWAY_MINUTES = 20.0

#: Input this recent means he is back, whatever state was set.
RETURNED_SECONDS = 20.0

#: On battery below this, background work waits for the charger.
MIN_BATTERY_PERCENT = 40

# --- states ---------------------------------------------------------- #
ACTIVE = "active"
RESTING = "resting"
STANDBY = "standby"
SLEEPING = "sleeping"
AWAY = "away"
IDLE_STATES = (RESTING, STANDBY, SLEEPING, AWAY)


def _idle_seconds():
    try:
        import screen_rest
        return screen_rest.idle_seconds()
    except Exception:
        return None


def current_state():
    """What the machine is doing, from what actually says so."""
    try:
        import screen_rest
        if screen_rest.is_resting():
            return RESTING
    except Exception:
        pass
    try:
        import runtime_power
        if runtime_power.is_standby():
            return STANDBY
    except Exception:
        pass
    try:
        import mode_manager
        if mode_manager.current_mode() == mode_manager.WinterMode.SLEEP:
            return SLEEPING
    except Exception:
        pass
    seconds = _idle_seconds()
    if seconds is not None and seconds >= AWAY_MINUTES * 60.0:
        return AWAY
    return ACTIVE


def user_returned():
    """Whether there has been input in the last few seconds."""
    seconds = _idle_seconds()
    return seconds is not None and seconds < RETURNED_SECONDS


def set_activity_probe(probe):
    """Register the runtime-owned, side-effect-free busy-state callback.

    Background workers used to do ``import agent`` here. When the desktop
    launcher had executed that file as ``__main__``, the import ran the whole
    assistant a second time and opened another microphone stream.
    """
    global _activity_probe
    _activity_probe = probe if callable(probe) else None


def agent_busy():
    """Whether a turn or deliberate voice recording is currently running."""
    probe = _activity_probe
    if probe is None:
        return False
    try:
        return bool(probe())
    except Exception:
        # A failed diagnostic must not let background inference compete with
        # an active turn.
        return True


def battery_ok():
    """False on a low battery that is not charging."""
    try:
        import psutil
        battery = psutil.sensors_battery()
        if battery is None or battery.power_plugged:
            return True
        return battery.percent >= MIN_BATTERY_PERCENT
    except Exception:
        return True


def interrupted():
    """Whether a running background job should stop now.

    Deliberately stricter than `allowed`: starting needs the machine to have
    been idle for a while, but stopping needs only a single keystroke.
    """
    return user_returned() or agent_busy()


def allowed(states=IDLE_STATES):
    """Whether background work may START now, and why not if it may not."""
    state = current_state()
    if state not in states:
        return False, f"the machine is {state}"
    if user_returned():
        return False, "he is using it"
    if agent_busy():
        return False, "a turn is running"
    if not battery_ok():
        return False, "the battery is low and not charging"
    return True, state


# ------------------------------------------------------------------ #
# Budgets
# ------------------------------------------------------------------ #
def _path(base_dir):
    path = os.path.join(base_dir, _FILENAME)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    return path


def _today():
    return datetime.date.today().isoformat()


def _load(base_dir):
    try:
        with open(_path(base_dir), "r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def _save(base_dir, data):
    """Atomic, so a crash mid-write cannot lose the day's count."""
    target = _path(base_dir)
    handle = tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=os.path.dirname(target), delete=False,
        suffix=".tmp")
    try:
        json.dump(data, handle, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    finally:
        handle.close()
    os.replace(handle.name, target)


def spent_today(base_dir, job, resource):
    with _LOCK:
        day = _load(base_dir).get(_today(), {})
        return int(day.get(job, {}).get(resource, 0))


def spend(base_dir, job, resource, amount=1):
    """Record use of an allowance. Old days are dropped as it goes."""
    with _LOCK:
        data = _load(base_dir)
        today = _today()
        # Only today is kept: this is a limit, not a ledger.
        data = {today: data.get(today, {})}
        bucket = data[today].setdefault(job, {})
        bucket[resource] = int(bucket.get(resource, 0)) + int(amount)
        _save(base_dir, data)
        return bucket[resource]


def within_daily(base_dir, job, resource, limit):
    return spent_today(base_dir, job, resource) < int(limit)


class Run:
    """The allowance for one run of one job.

    Counts calls and seconds, and answers "may I keep going?" in one place so
    a job does not have to remember the four things that should stop it.
    """

    def __init__(self, base_dir, job, max_calls, max_seconds,
                 daily_calls=None):
        self.base_dir = base_dir
        self.job = job
        self.max_calls = int(max_calls)
        self.max_seconds = float(max_seconds)
        self.daily_calls = daily_calls
        self.calls = 0
        self.started = time.monotonic()
        self.stopped_because = ""

    def elapsed(self):
        return time.monotonic() - self.started

    def may_continue(self):
        if interrupted():
            self.stopped_because = "he came back"
        elif self.calls >= self.max_calls:
            self.stopped_because = f"used its {self.max_calls} calls"
        elif self.elapsed() >= self.max_seconds:
            self.stopped_because = f"used its {int(self.max_seconds)} seconds"
        elif (self.daily_calls is not None
              and not within_daily(self.base_dir, self.job, "calls",
                                   self.daily_calls)):
            self.stopped_because = "reached today's allowance"
        else:
            return True
        return False

    def charge(self, calls=1):
        """Record a model call or a search against both allowances."""
        self.calls += int(calls)
        spend(self.base_dir, self.job, "calls", calls)
