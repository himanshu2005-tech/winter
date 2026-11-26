# -*- coding: utf-8 -*-
"""
schedule.py -- what is already in the window someone just referred to.

"I'm going to study tomorrow morning" and "there is a film at 9am tomorrow"
were two facts VANTA held in different places and never put together. The
calendar was read only when an action read it, so tomorrow's events were not
in front of the model at all when the sentence arrived; tracked commitments
were, but nothing asked whether a stated plan collided with one. The reply was
"nice, what are you studying?" and the clash surfaced the next morning.

Noticing that is two jobs, and they belong on different sides of the line.

    Which window did he mean, and what is already in it?   -> arithmetic
    Is that a problem, and what should be said about it?   -> judgment

So this module does the first and nothing of the second. It resolves the
window a sentence refers to, finds what is already booked inside it, and
states both as facts. It never decides that a clash matters, never ranks a
film against revision, and contains no rule about what to say -- a module that
decided "warn when the words study and film appear together" would be a
thousand hand-written rules that each cover one sentence and get the
thousand-and-first wrong.

This is the same split `when.py` draws for a single timestamp. The model got
"is that today?" wrong because it was being asked to do arithmetic by
inference; this is the same error one level up, over an interval instead of an
instant.

NOTHING HERE IS A PERMISSION
---------------------------
The window comes from what Himanshu said, which is argument text and can never
create an executable outcome. It is used to look things up and never to decide
that something may run.
"""

import datetime
import re
import threading

import when

#: What parts of a day mean, in local hours. Deliberately generous at the
#: edges: "morning" has to cover an 11:30 film, because being told about a
#: clash half an hour outside a guessed boundary is useful and being told
#: nothing is not.
DAY_PARTS = {
    "morning": (6, 12),
    "afternoon": (12, 17),
    "evening": (17, 22),
    "night": (20, 24),
    "tonight": (17, 24),
    "tonite": (17, 24),
}

_PART_WORDS = re.compile(
    r"\b(morning|afternoon|evening|tonight|tonite|night)\b", re.IGNORECASE)

#: "tomorrow" and the shorthands he actually writes. Shared vocabulary with
#: outcome_compiler, which learned the hard way that "tmr" dropped the day
#: entirely and sent a Saturday reminder to an alarm that fired next morning.
_RELATIVE_DAYS = {
    "today": 0, "tonight": 0, "tonite": 0,
    "tomorrow": 1, "tomorow": 1, "tmrw": 1, "tmr": 1, "tmw": 1,
    "tomo": 1, "2moro": 1, "tmrrw": 1,
    "yesterday": -1,
}

_WEEKDAYS = {
    "monday": 0, "mon": 0, "tuesday": 1, "tues": 1, "tue": 1,
    "wednesday": 2, "weds": 2, "wed": 2, "thursday": 3, "thurs": 3,
    "thur": 3, "thu": 3, "friday": 4, "fri": 4, "saturday": 5, "sat": 5,
    "sunday": 6, "sun": 6,
}

_DAY_WORDS = re.compile(
    r"\b(" + "|".join(sorted(set(_RELATIVE_DAYS) | set(_WEEKDAYS),
                             key=len, reverse=True)) + r")\b", re.IGNORECASE)

#: An explicit clock time: "at 9", "at 9am", "9:30 pm", "by 18:00".
_CLOCK = re.compile(
    r"\b(?:at|by|from|around|about)?\s*"
    r"(\d{1,2})(?::(\d{2}))?\s*(am|pm|a\.m\.|p\.m\.)?\b", re.IGNORECASE)

#: How wide to make the window around a bare clock time. An hour and a half
#: each way: the question is "is something else happening around then", and a
#: film that starts forty minutes after he sits down to study is the answer.
CLOCK_MARGIN_HOURS = 1.5

#: How long a calendar read stays fresh. A conversation must not become one
#: API call per sentence, and a schedule does not change minute to minute.
CACHE_SECONDS = 300.0

_CACHE_LOCK = threading.Lock()
_CACHE = {"at": 0.0, "events": None}


class Window:
    """A stretch of time someone referred to, and how they referred to it."""

    __slots__ = ("start", "end", "said")

    def __init__(self, start, end, said):
        self.start = start
        self.end = end
        self.said = said

    def __repr__(self):
        return f"Window({self.said!r}, {self.start.isoformat()} .. {self.end.isoformat()})"

    def contains(self, moment):
        return self.start <= moment < self.end

    def describe(self):
        # The end date is shown whenever it is not the start's, or a whole day
        # reads as "Thu 15 Oct 00:00 to 00:00" -- which looks like an empty
        # window rather than all of Thursday.
        if self.end.date() != self.start.date():
            tail = self.end.strftime("%a %d %b %H:%M")
        else:
            tail = self.end.strftime("%H:%M")
        return f"{self.said} ({self.start.strftime('%a %d %b %H:%M')} to {tail})"


def window_for(text, now=None):
    """The stretch of time a sentence refers to, or None.

    Returns None rather than guessing when no day and no clock are mentioned.
    A sentence with no time in it is not a plan about a window, and treating
    "I should study more" as being about this afternoon would make every
    second turn carry a schedule nobody asked about.
    """
    said = str(text or "")
    moment = now or datetime.datetime.now()

    day_match = _DAY_WORDS.search(said)
    part_match = _PART_WORDS.search(said)
    clock = _clock_in(said)

    if not day_match and not part_match and clock is None:
        return None

    # Which day.
    offset = 0
    label_day = "today"
    if day_match:
        word = day_match.group(1).lower()
        label_day = word
        if word in _RELATIVE_DAYS:
            offset = _RELATIVE_DAYS[word]
        else:
            target = _WEEKDAYS[word]
            offset = (target - moment.weekday()) % 7
            # "Monday" said on a Monday means the next one, unless it is
            # qualified -- "this Monday" is today.
            if offset == 0 and "this" not in said.lower():
                offset = 7
    day = (moment + datetime.timedelta(days=offset)).replace(
        hour=0, minute=0, second=0, microsecond=0)

    # Which part of it.
    if clock is not None:
        centre = day.replace(hour=clock[0], minute=clock[1])
        margin = datetime.timedelta(hours=CLOCK_MARGIN_HOURS)
        said_label = f"{label_day} around {centre.strftime('%H:%M')}"
        return Window(centre - margin, centre + margin, said_label)

    if part_match:
        part = part_match.group(1).lower()
        first, last = DAY_PARTS[part]
        start = day + datetime.timedelta(hours=first)
        end = day + datetime.timedelta(hours=last)
        # "in the morning", said at eight in the evening, means tomorrow
        # morning. Without this it resolved to a window that closed eight
        # hours ago, found nothing in it, and said nothing -- which is the
        # same as not looking. Only when no day was named: "this morning" said
        # in the evening really is about this morning.
        # "this morning" anchors to today however late it is said, and "this"
        # is not a day word, so it has to be looked for next to the part.
        anchored = re.search(r"\bthis\s+" + re.escape(part), said, re.IGNORECASE)
        if not day_match and not anchored and end <= moment:
            start += datetime.timedelta(days=1)
            end += datetime.timedelta(days=1)
            label_day = "tomorrow"
        # "tonight" is both the day and the part of it, and naming it twice
        # reads as a stutter.
        label = part if label_day == part else f"{label_day} {part}"
        return Window(start, end, label.strip())

    # A day with no part named: the whole of it.
    return Window(day, day + datetime.timedelta(days=1), label_day)


def _clock_in(said):
    """An explicit clock time in the sentence, as (hour, minute), or None."""
    for match in _CLOCK.finditer(said):
        raw_hour, raw_minute, meridiem = match.groups()
        try:
            hour = int(raw_hour)
        except (TypeError, ValueError):
            continue
        minute = int(raw_minute or 0)
        if minute > 59:
            continue
        suffix = (meridiem or "").replace(".", "").lower()
        if suffix == "pm" and hour < 12:
            hour += 12
        elif suffix == "am" and hour == 12:
            hour = 0
        elif not suffix:
            # A bare number is only a time when it could be one. "20 roles"
            # and "5 files" are not nine o'clock, so a number without am/pm
            # and without a colon is only read as a clock when something in
            # the sentence positions it as one.
            if raw_minute is None and not re.search(
                    r"\b(?:at|by|from|around|about)\s*" + re.escape(raw_hour),
                    said, re.IGNORECASE):
                continue
        if 0 <= hour <= 23:
            return hour, minute
    return None


# ------------------------------------------------------------------ #
# What is already booked
# ------------------------------------------------------------------ #
def _calendar_events(base_dir, days_ahead=8):
    """Upcoming events, cached, and never allowed to break a turn.

    Reading the calendar is a network call. On the conversational path that
    has to be cheap and it has to be optional: a turn where Google is slow or
    signed out must answer normally, without a schedule, rather than wait.
    """
    import time as _time
    with _CACHE_LOCK:
        if (_CACHE["events"] is not None
                and _time.time() - _CACHE["at"] < CACHE_SECONDS):
            return _CACHE["events"]
    events = []
    try:
        from providers.google_api import get_credentials
        from googleapiclient.discovery import build
        credentials = get_credentials(base_dir)
        service = build("calendar", "v3", credentials=credentials)
        now = datetime.datetime.utcnow()
        result = service.events().list(
            calendarId="primary",
            timeMin=now.isoformat() + "Z",
            timeMax=(now + datetime.timedelta(days=days_ahead)).isoformat() + "Z",
            maxResults=50, singleEvents=True, orderBy="startTime").execute()
        for item in result.get("items", []):
            start = item["start"].get("dateTime") or item["start"].get("date")
            end = item["end"].get("dateTime") or item["end"].get("date")
            if not start:
                continue
            events.append({
                "what": item.get("summary", "an untitled event"),
                "start": start,
                "end": end,
                "all_day": "T" not in str(start),
                "source": "calendar",
            })
    except Exception as exc:
        print(f"[Schedule] Calendar unavailable ({exc}); "
              f"working without it this turn.")
        events = []
    with _CACHE_LOCK:
        _CACHE["events"] = events
        _CACHE["at"] = _time.time()
    return events


def invalidate_cache():
    """Forget the cached calendar. Called after VANTA changes it itself."""
    with _CACHE_LOCK:
        _CACHE["events"] = None
        _CACHE["at"] = 0.0


def _commitment_items(base_dir):
    try:
        import commitments
        rows = []
        for item in commitments._load(base_dir):
            if item.get("status") not in (None, "", "active"):
                continue
            hard = item.get("hard_time")
            if not hard:
                continue
            rows.append({
                "what": item.get("what", "a commitment"),
                "start": hard,
                "end": hard,
                "all_day": False,
                "source": "commitment",
            })
        return rows
    except Exception as exc:
        print(f"[Schedule] Commitments unavailable ({exc}).")
        return []


def booked_in(base_dir, window, now=None):
    """Everything already in this window, from the calendar and commitments.

    Pure interval arithmetic over times `when.parse` has already normalised,
    because comparing a timezone-bearing ISO string against a local datetime
    by inference is exactly the mistake when.py exists to prevent.
    """
    if window is None:
        return []
    found = []
    for item in list(_calendar_events(base_dir)) + _commitment_items(base_dir):
        start = when.parse(item["start"])
        if start is None:
            continue
        end = when.parse(item.get("end")) or start
        start = _naive(start)
        end = _naive(end)
        if item.get("all_day"):
            # An all-day entry has no clock, so it covers the whole of its day
            # rather than the midnight it parses to.
            end = max(end, start + datetime.timedelta(days=1))
        if end <= start:
            end = start + datetime.timedelta(minutes=30)
        if start < window.end and window.start < end:
            found.append(dict(item, parsed_start=start, parsed_end=end))
    found.sort(key=lambda row: row["parsed_start"])
    return found


def _naive(moment):
    """Drop the offset once it has been applied, so intervals compare."""
    if moment.tzinfo is not None:
        moment = moment.astimezone().replace(tzinfo=None)
    return moment


def describe_booked(items):
    """The clashing entries, each already saying when it is."""
    lines = []
    for item in items:
        stamp = when.describe(item["start"], all_day=item.get("all_day", False))
        source = "" if item["source"] == "calendar" else f" ({item['source']})"
        lines.append(f"  - {stamp}: {item['what']}{source}")
    return "\n".join(lines)


def conflict_note(base_dir, text, now=None):
    """What the model is told when a sentence lands on a booked window.

    Facts only. It states the window the sentence referred to and what is
    already in it, and says explicitly that whether this matters is a
    judgment -- because a film during revision is worth mentioning, and a
    reminder to drink water is not, and no rule written here could tell them
    apart.
    """
    window = window_for(text, now=now)
    if window is None:
        return ""
    items = booked_in(base_dir, window, now=now)
    if not items:
        return ""
    return (
        "ALREADY IN THAT WINDOW. He just referred to "
        f"{window.describe()}, and this is already booked then:\n"
        + describe_booked(items)
        + "\nSay so if it genuinely collides with what he just described, in "
          "one line, before going along with the plan. If it does not collide "
          "-- a reminder, something he is obviously already counting on, "
          "something that finishes before he starts -- do not mention it. "
          "This is a fact about his calendar, not an instruction about what "
          "to do with it."
    )
