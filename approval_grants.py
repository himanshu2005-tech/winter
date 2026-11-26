# -*- coding: utf-8 -*-
"""
approval_grants.py -- letting a corrected command inherit the approval it earned.

An approved action that fails is handed back so the model can fix the command
and run it again. That was useless on its own: the retry is the same gated
action type, so it queued for approval a second time, and Himanshu had to
approve the same intent twice -- once for the command that was broken, once for
the command that worked. Telling the model "the approval still stands" changes
nothing, because the gate is not listening to the model.

So the approval is carried forward explicitly, and narrowly. What a grant
permits:

* the SAME action type he approved, and nothing else
* ONCE -- it is consumed on use, so a second failure needs him again
* within a few minutes, because consent to fix a command now is not consent to
  run that kind of command this evening

What it deliberately does not do is let the model approve itself. A grant only
exists because Himanshu approved something that then failed; nothing in a
model's output can create one. The narrowness is the point: he consented to a
purpose, and a typo should not send him back to the prompt, but "fix it" is not
"do whatever you like with this action type".

Every use is printed, so a grant is never invisible.
"""

import threading
import time

#: How long a corrected retry may inherit the approval.
TTL_SECONDS = 300.0
#: How many retries one approval funds. One: if the fix also fails, the model is
#: guessing, and guessing is exactly when a human should be back in the loop.
MAX_USES = 1

_lock = threading.RLock()
_grants = {}


def grant(action_type, reason="", ttl=None):
    """Record that a corrected retry of this action type may skip approval."""
    key = str(action_type or "").upper()
    if not key:
        return False
    with _lock:
        _grants[key] = {
            "expires": time.time() + float(ttl or TTL_SECONDS),
            "uses": MAX_USES,
            "reason": reason,
        }
    print(f"[Approvals] {key} may be retried once without re-approval "
          f"for {int(ttl or TTL_SECONDS)}s ({reason or 'it failed after approval'}).")
    return True


def consume(action_type):
    """Whether this action may run on a previous approval, spending the grant.

    Consumed on the check rather than on success, because a grant that survived
    failed attempts would keep letting a model try variations on one yes.
    """
    key = str(action_type or "").upper()
    with _lock:
        entry = _grants.get(key)
        if not entry:
            return False
        if entry["expires"] < time.time():
            _grants.pop(key, None)
            print(f"[Approvals] The retry window for {key} had already closed; "
                  f"asking again.")
            return False
        entry["uses"] -= 1
        reason = entry.get("reason") or ""
        if entry["uses"] <= 0:
            _grants.pop(key, None)
    print(f"[Approvals] {key} is running on the approval already given "
          f"({reason}). Any further attempt needs Himanshu again.")
    return True


def active(action_type):
    """Whether a grant exists, without spending it. For tests and reporting."""
    key = str(action_type or "").upper()
    with _lock:
        entry = _grants.get(key)
        if not entry:
            return False
        if entry["expires"] < time.time():
            _grants.pop(key, None)
            return False
        return entry["uses"] > 0


def revoke(action_type=None):
    """Drop one grant, or all of them."""
    with _lock:
        if action_type is None:
            _grants.clear()
            return
        _grants.pop(str(action_type or "").upper(), None)
