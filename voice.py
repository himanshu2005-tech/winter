# -*- coding: utf-8 -*-
"""
voice.py -- how VANTA sounds, online and off.

Kokoro was doing all the speaking. It is a good local model and it has one
property that matters more than quality: it works with no network. It also runs
on CPU here, loads a torch model on first use, and sounds like a local model.

So: a better voice when the network is there, Kokoro when it is not.

    online   edge-tts     Microsoft's neural voices through the Edge
                          read-aloud endpoint. Free, no key, no account,
                          about 1.5s for a sentence.
    offline  Kokoro       already installed, already working, no network
    last     pyttsx3      the Windows voice, so silence is never the answer

The fallback is not tried once and trusted forever. Himanshu's connection drops
several times an hour, and paying an eight second timeout on every single line
to rediscover that would be worse than sounding local -- so a failure is
remembered for a minute and the next line goes straight to Kokoro.
"""

import asyncio
from dataclasses import dataclass
import os
import re
import tempfile
import threading
import time
import uuid

import identity

# Voice, overridable without touching code. Sonia is a British female neural
# voice with an unhurried, even delivery -- the closest of the free Microsoft
# voices to "soothing". Alternatives worth trying, all free and keyless:
#   en-GB-SoniaNeural    warmer, slower British female
#   en-US-AriaNeural     Microsoft's own assistant voice, neutral American
#   en-US-MichelleNeural calm and low American female
#   en-US-JennyNeural    warm American female
#   en-US-MichelleNeural calm, low American female
#   en-IE-EmilyNeural    soft Irish female
#   en-IN-NeerjaNeural   Indian English female
# Libby reads briskly and lands as an assistant. Sonia is warmer and slower,
# which combined with a lowered pitch and a slowed rate came out breathy rather
# than efficient -- soothing was the instruction and sultry was the result. The
# lesson is that "calm" is carried by even pacing, not by a low pitch: drop the
# pitch and slow it down together and you get intimacy, which is the wrong
# register for something that reads out your calendar.
# Ava's multilingual neural voice has more natural phrase-level intonation than
# Libby's brisk announcer delivery.  It remains free/keyless and can still be
# overridden from the environment.
# Chosen on Microsoft's own voice metadata, which carries two fields and I had
# only been reading one. Personality is the obvious half; ContentCategories is
# the half that matters:
#
#   AvaMultilingual  Expressive, Caring, Pleasant   Conversation, Copilot
#   EmmaMultilingual Cheerful, Clear                Conversation, Copilot
#   Jenny            Considerate, Comfort           General
#   Aria             Positive, Confident            News, Novel
#   Michelle         Friendly, Pleasant             News, Novel
#   Sonia / Libby    Friendly, Positive             General
#
# Aria is a NEWSREADER voice. It was picked here for being the only one tagged
# "Confident", which read as mature on paper and arrives as a bulletin: no
# emotion, because reading the news is not supposed to have any. Ava is
# Microsoft's own Copilot voice -- built for assistant conversation, and the
# only one tagged Expressive and Caring. Himanshu had already set it himself;
# this is putting back a choice that was better than mine.
#
# The breathiness that started all of this was prosody, not timbre: Sonia at
# -7% rate and -2Hz pitch. Both are neutral now, so expressive does not become
# intimate.
DEFAULT_VOICE = "en-US-AvaMultilingualNeural"

# Soothing is mostly pace, not timbre. A neural voice at its default rate reads
# like it is presenting; a few percent slower reads like it is talking to you.
# Both are settable, because how calm this should sound is a matter of taste
# rather than something to hard-code.
# A slight trim keeps it from sounding hurried. -7% plus a pitch drop was too
# much of both; -3% alone reads as measured rather than languid.
DEFAULT_RATE_TRIM = float(identity.env("TTS_RATE_TRIM", "-3"))
# Neutral. The -2Hz drop was the single biggest contributor to the wrong tone.
DEFAULT_PITCH = identity.env("TTS_PITCH", "+0Hz")

# How long to wait for the online voice before giving up on this line.
#
# The first call of a session pays for DNS, TLS and the websocket handshake to
# Microsoft's endpoint: measured at 12.9s cold against 1.4-2.9s warm. An 8s
# budget therefore failed the FIRST line every time and then memoised itself
# offline for a minute, so the good voice was never heard at boot -- which is
# precisely when VANTA speaks its greeting. The cold call gets room; the warm
# ones stay tight, because a slow voice mid-conversation should fall back fast.
# 8s was too tight once speech became per-sentence. One reply is now several
# synthesis calls, so there are several chances to trip the budget, and tripping
# it once demoted the voice for a whole minute -- mid-reply. Measured warm calls
# are 1-2s; 15 leaves room for a slow one without changing voices.
ONLINE_TIMEOUT = float(identity.env("TTS_TIMEOUT", "15"))
FIRST_CALL_TIMEOUT = float(identity.env("TTS_FIRST_TIMEOUT", "25"))

_ever_succeeded = False

# How long to stop trying after a failure. Long enough not to re-pay the
# timeout on every sentence, short enough that a reconnect is noticed.
# 30 rather than 60, and the reason the number matters less than it looks:
# the mid-reply case is handled separately now -- a failure while speaking one
# reply does not set this memo at all -- so this only governs the gap BETWEEN
# replies, where being slow to retry a dead endpoint is the right instinct.
# Still halved, because a single blip costing a full minute of a different
# voice was the complaint.
OFFLINE_MEMO_SECONDS = 30.0

# ...but a flat 30 seconds is right only for a blip. Against an endpoint that
# is simply not reachable it is pathological: the memo expires, the next line
# pays the full handshake timeout again, fails again, and sets another 30
# seconds. Observed in a real session -- every single line preceded by
# "Online voice unavailable (WSServerHandshakeError after 25s)", so every
# reply carried 25 seconds of dead air before Kokoro said it instead.
#
# Consecutive failures therefore double the memo up to a ceiling. A blip still
# costs 30 seconds; an endpoint that is down costs 25 seconds once and then
# stops being asked. One success resets it.
OFFLINE_MEMO_CEILING = float(identity.env("TTS_OFFLINE_CEILING", "600"))

_lock = threading.RLock()
_offline_until = 0.0
_consecutive_failures = 0


def _offline_memo_seconds(failures):
    """How long to stop trying, after this many consecutive failures."""
    span = OFFLINE_MEMO_SECONDS * (2 ** max(0, int(failures) - 1))
    return min(span, OFFLINE_MEMO_CEILING)


def _note_online():
    """A success clears the memo and the failure streak."""
    global _consecutive_failures, _offline_until
    with _lock:
        if _consecutive_failures:
            print(f"[Voice] Online voice is back after "
                  f"{_consecutive_failures} failure(s).")
        _consecutive_failures = 0
        _offline_until = 0.0


def offline_state():
    """What the backoff currently is, for tests and for the status line."""
    with _lock:
        return {"failures": _consecutive_failures,
                "offline_until": _offline_until,
                "seconds_left": max(0.0, _offline_until - time.time())}


def voice_name():
    return (identity.env("TTS_VOICE", "") or "").strip() or DEFAULT_VOICE


def pitch_name():
    raw = str(DEFAULT_PITCH or "").strip()
    if not raw:
        return "+0Hz"
    # edge-tts wants an explicit sign; "-2Hz" is fine, "2Hz" is rejected.
    return raw if raw[0] in "+-" else "+" + raw


def online_voice_enabled():
    """False while a recent failure is still being remembered."""
    if identity.env_flag("TTS_OFFLINE", False):
        return False
    with _lock:
        return time.time() >= _offline_until


# Whether this thread is partway through speaking one reply.
#
# "The voice is not uniform, it's cutting in between" was two faults, and this
# is the second: a single timeout set a 60-second memo, so sentence one came out
# in Ava and sentence three in Kokoro -- a different voice, mid-sentence-run.
# Within one reply, consistency matters more than speed, so a failure here
# falls back for that sentence only and does NOT demote the engine for
# everything that follows.
_utterance = threading.local()


def begin_utterance():
    _utterance.active = True


def end_utterance():
    _utterance.active = False


def in_utterance():
    return bool(getattr(_utterance, "active", False))


def _note_offline(reason):
    global _offline_until, _consecutive_failures
    if in_utterance():
        # Mid-reply. Let the next sentence try the same engine again rather
        # than switching voices on him halfway through a sentence run.
        print(f"[Voice] One sentence fell back ({reason}); keeping the same "
              f"voice for the rest of this reply.")
        return
    with _lock:
        _consecutive_failures += 1
        span = _offline_memo_seconds(_consecutive_failures)
        _offline_until = time.time() + span
        failures = _consecutive_failures
    if span >= OFFLINE_MEMO_CEILING:
        print(f"[Voice] Online voice unavailable ({reason}) after {failures} "
              f"attempts; Kokoro is now the voice until it recovers "
              f"(rechecking every {int(span)}s).")
    else:
        print(f"[Voice] Online voice unavailable ({reason}); using Kokoro for "
              f"the next {int(span)}s.")


def _rate_for(speed):
    """Kokoro takes a multiplier; edge-tts takes a percentage string.

    A pure unit conversion, and kept that way: the soothing trim below is a
    separate decision about delivery, and folding it in here made a conversion
    function silently editorialise about pace. A test caught exactly that.
    """
    try:
        percent = int(round((float(speed) - 1.0) * 100))
    except (TypeError, ValueError):
        percent = 0
    return f"{percent:+d}%"


def _delivery_rate(speed, offset=0):
    """The rate actually spoken: the conversion, shifted by the trim.

    The trim shifts the whole scale rather than pinning it, so asking for a
    faster read still gets one -- just from a calmer starting point.
    """
    base = int(str(_rate_for(speed)).replace("%", ""))
    percent = base + int(round(DEFAULT_RATE_TRIM)) + int(round(offset))
    # Past about a quarter slower it stops sounding calm and starts sounding
    # slurred, so the trim cannot reach somewhere unusable.
    return f"{max(-25, min(40, percent)):+d}%"


@dataclass(frozen=True)
class Delivery:
    name: str = "neutral"
    rate_offset: int = 0
    pitch: str = "+0Hz"
    volume: str = "+0%"
    local_speed_scale: float = 1.0


def delivery_for(text):
    """Select vocal posture from the words being spoken, without an LLM call.

    This is deliberately about delivery, not meaning or safety decisions.  It
    cannot trigger actions and it never changes the dashboard text.
    """
    body = " ".join(str(text or "").casefold().split())
    if re.search(r"\b(emergency|danger|unsafe|accident|fire|call 112|immediately)\b", body):
        return Delivery("urgent", rate_offset=5, pitch="+1Hz", volume="+6%",
                        local_speed_scale=1.05)
    if re.search(
        r"\b(heartbroken|hurt|lonely|alone|overwhelmed|anxious|scared|sad|"
        r"rough|difficult|with you|stay with|take your time|rest)\b", body
    ):
        # Warm, not breathy: a small pace change and a tiny pitch drop. Large
        # changes sound theatrical and were the source of the old "exotic" tone.
        return Delivery("warm", rate_offset=-4, pitch="-1Hz", volume="-1%",
                        local_speed_scale=0.96)
    if re.search(r"(?:!|\b(great|excellent|done|nice|congratulations|good news)\b)", body):
        return Delivery("bright", rate_offset=3, pitch="+2Hz", volume="+2%",
                        local_speed_scale=1.03)
    return Delivery()


def _edge_tts(text, speed, note_failures=True, delivery=None):
    """Synthesise with the online voice. Returns a path, or None.

    note_failures exists for the warm-up call, which must never be able to
    push the voice offline: a failure there is not evidence that the endpoint
    is unreachable for real speech.
    """
    global _ever_succeeded
    path = os.path.join(tempfile.gettempdir(),
                        f"vanta_voice_{os.getpid()}_{uuid.uuid4().hex}.mp3")

    budget = ONLINE_TIMEOUT if _ever_succeeded else FIRST_CALL_TIMEOUT
    delivery = delivery or delivery_for(text)

    async def run():
        import edge_tts
        communicate = edge_tts.Communicate(text, voice_name(),
                                           rate=_delivery_rate(speed, delivery.rate_offset),
                                           pitch=delivery.pitch or pitch_name(),
                                           volume=delivery.volume)
        await asyncio.wait_for(communicate.save(path), timeout=budget)

    try:
        asyncio.run(run())
        _ever_succeeded = True
    except Exception as exc:
        if note_failures:
            _note_offline(f"{type(exc).__name__} after {budget:.0f}s")
        else:
            print(f"[Voice] Warm-up did not complete ({type(exc).__name__}); "
                  f"the first spoken line will pay the handshake instead.")
        try:
            if os.path.exists(path):
                os.remove(path)
        except OSError:
            pass
        return None

    if not os.path.exists(path) or os.path.getsize(path) < 512:
        # A file that exists but holds nothing is worse than no file: it plays
        # as silence, which looks exactly like VANTA ignoring him.
        _note_offline("empty audio")
        try:
            os.remove(path)
        except OSError:
            pass
        return None
    # Only here: the call returned AND the file holds real audio. Resetting on
    # the call alone would let an endpoint that answers with empty files reset
    # the streak forever and never escalate.
    _note_online()
    return path


def warm(background=True):
    """Pay the handshake before the first thing VANTA says needs to be said.

    Without this the greeting is the line that absorbs the cold connection, and
    a greeting is exactly the wrong line to lose.
    """
    def run():
        try:
            # A lone "." synthesises no audio at all, so edge-tts raises
            # NoAudioReceived -- and because a failure here marked the voice
            # offline for a minute, the warm-up was actively breaking the very
            # thing it existed to protect. It needs real words.
            path = _edge_tts("Ready.", 1.0, note_failures=False)
            if path and os.path.exists(path):
                os.remove(path)
        except Exception:
            pass

    if background:
        threading.Thread(target=run, daemon=True, name="voice-warm").start()
    else:
        run()


def speak_to_file(text, speed=1.0, kokoro_synth=None):
    """Produce an audio file for *text*. Returns (path, engine) or (None, None).

    kokoro_synth is the caller's existing local synthesiser, passed in rather
    than imported so this module does not have to own the torch pipeline or
    know when it was warmed.
    """
    # Defence in depth: agent.speak() already cleans display markup, but this
    # lower boundary is also used by remote audio and may gain new callers.
    from conversation_output import speech_safe_text
    body = speech_safe_text(text)
    if not body:
        return None, None

    delivery = delivery_for(body)

    if online_voice_enabled():
        path = _edge_tts(body, speed, delivery=delivery)
        if path:
            return path, "edge"

    if kokoro_synth is not None:
        try:
            path = kokoro_synth(body, speed * delivery.local_speed_scale)
            if path:
                return path, "kokoro"
        except Exception as exc:
            print(f"[Voice] Kokoro failed: {exc}")

    return None, None
