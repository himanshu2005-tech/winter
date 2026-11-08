# -*- coding: utf-8 -*-
"""
identity.py -- what this assistant is called, in one place.

The name used to be spelt out in 370 places, which is why renaming it was a
day's work rather than a line. It is now read from here, so the next change is
one edit and the wake word, the window title, the spoken persona and the log
prefix cannot drift apart.

Environment variables keep the WINTER_ prefix for now. They are internal
configuration rather than the assistant's name, Himanshu's .env is full of
them, and breaking a working .env to make a prefix match a rename would be a
poor trade. `env()` reads the new spelling first and falls back, so both work
and the migration can happen whenever it is convenient.
"""

import os

NAME = os.getenv("VANTA_NAME", "Vanta")
WAKE_WORD = os.getenv("VANTA_WAKE_WORD", NAME).strip()

#: Spellings used in prose, titles and logs.
DISPLAY = NAME
UPPER = NAME.upper()
LOWER = NAME.lower()

#: Both prefixes are accepted, new one first.
_PREFIXES = ("VANTA_", "WINTER_")


def env(suffix, default=None):
    """Read a setting under either prefix.

    `env("STT")` finds VANTA_STT, then WINTER_STT, then the default. Written
    this way so the prefix migration never needs a flag day.
    """
    name = str(suffix or "").strip().upper().lstrip("_")
    for prefix in _PREFIXES:
        value = os.getenv(prefix + name)
        if value is not None and value.strip():
            return value
    return default


def env_flag(suffix, default=False):
    value = env(suffix)
    if value is None:
        return bool(default)
    return value.strip().lower() in ("1", "true", "yes", "on")
