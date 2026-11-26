# -*- coding: utf-8 -*-
"""
when.py -- saying when something is, so nothing has to work it out.

A calendar line used to reach the model as a raw timestamp:

    - [abc123] 2026-09-22T07:30:00+05:30: TQM exam reminder

and the current time arrived separately, in a different format, as prose. To
answer "is that today?" the model had to parse both, reconcile the offsets and
do the subtraction -- and it got it wrong, announcing a reminder that had
already passed that morning as being due tomorrow. Nothing about that is
surprising: it is arithmetic, done by inference, on two formats that do not
match.

So the arithmetic happens here instead, and the line the model reads already
says what it means:

    - [abc123] today 07:30 (8 hours ago): TQM exam reminder

The relative phrase is deliberately included alongside the day. "Today 07:30"
at three in the afternoon is still ambiguous about whether it has happened;
"8 hours ago" is not.
"""

import datetime
import re

_WEEKDAY_FORMAT = "%a %d %b"


def parse(value, default_tz=None):
    """Parse a calendar timestamp. Returns an aware datetime, or None.

    Google returns either a full timestamp for a timed event or a bare date
    for an all-day one, and both arrive as strings.
    """
    if isinstance(value, datetime.datetime):
        moment = value
    elif isinstance(value, datetime.date):
        moment = datetime.datetime.combine(value, datetime.time.min)
    else:
        text = str(value or "").strip()
        if not text:
            return None
        # fromisoformat before 3.11 refuses the trailing Z; normalise it.
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            moment = datetime.datetime.fromisoformat(text)
        except ValueError:
            try:
                moment = datetime.datetime.combine(
                    datetime.date.fromisoformat(text[:10]), datetime.time.min)
            except ValueError:
                return None
    if moment.tzinfo is None:
        moment = moment.replace(
            tzinfo=default_tz or datetime.datetime.now().astimezone().tzinfo)
    return moment


def day_name(moment, now=None):
    """'today', 'tomorrow', 'yesterday', or a dated weekday."""
    now = now or datetime.datetime.now().astimezone()
    if moment.tzinfo is not None and now.tzinfo is None:
        now = now.astimezone()
    days = (moment.astimezone(now.tzinfo).date() - now.date()).days
    if days == 0:
        return "today"
    if days == 1:
        return "tomorrow"
    if days == -1:
        return "yesterday"
    return moment.astimezone(now.tzinfo).strftime(_WEEKDAY_FORMAT)


def relative(moment, now=None):
    """'in 20 minutes', '8 hours ago', 'in 3 days'."""
    now = now or datetime.datetime.now().astimezone()
    seconds = (moment.astimezone(now.tzinfo) - now).total_seconds()
    past = seconds < 0
    seconds = abs(seconds)

    if seconds < 90:
        phrase = "now"
        return phrase
    # Round first, then pluralise. The other way round -- "n units if n >= 2
    # else 1 unit" -- forced everything below two into the singular, so an
    # exam 1.76 days away was announced as "in 1 day". The model noticed
    # before Himanshu did: "Saturday to Monday is 2 days, but the system says
    # in 1 day", and then obeyed the instruction not to recompute.
    if seconds < 3600:
        phrase = f"{int(round(seconds / 60))} minutes"
    elif seconds < 86400:
        count = int(round(seconds / 3600))
        phrase = "1 hour" if count == 1 else f"{count} hours"
    else:
        count = int(round(seconds / 86400))
        phrase = "1 day" if count == 1 else f"{count} days"
    return f"{phrase} ago" if past else f"in {phrase}"


def describe(value, now=None, all_day=False):
    """The phrase that goes in front of an event. Never raises.

    Returns something like "today 07:30 (8 hours ago)". If the timestamp
    cannot be parsed the original string comes back untouched, because a
    slightly ugly line is better than a missing event.
    """
    moment = parse(value)
    if moment is None:
        return str(value)
    now = now or datetime.datetime.now().astimezone()
    day = day_name(moment, now)
    if all_day or len(str(value).strip()) <= 10:
        return f"{day} (all day)" if day in ("today", "tomorrow", "yesterday") \
            else f"{day}, all day"
    local = moment.astimezone(now.tzinfo)
    clock = local.strftime("%H:%M")
    # "Tomorrow at midnight" is routinely misunderstood as the end of
    # tomorrow.  A 00:00 calendar timestamp is the very start of that date;
    # put that fact in the deterministic context instead of asking the model
    # to explain the convention correctly each time.
    boundary = " (start of day)" if local.hour == 0 and local.minute == 0 else ""
    return f"{day} {clock}{boundary} ({relative(moment, now)})"


def is_past(value, now=None):
    moment = parse(value)
    if moment is None:
        return False
    now = now or datetime.datetime.now().astimezone()
    return moment.astimezone(now.tzinfo) < now


# --------------------------------------------------------------------- #
# Anchoring relative day words in text that outlives the day
# --------------------------------------------------------------------- #
# A fact stored as "the exam is tomorrow" is true for one day and wrong for
# every day after. VANTA kept announcing a reminder as due tomorrow because a
# note written the evening before said so, and that note was injected into
# every turn afterwards. Anything written to long-term storage gets its day
# words resolved to a date first.

# Longest phrases first: "the day after tomorrow" must not be read as
# "tomorrow" with three stray words in front of it.
_DAY_WORDS = (
    ("the day after tomorrow", 2, ""),
    ("the day before yesterday", -2, ""),
    ("tomorrow morning", 1, "the morning of "),
    ("tomorrow evening", 1, "the evening of "),
    ("tomorrow night", 1, "the night of "),
    ("yesterday morning", -1, "the morning of "),
    ("yesterday evening", -1, "the evening of "),
    ("this morning", 0, "the morning of "),
    ("this afternoon", 0, "the afternoon of "),
    ("this evening", 0, "the evening of "),
    ("tonight", 0, "the night of "),
    ("tomorrow", 1, ""),
    ("yesterday", -1, ""),
    ("today", 0, ""),
)


def anchor(text, moment=None):
    """Rewrite relative day words in *text* as dates, relative to *moment*.

    "the exam is tomorrow", written on the 21st, becomes "the exam is Tue 22
    Sep 2026" -- which still reads correctly a week later. Returns the text
    unchanged when there is nothing to resolve.
    """
    if not text:
        return text
    moment = moment or datetime.datetime.now().astimezone()
    if isinstance(moment, str):
        moment = parse(moment) or datetime.datetime.now().astimezone()

    result = text
    for phrase, offset, prefix in _DAY_WORDS:
        if phrase not in result.lower():
            continue
        day = (moment + datetime.timedelta(days=offset)).strftime("%a %d %b %Y")
        result = re.sub(r"\b" + re.escape(phrase) + r"\b",
                        prefix + day, result, flags=re.IGNORECASE)
    return result
