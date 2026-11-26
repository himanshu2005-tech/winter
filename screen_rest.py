# -*- coding: utf-8 -*-
"""
screen_rest.py -- noticing that nobody is there, and letting the screen rest.

Himanshu fell asleep working. The screen stayed on all night, full of the
thing he had been doing, and was still on in the morning. Nothing in VANTA
noticed: it watched the screen for what was on it and never for how long it
had been the same.

So: when the machine has had no input for a while AND the screen has not
changed, black it out, put VANTA's mark on it, and go quiet. Any key, any
movement, and it is gone.

WHY BOTH CONDITIONS
-------------------
Input idleness alone is wrong, and wrong in the most annoying possible way.
Watching a film is forty minutes of no keyboard and no mouse, and blacking
the screen out in the middle of one would be worse than the problem this
solves. A film changes every frame; a document someone fell asleep over does
not. So the screen has to be still as well as the hands.

RESTING IS NOT STANDBY, AND NOT SLEEP
-------------------------------------
Three different things, and they were worth keeping separate:

    runtime_power STANDBY   the intelligence pipeline is off entirely; only
                            the local wake word and the alarm scheduler run
    WinterMode SLEEP        a mode: how VANTA helps, at night
    resting (this)          the screen is dark and VANTA is silent, and
                            everything behind it carries on

Manual standby deliberately reuses this module's dark visual surface, but
runtime_power still owns whether background intelligence is paused. Sharing
the presentation does not collapse the lifecycle states.

Resting is the one he asked for: "it doesn't disturb me but record emails and
other stuff". So it does not touch runtime_power at all. Silence comes from
`quiet`, which already exists and which the ambient watcher, the commitment
monitor and the briefing scheduler already consult -- the whole point of that
module being that a promise of silence is state rather than a sentence in one
reply. A second suppression path here would be a promise the workers had
never heard of, which is the exact bug quiet.py was written to fix.
"""

import ctypes
import os
import threading
import time


def _configured_idle_minutes():
    raw = os.getenv(
        "VANTA_STANDBY_IDLE_MINUTES",
        os.getenv("WINTER_STANDBY_IDLE_MINUTES", "20"),
    )
    try:
        value = float(raw)
        return value if value > 0 else 20.0
    except (TypeError, ValueError):
        print(f"[Rest] Invalid standby idle interval {raw!r}; using 20 minutes.")
        return 20.0


#: How long without a keystroke or a mouse move before the screen is a
#: candidate for resting.
IDLE_MINUTES = _configured_idle_minutes()

#: How different two frames may be and still count as the same screen. A
#: blinking cursor, a clock in the tray and a progress spinner all move a few
#: pixels; a video moves most of them.
STILL_THRESHOLD = 0.02

#: How far apart the two frames are taken. Long enough that a slow animation
#: is caught, short enough not to delay resting by much.
STILL_GAP_SECONDS = 6.0

#: How often to look. Cheap: a tick is one integer from the OS, and the
#: screen is only sampled once that integer is large enough to matter.
POLL_SECONDS = 30.0

#: Downsample before comparing. A full 1920x1080 difference is eight million
#: subtractions to answer a yes/no question; a thumbnail answers it the same
#: way for a thousandth of the work.
COMPARE_SIZE = (64, 36)

_LOCK = threading.RLock()
_resting = False
#: The screen is dark because he is asleep, not because it has been idle. That
#: darkness is held: a nudged mouse or a vibrating desk must not light a room
#: he is sleeping in.
_sleeping = False
_standby = False
_resting_since = 0.0
_suspended_until = 0.0


def idle_seconds():
    """Seconds since the last keyboard or mouse input, or None off Windows."""
    class LASTINPUTINFO(ctypes.Structure):
        _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]
    try:
        info = LASTINPUTINFO()
        info.cbSize = ctypes.sizeof(info)
        ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info))
        ticks = ctypes.windll.kernel32.GetTickCount64()
        return max(0.0, (ticks - info.dwTime) / 1000.0)
    except Exception:
        return None


def _thumbnail():
    from PIL import ImageGrab
    frame = ImageGrab.grab()
    return frame.convert("L").resize(COMPARE_SIZE)


def frame_difference(first, second):
    """How much two frames differ, 0.0 to 1.0. Pure, so it can be checked."""
    a, b = list(first.getdata()), list(second.getdata())
    if not a or len(a) != len(b):
        return 1.0
    return sum(abs(x - y) for x, y in zip(a, b)) / (len(a) * 255.0)


def screen_is_still(gap_seconds=None):
    """Whether the screen is the same picture it was a few seconds ago."""
    gap = STILL_GAP_SECONDS if gap_seconds is None else gap_seconds
    try:
        first = _thumbnail()
        time.sleep(max(0.0, gap))
        second = _thumbnail()
    except Exception as exc:
        print(f"[Rest] Could not sample the screen ({exc}); not resting.")
        return False
    return frame_difference(first, second) <= STILL_THRESHOLD


def is_resting():
    with _LOCK:
        return _resting


def is_sleeping():
    """Whether the dark screen is the sleep-mode one."""
    with _LOCK:
        return _resting and _sleeping


def is_standby_screen():
    with _LOCK:
        return _resting and _standby


def resting_since():
    with _LOCK:
        return _resting_since


def suspend(minutes=60.0):
    """Do not rest for a while -- for a film, or after he dismisses it."""
    global _suspended_until
    with _LOCK:
        _suspended_until = time.monotonic() + max(0.0, minutes) * 60.0


def suspended():
    with _LOCK:
        return time.monotonic() < _suspended_until


def should_rest(idle=None, still=None):
    """The decision, with both inputs injectable so it can be tested.

    Separated from the sampling because the decision is the part worth being
    sure about, and the part that needs a real screen is the part that cannot
    be checked in a test.
    """
    if is_resting() or suspended():
        return False
    seconds = idle_seconds() if idle is None else idle
    if seconds is None or seconds < IDLE_MINUTES * 60.0:
        return False
    return screen_is_still() if still is None else bool(still)


def should_verify_identity(idle=None, still=None, already_checked=False,
                           minutes=None):
    """Whether this idle epoch warrants one fresh local face check."""
    if already_checked:
        return False
    seconds = idle_seconds() if idle is None else idle
    threshold = (float(os.getenv("VANTA_IDENTITY_STILL_MINUTES", "5"))
                 if minutes is None else float(minutes))
    if seconds is None or seconds < max(1.0, threshold) * 60.0:
        return False
    return screen_is_still() if still is None else bool(still)


def begin(ui_queue, reason="the screen has been untouched", sleeping=False,
          standby=False):
    """Darken the screen and go quiet. Returns whether anything changed.

    `sleeping` is the sleep-mode variant: fully black, nothing drawn on it,
    and not woken by mouse movement.
    """
    global _resting, _resting_since, _sleeping, _standby
    standby = bool(standby or str(reason or "").lower().startswith("standby"))
    with _LOCK:
        if _resting:
            if (sleeping and not _sleeping) or (standby and not _standby):
                # Already dark from being idle; promote the same surface to
                # the explicitly held sleep/standby state.
                _sleeping = bool(sleeping)
                _standby = bool(standby)
                if ui_queue is not None:
                    ui_queue.put(("rest_begin", "standby mode" if standby
                                  else "sleep mode"))
                return True
            return False
        _resting = True
        _sleeping = bool(sleeping)
        _standby = bool(standby)
        _resting_since = time.time()
    try:
        import quiet
        # Open-ended in practice: it is cleared the moment he touches
        # anything. The silence lives in quiet rather than here so that the
        # workers which already ask quiet whether they may speak keep
        # working without being taught about a second flag.
        quiet.begin(12 * 60, reason=reason)
    except Exception as exc:
        print(f"[Rest] Could not go quiet ({exc}); resting anyway.")
    if ui_queue is not None:
        ui_queue.put(("rest_begin", reason))
    print(f"[Rest] Screen resting: {reason}.")
    return True


def end(ui_queue, reason="input"):
    """Bring the screen back. Returns whether anything changed."""
    global _resting, _sleeping, _standby
    with _LOCK:
        if not _resting:
            return False
        _resting = False
        _sleeping = False
        _standby = False
    try:
        import quiet
        if quiet.active():
            quiet.clear()
    except Exception:
        pass
    if ui_queue is not None:
        ui_queue.put(("rest_end", reason))
    print(f"[Rest] Screen back ({reason}).")
    return True


def worker(ui_queue, poll_seconds=POLL_SECONDS, on_still=None):
    """Watch for a screen nobody is using, and for its owner coming back."""
    print(f"[Rest] Watching for a still screen after {IDLE_MINUTES:.0f} "
          f"minutes idle.")
    still_check_done = False
    identity_minutes = float(os.getenv("VANTA_IDENTITY_STILL_MINUTES", "5"))
    while True:
        try:
            seconds = idle_seconds()
            if seconds is not None and seconds < 2.0:
                still_check_done = False
            if (on_still is not None and should_verify_identity(
                    idle=seconds, already_checked=still_check_done,
                    minutes=identity_minutes)):
                # One bounded check per idle epoch. Videos and animation fail
                # this test, while a genuinely unattended static screen asks
                # the camera worker for one fresh owner verification.
                on_still("screen still for five minutes")
                still_check_done = True
            if is_resting():
                # Any input at all ends it. The threshold is deliberately
                # tiny rather than zero: GetLastInputInfo has a tick of
                # granularity, and waking on its own rounding would make the
                # blackout flicker.
                # A sleeping screen ends when he leaves sleep mode or presses
                # a key on it (see rest_screen), never on a bumped mouse.
                if (not is_sleeping() and not is_standby_screen()
                        and seconds is not None
                        and seconds < 2.0):
                    end(ui_queue, "he came back")
                    # Do not re-darken the instant he stops typing to read
                    # something.
                    suspend(minutes=IDLE_MINUTES)
            elif should_rest():
                begin(ui_queue)
        except Exception as exc:
            print(f"[Rest] Watcher error: {exc}")
        time.sleep(max(2.0, float(poll_seconds)))
