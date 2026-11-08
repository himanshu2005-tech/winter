# -*- coding: utf-8 -*-
"""
fast_commands.py -- the turns that should never reach a model.

"Mute" was six completions and roughly fifty thousand prompt tokens away from
muting the speakers, because every utterance went through the mode router, the
cognitive router, the planner, the executor and the critic. For something a
keyboard does in one keypress that is absurd on both counts: it costs money and
it takes seconds, and the answer was never in doubt.

These are the utterances whose meaning is fixed. There is nothing to plan,
nothing to infer, and no situation in which "mute" means anything other than
mute.

Three rules keep this from eating real requests:

*Whole utterances only.* The normalised text must BE the command, not contain
it. "Mute" mutes. "Why is chrome muted", "mute the tab not the whole system"
and "should I mute this call" all go to the model, because they are questions
and instructions about muting rather than the command itself. Substring
matching here would be a disaster: it would silently answer a question nobody
asked.

*Nothing ambiguous.* Bare "next" is deliberately absent -- during a lesson it
means the next board page, and a confident wrong action is worse than a slow
right one. Bare "stop" is absent too: that is how Himanshu interrupts a running
task, and stealing it would take away the brake.

*Nothing destructive, and no false claims.* Everything here is instant and
reversible. When the machine cannot do it -- no audio endpoint, an external
monitor with no brightness control -- the handler returns None and the turn
goes to the model as if this file did not exist. Reporting success for
something that did not happen is the one outcome worse than being slow.
"""

import datetime
import os
import re

# A number from 0 to 100, for the "volume 40" family.
_PERCENT = re.compile(r"^(?:set\s+)?(?:the\s+)?"
                      r"(volume|brightness)\s+(?:to\s+)?(\d{1,3})\s*(?:percent|%)?$")


def normalise(text):
    """Strip address, politeness and punctuation down to the bare command.

    "Hey Vanta, please mute, thanks" and "mute" are the same instruction, and
    a table of literal phrases that did not know that would miss most of the
    ways he actually speaks.
    """
    cleaned = str(text or "").strip().lower()
    # Keep word characters, digits and % -- everything else is punctuation here.
    cleaned = re.sub(r"[^\w%\s]", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    for _ in range(4):
        before = cleaned
        cleaned = re.sub(r"^(?:hey|ok|okay|yo|hi|hello)\s+", "", cleaned)
        cleaned = re.sub(r"^vanta\s+", "", cleaned)
        cleaned = re.sub(r"^(?:please|pls|just|now|quickly)\s+", "", cleaned)
        cleaned = re.sub(r"^(?:can|could|would|will)\s+you\s+", "", cleaned)
        cleaned = re.sub(r"\s+(?:please|now|thanks|thank you|for me)$", "", cleaned)
        if cleaned == before:
            break
    return cleaned


# ------------------------------------------------------------------ #
# Handlers. Each returns a short line to say, or None to hand the turn
# back to the model untouched.
# ------------------------------------------------------------------ #

def _audio():
    import system_controls
    return system_controls


def _mute(_ui=None):
    result = _audio().set_mute(True)
    return "Muted." if result.startswith("[Success]") else None


def _unmute(_ui=None):
    result = _audio().set_mute(False)
    return "Unmuted." if result.startswith("[Success]") else None


def _volume_step(delta):
    def handler(_ui=None):
        controls = _audio()
        percent, _muted = controls.get_volume()
        if percent is None:
            return None
        target = max(0, min(100, percent + delta))
        if not controls.set_volume(target).startswith("[Success]"):
            return None
        # Raising the volume on a muted speaker looks broken, so asking for
        # more volume implies unmuting.
        if delta > 0:
            controls.set_mute(False)
        return f"Volume {target} percent."
    return handler


def _volume_to(level):
    def handler(_ui=None):
        controls = _audio()
        if not controls.set_volume(level).startswith("[Success]"):
            return None
        if level > 0:
            controls.set_mute(False)
        return f"Volume {level} percent."
    return handler


def _brightness_step(delta):
    def handler(_ui=None):
        controls = _audio()
        current = controls.get_brightness()
        if current is None:
            # An external monitor exposes no brightness control. The model can
            # explain that; this file should not pretend it worked.
            return None
        target = max(0, min(100, current + delta))
        if not controls.set_brightness(target).startswith("[Success]"):
            return None
        return f"Brightness {target} percent."
    return handler


def _brightness_to(level):
    def handler(_ui=None):
        if not _audio().set_brightness(level).startswith("[Success]"):
            return None
        return f"Brightness {level} percent."
    return handler


def _media(action, spoken):
    def handler(_ui=None):
        if not _audio().media(action).startswith("[Success]"):
            return None
        return spoken
    return handler


def _study_music(_ui=None):
    """Play and verify a sensible study default without involving an LLM."""
    from providers import ytmusic_api
    result = ytmusic_api.play("lofi hip hop study beats instrumental focus")
    if not result.startswith("[Success]") or "Now playing:" not in result:
        return ("I opened the study-music results, but playback did not start. "
                "I did not launch another track.")
    track = result.split("Now playing:", 1)[1].strip()
    return f"Study music is playing: {track}."


def _default_music(_ui=None):
    """Start or resume verified music; never claim a blind media-key worked."""
    from providers import ytmusic_api
    result = ytmusic_api.play_default()
    if not result.startswith("[Success]") or "Now playing:" not in result:
        return ("I couldn't verify that music started. I left your current "
                "screen untouched.")
    track = result.split("Now playing:", 1)[1].strip()
    return f"Playing: {track}."


def _ui_message(kind, payload, spoken):
    def handler(ui_queue=None):
        if ui_queue is None:
            return None
        ui_queue.put((kind, payload))
        return spoken
    return handler


def _time(_ui=None):
    return datetime.datetime.now().strftime("It's %H:%M.")


def _date(_ui=None):
    now = datetime.datetime.now()
    return now.strftime("It's %A, %d %B %Y.").replace(" 0", " ")


def _battery(_ui=None):
    try:
        import psutil
        battery = psutil.sensors_battery()
    except Exception:
        return None
    if battery is None:
        return None
    state = "charging" if battery.power_plugged else "on battery"
    return f"Battery {int(round(battery.percent))} percent, {state}."


def _authenticate_chatgpt(_ui=None):
    """Run the official browser OAuth flow without spending a model call."""
    from pathlib import Path
    from providers import chatgpt_auth
    result = chatgpt_auth.authenticate(Path(__file__).resolve().parent)
    import llm_router
    llm_router.init_clients()
    return result


def _chatgpt_status(_ui=None):
    from providers import chatgpt_auth
    state = chatgpt_auth.status(refresh=True)
    if state["available"]:
        suffix = f" as {state['email']}" if state.get("email") else ""
        return f"ChatGPT is connected{suffix}."
    return "ChatGPT is not connected. Say authenticate ChatGPT to connect it."


def _reauthorize_google(_ui=None):
    """Run the existing Google OAuth flow directly after an explicit request.

    Authentication is infrastructure, not a reasoning task. Sending this
    through routing/planning can strand a valid OAuth action behind a tiny
    execution budget (particularly when the request interrupts startup).
    """
    from pathlib import Path
    from providers.google_api import reauthorize_google, google_connection_status
    try:
        base = Path(__file__).resolve().parent
        reauthorize_google(base)
        status = google_connection_status(base)
        if not status.get("ok"):
            return ("Google sign-in finished, but VANTA could not verify the "
                    f"new grant: {status.get('message', 'unknown error')}")
        return "Google access was reauthorized and verified successfully."
    except Exception as exc:
        # This command is dispatched out of band while another turn may be
        # completing. Returning a concrete failure keeps it from disappearing
        # silently when the browser cannot open or the consent flow is closed.
        return f"Google reauthorization could not be completed: {exc}"


# ------------------------------------------------------------------ #
# The table. Phrases are normalised forms, so no punctuation and no
# leading "vanta" -- normalise() has already removed those.
# ------------------------------------------------------------------ #
COMMANDS = {}


def _register(handler, *phrases):
    for phrase in phrases:
        COMMANDS[phrase] = handler


_register(_mute, "mute", "mute it", "mute audio", "mute the audio", "mute sound",
          "mute the sound", "mute the speaker", "mute the speakers",
          "mute yourself", "silence", "be quiet")
_register(_unmute, "unmute", "unmute it", "unmute audio", "unmute the audio",
          "unmute sound", "unmute the sound", "unmute the speaker",
          "unmute the speakers", "sound on")

_register(_volume_step(10), "volume up", "turn it up", "turn the volume up",
          "turn up the volume", "louder", "a bit louder", "raise the volume")
_register(_volume_step(-10), "volume down", "turn it down",
          "turn the volume down", "turn down the volume", "quieter",
          "a bit quieter", "a little quieter", "slightly quieter",
          "lower the volume", "lower it a bit", "turn it down a bit",
          "turn it down a little", "reduce the volume a bit",
          "make it a little quieter", "the sound is too loud")
_register(_volume_to(100), "max volume", "full volume", "volume max",
          "maximum volume", "volume all the way up")
_register(_volume_to(0), "volume off", "volume zero")

_register(_brightness_step(10), "brighter", "brightness up", "screen brighter",
          "turn the brightness up", "turn up the brightness")
_register(_brightness_step(-10), "dimmer", "darker", "brightness down",
          "screen dimmer", "turn the brightness down", "turn down the brightness")
_register(_brightness_to(100), "max brightness", "full brightness")

# "pause" and "play" both send the one transport key, so the reply stays
# neutral rather than claiming a direction it cannot read back.
_register(_media("playpause", "Done."), "play", "pause", "resume", "play it",
          "pause it", "pause the music", "resume the music", "pause music")
_register(_default_music, "play the music", "play music", "start music",
          "put on music", "play some music")
_register(_study_music, "play study music", "start study music",
          "play some study music", "put on study music",
          "play lofi study music", "play lo fi study music")
# Qualified only: a bare "next" belongs to the active conversational context.
_register(_media("next", "Skipped."), "next song", "next track", "skip song",
          "skip the song", "skip this song", "skip track")
_register(_media("previous", "Going back."), "previous song", "previous track",
          "last song", "go back a song")
_register(_media("stop", "Stopped."), "stop the music", "stop music",
          "stop playback", "stop the song")

_register(_ui_message("screen_light_off", "", "Light off."),
          "lights off", "light off", "screen light off", "turn the light off",
          "turn off the light", "turn the lights off", "kill the light")
_register(_ui_message("hide_result", "", "Report hidden."),
          "hide the report", "close the report", "report off", "hide report",
          "hide the board", "close the board", "board off", "hide board",
          "put the board away", "clear the board away")

_register(_time, "what time is it", "what s the time", "whats the time",
          "the time", "time", "what is the time", "tell me the time")
_register(_date, "what s the date", "whats the date", "what is the date",
          "what day is it", "the date", "today s date", "todays date")
_register(_battery, "battery", "battery level", "how s the battery",
          "hows the battery", "what s my battery", "whats my battery",
          "battery status", "how much battery do i have")
_register(_authenticate_chatgpt, "authenticate chatgpt", "connect chatgpt",
          "sign in with chatgpt", "login with chatgpt", "log in with chatgpt")
_register(_chatgpt_status, "chatgpt status", "is chatgpt connected",
          "check chatgpt connection")
_register(_reauthorize_google, "reauthorize google", "reconnect google",
          "authenticate google", "connect google", "sign in with google",
          "login with google", "log in with google")


def resolve(text):
    """The handler for this utterance, or None if a model should see it."""
    phrase = normalise(text)
    if not phrase:
        return None
    if phrase in COMMANDS:
        return COMMANDS[phrase]
    # Natural but still unambiguous study-music requests stay on the reflex
    # path. Requiring both a play verb and a study/focus music concept avoids
    # stealing unrelated sentences such as "play chess while I study".
    if (re.search(r"\b(?:play|start|put on)\b", phrase)
            and re.search(r"\b(?:study|focus|lo ?fi)\b", phrase)
            and re.search(r"\b(?:music|beats|playlist|mix)\b", phrase)):
        return _study_music
    match = _PERCENT.match(phrase)
    if match:
        kind, raw = match.group(1), int(match.group(2))
        if raw > 100:
            return None
        return _volume_to(raw) if kind == "volume" else _brightness_to(raw)
    return None


def runtime_enabled(text):
    """Whether the production agent may bypass semantic routing for *text*.

    Emergency speech/task controls live in ``agent.handle_live_control``. The
    only remaining model bypasses here are authentication lifecycle operations
    that may be needed before a cloud model is reachable. The larger historical
    reflex table remains as a one-flag rollback and for direct unit testing.
    """
    handler = resolve(text)
    if handler in {_authenticate_chatgpt, _chatgpt_status,
                   _reauthorize_google}:
        return True
    return os.getenv("VANTA_LEGACY_FAST_COMMANDS", "0").strip().casefold() in {
        "1", "true", "yes", "on"
    }


def run(text, ui_queue=None):
    """Execute the utterance if it is a fast command.

    Returns the line to say, or None when this was not a fast command or the
    machine could not carry it out -- in both cases the caller must let the
    turn proceed to the model as normal.
    """
    handler = resolve(text)
    if handler is None:
        return None
    try:
        return handler(ui_queue)
    except Exception as exc:
        # A fast path that raises must not lose the turn.
        print(f"[Fast] '{normalise(text)}' failed, handing to the model: {exc}")
        return None


def phrases():
    """Every literal phrase handled, for tests and for the dashboard."""
    return sorted(COMMANDS)
