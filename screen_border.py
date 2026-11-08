# -*- coding: utf-8 -*-
"""
screen_border.py -- the colour of the screen's edge says what VANTA is doing.

The ambient border used to exist only while listening, because drawing it
while thinking put a pulsing frame around the monitor for minutes. This brings
it back for every state without the thing that made it unbearable: it is thin,
steady, in ONE colour per state, and the only movement is a single slow comet
travelling the perimeter while work is in progress -- movement being the honest
signal that something is still happening. At rest there is nothing at all.

    colour    meaning
    blue      listening: the microphone is live
    amber     thinking: working on something
    violet    acting: a tool is running (clicking, searching, writing a file)
    green     speaking
    red       something just failed (a short flash)
    teal      Owner Override is on (the camera was told to trust it is him)
    orange    Public Space is on (private details withheld)
    crimson   Lockdown is on

Choosing which one applies is a pure function so the priorities -- an error
outranks a protocol outranks a tool outranks the plain state -- are tested
without a display.
"""

import math

COLORS = {
    "listening": "#0a84ff",
    "thinking": "#ffb020",
    "acting": "#a78bfa",
    "speaking": "#30d158",
    "error": "#ff453a",
    "owner_override": "#2dd4bf",
    "public_space": "#fb923c",
    "lockdown": "#e11d48",
}
#: Modes that draw a comet while active (work is in progress).
TRAVELLING = frozenset({"thinking", "acting"})

#: How long a tool step keeps the border violet after its last message.
ACTION_HOLD_SECONDS = 2.5
ERROR_FLASH_SECONDS = 1.4

PROTOCOL_MODES = {"owner_override": "owner_override", "public_space": "public_space",
                  "lockdown": "lockdown"}


def choose(state, now, action_until=0.0, error_until=0.0, protocol=None,
           resting=False):
    """The border mode for this instant, or None for no border."""
    if resting:
        return None
    if now < error_until:
        return "error"
    if state == "listening":
        return "listening"
    if state == "speaking":
        return "speaking"
    if state in ("thinking", "loading") or now < action_until:
        if now < action_until:
            return "acting"
        return "thinking"
    if protocol in PROTOCOL_MODES:
        return PROTOCOL_MODES[protocol]
    return None


def perimeter_point(progress, width, height, inset=2):
    """(x, y) a fraction of the way round the screen edge, clockwise from top-left."""
    w, h = width - 2 * inset, height - 2 * inset
    length = 2 * (w + h)
    distance = (progress % 1.0) * length
    if distance < w:
        return inset + distance, inset
    distance -= w
    if distance < h:
        return inset + w, inset + distance
    distance -= h
    if distance < w:
        return inset + w - distance, inset + h
    distance -= w
    return inset, inset + h - distance


def comet(progress, width, height, length=0.09, steps=22):
    """Points from tail to head of a comet ending at `progress`."""
    return [perimeter_point(progress - length * (1 - i / steps), width, height)
            for i in range(steps + 1)]


def breath(t, speed=0.9):
    """0..1, slowly."""
    return (math.sin(t * speed) + 1) / 2
