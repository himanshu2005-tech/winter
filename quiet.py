# -*- coding: utf-8 -*-
"""
quiet.py -- "I'm napping, stay quiet" as something the system enforces.

Himanshu said he was sleeping for ten minutes. VANTA answered "I'll stay quiet
until then", set a timer, and meant it -- and then a proactive event fired and it
read him an email while he slept. The promise was real and there was nothing
behind it: silence lived in one sentence of one reply, and the ambient watcher,
the commitment monitor and the briefing scheduler had never heard of it.

So silence is state, and the things that speak unbidden ask before they do.

WHAT IS SUPPRESSED, AND WHAT IS NOT
-----------------------------------
Suppressed: anything VANTA decided to say on its own -- briefings, ambient
observations, inference chatter, email notices. Nobody asked for those now.

NOT suppressed:

* alarms and timers, including the one that ends the nap. Silencing the thing
  he asked to be woken by would be obeying the letter of "be quiet" and
  betraying the point of it.
* anything he says himself. Speaking to VANTA while it is being quiet is a
  request, and a request is always answered.

A window always has an end. There is no indefinite quiet: a mode that outlives
the situation it was set for stops being a preference and becomes a bug, which
is what happened to every sticky mode this project used to have.
"""

import threading
import time

MAX_MINUTES = 240.0

_lock = threading.RLock()
_until = 0.0
_reason = ""


def begin(minutes, reason=""):
    """Be quiet for this many minutes. Returns the wall-clock end time."""
    global _until, _reason
    try:
        span = float(minutes)
    except (TypeError, ValueError):
        span = 0.0
    span = max(0.0, min(span, MAX_MINUTES))
    if span <= 0:
        return None
    with _lock:
        _until = time.time() + span * 60.0
        _reason = str(reason or "").strip()
        ends = _until
    print(f"[Quiet] Nothing unprompted for {span:g} minutes"
          + (f" ({_reason})" if _reason else "") + ".")
    return ends


def clear():
    global _until, _reason
    with _lock:
        was = _until > time.time()
        _until, _reason = 0.0, ""
    if was:
        print("[Quiet] Speaking freely again.")


def active():
    with _lock:
        return _until > time.time()


def reason():
    with _lock:
        return _reason if _until > time.time() else ""


def remaining_seconds():
    with _lock:
        return max(0.0, _until - time.time())


def describe():
    """One line for the model, so it knows it is meant to be quiet."""
    left = remaining_seconds()
    if left <= 0:
        return ""
    minutes = int(round(left / 60.0)) or 1
    why = reason()
    return (f"--- QUIET ---\nHimanshu asked for quiet for about another "
            f"{minutes} minute(s)" + (f" ({why})" if why else "") + ". Say "
            f"nothing unprompted until then. Alarms and timers he set still "
            f"fire, and anything he asks you directly is still answered.")
