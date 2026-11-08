# -*- coding: utf-8 -*-
"""
turn_context.py -- where the current turn came from, and therefore where it goes.

Routing the final reply to Telegram was not enough. A turn says several things
before it finishes: an acknowledgement, a line before a long action, a note that
something is waiting. Each of those went through speak() directly, so a request
sent from a phone still talked to an empty room three or four times and only the
last sentence went to Telegram.

Patching every call site would mean finding every call site, now and forever.
Instead the turn records its own channel, and the things that reach outward --
speech, and approval prompts -- ask. One place to set, one place to read.

Thread-local because each turn runs on its own thread: a Telegram turn must not
silence the speaker for a voice turn happening beside it.
"""

import threading

_state = threading.local()

LOCAL = "local"
TELEGRAM = "telegram"
MOBILE = "mobile"


def begin(channel, origin=None):
    """Mark the channel this turn arrived on."""
    _state.channel = channel or LOCAL
    _state.origin = str(origin or "user")
    _state.guest = False
    _state.guest_reason = ""


def end():
    _state.channel = LOCAL
    _state.origin = "user"
    _state.guest = False
    _state.guest_reason = ""


def set_guest(is_guest, reason=""):
    """Record that this turn is answering someone who is not confirmed as him.

    A guest turn must not see, say or do anything personal. The decision is
    made once, early, and every layer that could leak -- the prompt, the
    retrieved context, the action authoriser -- reads it from here rather than
    each making its own guess, which is how a guest session came to have the
    personal profile in its system prompt while its context said "unavailable".
    """
    _state.guest = bool(is_guest)
    _state.guest_reason = str(reason or "") if is_guest else ""


def is_guest():
    return bool(getattr(_state, "guest", False))


def guest_reason():
    return getattr(_state, "guest_reason", "")


def channel():
    return getattr(_state, "channel", LOCAL)


def is_remote():
    """Whether the person asking is somewhere other than at this machine.

    The laptop speaker is the wrong output for a remote turn twice over: he
    cannot hear it, and whoever is near the laptop can.
    """
    return channel() != LOCAL


def origin():
    return getattr(_state, "origin", "user")


def is_proactive():
    return origin() == "proactive"


def detect(text):
    """The channel a raw input string implies, without consuming the prefix."""
    if str(text or "").lstrip().startswith("[Telegram]:"):
        return TELEGRAM
    if str(text or "").lstrip().startswith("[Mobile]:"):
        return MOBILE
    return LOCAL


def strip_prefix(text):
    """The text without its channel marker.

    The approvals fast-path compared the raw string against "approve", so
    "[Telegram]: approve" matched nothing and approving from a phone silently
    did nothing at all.
    """
    raw = str(text or "")
    stripped = raw.lstrip()
    if stripped.startswith("[Telegram]:"):
        return stripped[len("[Telegram]:"):].strip()
    if stripped.startswith("[Mobile]:"):
        return stripped[len("[Mobile]:"):].strip()
    return raw
