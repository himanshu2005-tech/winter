# -*- coding: utf-8 -*-
"""
settings.py -- the few choices that must have exactly one home.

These are decisions with no technically correct answer, where the only real
failure is having the answer in several places. A reminder channel default
scattered across a prompt, a handler and a planner drifts apart, and the
drifting is invisible until "remind me at 4:30" produces an alarm one week and
a calendar entry the next.

Each value is overridable from the environment so it can be corrected without
touching code, and each one says why it defaults as it does.
"""

import os


def _text(name, default):
    return (os.getenv(name, "") or "").strip() or default


# --- reminders -------------------------------------------------------- #
# "Remind me at 4:30" names no channel. Three capabilities can satisfy it --
# SET_ALARM, ADD_TASK, ADD_CALENDAR_EVENT -- and exactly one should run, or a
# single request becomes three notifications.
#
# SET_ALARM is the conservative default: it is local, it needs no network or
# account, it fires at the stated time, and it is trivially cancellable. A
# calendar entry is shared and visible to anyone with access to the calendar,
# which is a larger consequence than the request implies; a task has no time
# and so does not actually remind anyone at 4:30.
REMINDER_CHANNEL = _text("VANTA_REMINDER_CHANNEL", "SET_ALARM")

#: Channels a generic reminder may resolve to, if the configured one is
#: unavailable. Order is preference order.
REMINDER_CHANNELS = ("SET_ALARM", "ADD_TASK", "ADD_CALENDAR_EVENT")

#: Channels that can represent a day, not just a clock time.
#:
#: alarms.set_alarm takes a time and nothing else -- it fires today, or
#: tomorrow if the time has already passed. So "remind me on Saturday at 5am"
#: became an alarm for the next morning, three days early, and was reported as
#: done. This is a capability limit, not a preference: a dated reminder has to
#: go to a channel that can hold the date, whatever the configured default
#: says.
DATED_REMINDER_CHANNELS = ("ADD_CALENDAR_EVENT", "ADD_TASK")

#: The one to use when a reminder names a day and the default cannot hold it.
DATED_REMINDER_CHANNEL = _text("VANTA_DATED_REMINDER_CHANNEL",
                               "ADD_CALENDAR_EVENT")

# --- messaging -------------------------------------------------------- #
# "Tell Madhu I'll be late" names no app. Sending on both channels would
# deliver the same message twice.
MESSAGE_CHANNEL = _text("VANTA_MESSAGE_CHANNEL", "SEND_WHATSAPP")
MESSAGE_CHANNELS = ("SEND_WHATSAPP", "SEND_TELEGRAM")

# --- perception ------------------------------------------------------- #
# A bare "what can you see?" is genuinely ambiguous between the camera and the
# screen, and guessing wrong is intrusive in one direction (switching on a
# camera that was not asked for) and merely unhelpful in the other. So there
# is no default: with no established modality, VANTA asks.
PERCEPTION_DEFAULT = _text("VANTA_PERCEPTION_DEFAULT", "camera")

#: How many turns of conversation still count as establishing a modality.
#: Short on purpose -- "what can you see" ten exchanges after a camera photo
#: is not a camera question any more.
PERCEPTION_CONTEXT_TURNS = int(
    os.getenv("VANTA_PERCEPTION_CONTEXT_TURNS", "4") or 4)


def reminder_channel():
    """The configured reminder channel, validated against the known set."""
    channel = REMINDER_CHANNEL.upper()
    return channel if channel in REMINDER_CHANNELS else REMINDER_CHANNELS[0]


def dated_reminder_channel():
    """The channel for a reminder that names a day."""
    channel = DATED_REMINDER_CHANNEL.upper()
    return (channel if channel in DATED_REMINDER_CHANNELS
            else DATED_REMINDER_CHANNELS[0])


def message_channel():
    channel = MESSAGE_CHANNEL.upper()
    return channel if channel in MESSAGE_CHANNELS else MESSAGE_CHANNELS[0]
