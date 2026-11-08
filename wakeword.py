# -*- coding: utf-8 -*-
"""
wakeword.py -- listening for the name, without listening to everything else.

The old wake word was dead code: `wakeword_active` was never set True, so the
worker never started and push-to-talk was the only way in. It also listened for
"hey jarvis", recorded a fixed five seconds however long the sentence was, and
could not have said "Vanta" at all, because openWakeWord ships no model for it.

Three engines, tried in order, because the right one depends on what is
installed rather than on what would be nicest:

1. **A custom openWakeWord model**, if one is sitting in assets/wakewords. This
   is the cheapest and fastest option and the one to use long term -- a few
   hundred KB of ONNX scoring every 80 ms. It needs a one-time training step
   that cannot happen from inside this process.

2. **Porcupine**, if an access key and a .ppn keyword file are configured.
   Also excellent, also needs an account and a one-time download.

3. **A local speech gate** -- the default, and the reason this works today.
   Energy detection notices that somebody is speaking, and the short utterance
   is transcribed by a tiny LOCAL Whisper model to see whether it was the name.
   Slower than the other two by a few hundred milliseconds and heavier on CPU,
   but it needs nothing downloaded, nothing trained, and no account, and it
   recognises any name Himanshu chooses.

The pretrained "hey jarvis" model is deliberately NOT a fallback. Silently
answering to a different name than the one configured is worse than saying
plainly that the custom model is missing.

PRIVACY
-------
Every engine here runs entirely on this machine. What that does and does not
mean, stated precisely, because "private" is easy to claim and easy to get
wrong:

* Audio before the wake word is held in a bounded ring buffer in memory, a few
  seconds at most, and is overwritten continuously.
* It is never written to disk. The speech gate takes a numpy array, not a
  path, specifically so no temporary file exists to leak or to forget to
  delete.
* It never reaches a network. The gate uses the local Whisper model and cannot
  be configured online; the cloud transcriber is not reachable from this file.
* Nothing before the wake word is transcribed to anywhere it could be stored,
  logged, or sent to a model. The gate's text is compared to the name and
  discarded.

What it cannot mean, and should not be claimed: the microphone is open. Any
wake word requires continuously processing audio -- that is what "wake word"
is. The guarantee is about where that audio goes, which is nowhere.
"""

import collections
import math
import os

import identity

SAMPLE_RATE = 16000

#: How loud a frame has to be to count as speech, relative to full scale.
#:
#: 0.012 was tuned on a close, loud voice. A laptop array microphone across a
#: desk delivers ordinary speech at an RMS of 0.004-0.01, so at 0.012 the gate
#: never even looked at it -- "I said Vanta and nothing happened", with no
#: message, because nothing below the threshold is ever transcribed. The room's
#: own noise floor (see _threshold) still raises this in a noisy room.
SPEECH_RMS = float(identity.env("WAKE_RMS", "0.003"))
#: Silence this long ends an utterance.
SILENCE_END = float(identity.env("WAKE_SILENCE", "0.45"))
#: Utterances outside this range are not a one-word wake phrase.
MIN_SPEECH = 0.20
MAX_SPEECH = 2.60
#: "Hey Vanta, open my placements portal" is three to four seconds. It used to
#: be discarded as "too long to be the wake word" without ever being
#: transcribed, so the more naturally someone asked, the less it worked. The
#: name is checked in the first PREFIX_SECONDS; if it is there the rest of the
#: breath, up to this long, is kept as the command.
MAX_COMMAND_SPEECH = 12.0
PREFIX_SECONDS = 2.2
# Preserve the quiet lead-in immediately before speech. Laptop arrays often
# put an unstressed "Hey" below the threshold and the louder name above it;
# without this, the decoder sees the name with its first phoneme clipped.
PRE_ROLL_SECONDS = 0.35
#: openWakeWord / Porcupine confidence. 0.5 false-triggers; a two-syllable name
#: wants more margin than that.
THRESHOLD = float(identity.env("WAKE_THRESHOLD", "0.65"))
#: With this on, every utterance considered is logged with what was heard and
#: why it was accepted or not. Calibration proved this outside the app; the same
#: view is needed inside it, because "the wake word does not work" has the same
#: several causes there and they are not guessable from the outside.
DEBUG = identity.env_flag("WAKE_DEBUG", False)


def _debug(message):
    if DEBUG:
        print(f"[WakeWord] {message}")

WAKEWORD_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "assets", "wakewords")


def _rms(frame):
    """Root-mean-square of a float32 frame, without needing numpy loaded."""
    try:
        import numpy as np
        return float(np.sqrt(np.mean(np.square(frame))))
    except Exception:
        if len(frame) == 0:
            return 0.0
        return math.sqrt(sum(float(x) * float(x) for x in frame) / len(frame))


# openWakeWord ships these already trained, on disk, no download and no account.
# They are dramatically more reliable than the speech gate -- a purpose-trained
# CNN scoring every 80ms, rather than a general transcriber guessing at one
# invented word with no sentence around it to go on. The catch is that the
# phrase has to be one of these four, so choosing one means the wake phrase and
# the assistant's name are different things. That is normal -- and a wake phrase
# that works beats a wake phrase that matches the name.
PRETRAINED = {
    "alexa": "alexa_v0.1",
    "jarvis": "hey_jarvis_v0.1",
    "hey jarvis": "hey_jarvis_v0.1",
    "mycroft": "hey_mycroft_v0.1",
    "hey mycroft": "hey_mycroft_v0.1",
    "rhasspy": "hey_rhasspy_v0.1",
    "hey rhasspy": "hey_rhasspy_v0.1",
}


def pretrained_name(word=None):
    """The shipped model for this phrase, if there is one."""
    wanted = str(word or identity.WAKE_WORD).lower().strip()
    return PRETRAINED.get(wanted)


def custom_model_path():
    """A trained model for this wake word, if Himanshu has added one."""
    if not os.path.isdir(WAKEWORD_DIR):
        return None
    wanted = identity.WAKE_WORD.lower().replace(" ", "_")
    for name in sorted(os.listdir(WAKEWORD_DIR)):
        stem, ext = os.path.splitext(name.lower())
        if ext in (".onnx", ".tflite") and wanted in stem:
            return os.path.join(WAKEWORD_DIR, name)
    return None


def porcupine_keyword_path():
    explicit = identity.env("PORCUPINE_KEYWORD")
    if explicit and os.path.isfile(explicit):
        return explicit
    if not os.path.isdir(WAKEWORD_DIR):
        return None
    wanted = identity.WAKE_WORD.lower().replace(" ", "_")
    for name in sorted(os.listdir(WAKEWORD_DIR)):
        if name.lower().endswith(".ppn") and wanted in name.lower():
            return os.path.join(WAKEWORD_DIR, name)
    return None


# ------------------------------------------------------------------ #
# Engines. Each takes float32 mono frames and answers one question:
# was the name just said?
# ------------------------------------------------------------------ #

class OpenWakeWordEngine:
    name = "openWakeWord (custom model)"
    #: These engines score audio and cannot read words, so a command
    #: spoken in the same breath is not recoverable from them.
    trailing = ""

    def __init__(self, model_path):
        from openwakeword.model import Model
        # openWakeWord accepts either a path or the name of a shipped model, so
        # both routes land here and neither needs a special case downstream.
        self._model = Model(wakeword_models=[model_path])
        self._path = model_path

    def describe(self):
        # A shipped model is not a custom one, and saying so matters: it means
        # the phrase is Jarvis rather than the configured name.
        kind = "pretrained" if not os.path.sep in str(self._path) else "custom"
        return f"openWakeWord ({kind}): {os.path.basename(self._path)}"

    def feed(self, frame):
        import numpy as np
        self._model.predict((np.asarray(frame) * 32767).astype(np.int16))
        for key in self._model.prediction_buffer:
            if self._model.prediction_buffer[key][-1] > THRESHOLD:
                self._model.reset()
                return True
        return False

    def reset(self):
        try:
            self._model.reset()
        except Exception:
            pass


class PorcupineEngine:
    name = "Porcupine"
    #: These engines score audio and cannot read words, so a command
    #: spoken in the same breath is not recoverable from them.
    trailing = ""

    def __init__(self, access_key, keyword_path):
        import pvporcupine
        self._engine = pvporcupine.create(access_key=access_key,
                                          keyword_paths=[keyword_path],
                                          sensitivities=[THRESHOLD])
        self._path = keyword_path
        self._buffer = collections.deque()

    def describe(self):
        return f"{self.name}: {os.path.basename(self._path)}"

    def feed(self, frame):
        import numpy as np
        self._buffer.extend((np.asarray(frame) * 32767).astype(np.int16).tolist())
        length = self._engine.frame_length
        fired = False
        while len(self._buffer) >= length:
            chunk = [self._buffer.popleft() for _ in range(length)]
            if self._engine.process(chunk) >= 0:
                fired = True
        return fired

    def reset(self):
        self._buffer.clear()


class SpeechGateEngine:
    """Energy detection plus a tiny local transcriber.

    Whisper is not a wake-word engine and this is not pretending otherwise. It
    is here because it works for any name with nothing to install, and because
    running it only on bounded utterances -- rather than continuously -- keeps
    the cost to a short burst whenever somebody in the room speaks.
    """

    name = "local speech gate"

    def __init__(self, word=None):
        self._word = (word or identity.WAKE_WORD).lower().strip()
        # "Vanta, mute" is one breath, and the old design threw the command
        # half away: it fired on the name and then recorded a fresh five
        # seconds, so the thing already said had to be said again. Whatever
        # followed the name in the same utterance is kept here and used as the
        # command, which is what makes a one-word order feel instant.
        self.trailing = ""
        # The last thing transcribed, whether or not it was the name. Kept so a
        # caller can recognise an utterance that needs no wake word at all --
        # "mute" means one thing and costs nothing, so requiring a name first is
        # ceremony. Anything beyond that closed set still needs addressing,
        # because it reaches a paid model and may not be undoable.
        self.heard = ""
        # The first decoder pass, before the wake-name spelling prompt.  Only
        # this text may become a nameless follow-up or fast command.
        self.unbiased_heard = ""
        self.heard_as_followup = False
        # The runtime opens this for thirty seconds after a user turn. During
        # that window ordinary speech is a follow-up, not a candidate one-word
        # wake phrase, and may therefore be a full sentence.
        self.conversation_open = False
        self._followup_utterance = False
        # A bounded ring: the utterance being collected, and nothing older.
        self._speech = []
        self._pre_roll = []
        self._quiet_for = 0.0
        self._max_samples = int(MAX_SPEECH * SAMPLE_RATE)
        # The standalone gate stays responsive by default.  The live runtime
        # raises this to its conversational endpoint (currently two seconds)
        # so a thinking pause does not hand the turn to VANTA prematurely.
        self.endpoint_silence = SILENCE_END
        # Variants a small model realistically produces for the name. Compared
        # against the transcript rather than trusting it to spell the name.
        self._accept = {self._word}
        for variant in _spelling_variants(self._word):
            self._accept.add(variant)
        self._loose = set(_loose_variants(self._word))

    def describe(self):
        return f"{self.name}: listening for '{self._word}' (fully local)"

    def set_followup_window(self, opened):
        """Tell the local gate whether nameless follow-ups are currently valid.

        The worker owns the clock; the engine owns utterance collection. Once
        speech begins inside the window, expiry halfway through the sentence
        must not truncate that sentence.
        """
        self.conversation_open = bool(opened)

    @property
    def collecting(self):
        """True while a bounded local utterance is being collected.

        The UI uses this as a state signal only; raw samples remain private to
        the gate and are never exposed or persisted.
        """
        return bool(self._speech)

    #: How much louder than the room a frame has to be to count as speech.
    #: Twice the ambient level is audible; one and a half is the air
    #: conditioning flickering.
    FLOOR_MARGIN = 2.0
    #: Seconds of recent frames the room's loudness is measured over.
    FLOOR_WINDOW = 6.0

    def _threshold(self):
        """The loudness a frame needs to count as speech, given the room.

        A fixed number is right in a quiet room and wrong everywhere else. In
        heavy rain, or beside a fan, or with a film on, the ambient level sits
        above it: every frame then counts as speech, `_quiet_for` never
        accumulates, the utterance never ends, and the gate is stuck
        "collecting" forever without once transcribing anything -- which from
        the outside is exactly "I said Vanta and nothing happened".

        So the threshold follows the room. The floor is a low percentile of
        recent frames rather than a mean, so speech itself -- which is loud --
        cannot drag it up and make the next word harder to hear.
        """
        history = getattr(self, "_levels", None)
        if not history:
            return SPEECH_RMS
        ordered = sorted(history)
        floor = ordered[max(0, int(len(ordered) * 0.2) - 1)]
        return max(SPEECH_RMS, floor * self.FLOOR_MARGIN)

    def _note_level(self, level, frame_seconds):
        history = getattr(self, "_levels", None)
        if history is None:
            history = self._levels = []
        history.append(level)
        keep = max(8, int(self.FLOOR_WINDOW / max(frame_seconds, 1e-3)))
        if len(history) > keep:
            del history[:len(history) - keep]

    def feed(self, frame):
        import stt
        seconds = len(frame) / float(SAMPLE_RATE)
        level = _rms(frame)
        threshold = self._threshold()

        # After a sustained non-speech sound, learn that new room before
        # waiting for it to become absolutely quiet.  A fan or film may never
        # fall below the fixed minimum; relative quiet is what matters.
        if getattr(self, "_skip_until_quiet", False):
            self._note_level(level, seconds)
            if level < self._threshold():
                self._quiet_for += seconds
                if self._quiet_for >= self.endpoint_silence:
                    self._skip_until_quiet = False
                    self._quiet_for = 0.0
            else:
                self._quiet_for = 0.0
            return False

        if level >= threshold:
            self._quiet_for = 0.0
            self._peak = max(getattr(self, "_peak", 0.0), level)
            starting = not self._speech
            if starting and self.conversation_open:
                self._followup_utterance = True
            if starting and self._pre_roll:
                self._speech.extend(self._pre_roll)
                self._pre_roll = []
            # Host-selected audio blocks may vary, particularly after a
            # default-device or Bluetooth transition. Count actual samples.
            collected = sum(len(item) for item in self._speech)
            limit = (int(MAX_COMMAND_SPEECH * SAMPLE_RATE)
                     if (getattr(self, "_addressed", False)
                         or getattr(self, "_followup_utterance", False))
                     else self._max_samples)
            if collected < limit:
                self._speech.append(frame)
                if (not getattr(self, "_addressed", False)
                        and not getattr(self, "_followup_utterance", False)
                        and collected >= int(PREFIX_SECONDS * SAMPLE_RATE)):
                    # Long enough that it is either a sentence or the room.
                    # Look at the start: a name leads, so it is in there if it
                    # is there at all.
                    if self._prefix_is_addressed():
                        self._addressed = True
                    else:
                        _debug("long utterance that does not open with the name "
                               "-- ignored until it ends")
                        self.reset()
                        self._skip_until_quiet = True
            else:
                # Still loud after the longest it could possibly be. That is
                # not somebody speaking to it, it is the room -- drop the
                # collection so the gate can recover, instead of holding a
                # buffer it will never be allowed to finish. The floor above
                # has already started adapting to whatever this was.
                _debug("collecting past the longest possible utterance; "
                       "treating it as ambient noise and starting over")
                self.reset()
                self._skip_until_quiet = True
            return False

        if not self._speech:
            # Only a frame already classified as non-speech may teach the
            # ambient floor.  Previously the level was inserted first and the
            # threshold recalculated from it; immediately after startup the
            # first spoken "Vanta" therefore made itself 2.5x too quiet and
            # was discarded before Whisper ever saw it.
            self._note_level(level, seconds)
            self._pre_roll.append(frame)
            keep_samples = int(PRE_ROLL_SECONDS * SAMPLE_RATE)
            while (len(self._pre_roll) > 1
                   and sum(len(item) for item in self._pre_roll) > keep_samples):
                self._pre_roll.pop(0)
            return False
        self._quiet_for += seconds
        if self._quiet_for < self.endpoint_silence:
            return False

        import numpy as np
        # Sound devices differ on whether mono is shaped (n,) or (n, 1), and
        # can even change shape after a device reconnect. Normalise per frame
        # before joining so one transition cannot kill the wake gate.
        samples = np.concatenate(
            [np.asarray(frame, dtype=np.float32).reshape(-1)
             for frame in self._speech], axis=0)
        peak = getattr(self, "_peak", 0.0)
        addressed = getattr(self, "_addressed", False)
        followup = getattr(self, "_followup_utterance", False)
        self.reset()
        duration = len(samples) / float(SAMPLE_RATE)
        limit = MAX_COMMAND_SPEECH if (addressed or followup) else MAX_SPEECH
        if not (MIN_SPEECH <= duration <= limit):
            _debug(f"[{duration:.2f}s peak {peak:.4f}] too "
                   f"{'short' if duration < MIN_SPEECH else 'long'} to be the "
                   f"wake word -- not transcribed")
            return False
        # The name is passed as a hotword: a made-up word has no prior
        # probability, so without the nudge the decoder prefers whatever common
        # English phrase fits the sounds.
        try:
            decoded = stt.transcribe_samples_local(
                samples, hotwords=self._word, return_unbiased=True)
        except TypeError as exc:
            # Compatibility for an installed/embedded STT adapter (and test
            # doubles) implementing the older two-argument contract. Do not
            # mask unrelated decoder TypeErrors.
            if "return_unbiased" not in str(exc):
                raise
            decoded = stt.transcribe_samples_local(
                samples, hotwords=self._word)
        # Test doubles and older STT implementations may still return one
        # string.  Runtime STT returns (name-corrected, unbiased).
        if isinstance(decoded, tuple) and len(decoded) == 2:
            heard, unbiased = decoded
        else:
            heard = unbiased = decoded
        self.heard = str(heard or "").strip()
        self.unbiased_heard = str(unbiased or "").strip()
        self.heard_as_followup = followup
        if (self.heard != self.unbiased_heard and self.matches(self.heard)
                and not self._biased_wake_supported(
                    self.heard, self.unbiased_heard)):
            # A prompted decoder is allowed to repair how the name is spelled;
            # it is not allowed to invent the name or the command.  The first,
            # unprompted pass must contain either a plausible name/lead-in and
            # enough of the same command.  Otherwise room noise such as a fan
            # can become "Vanta, what is the time" solely because those words
            # appeared in a decoder prompt.
            _debug(f"[{duration:.2f}s peak {peak:.4f}] biased transcript "
                   f"{self.heard!r} unsupported by {self.unbiased_heard!r}")
            self._report_ignored(self.unbiased_heard, duration, peak)
            return False
        if not self.matches(heard):
            _debug(f"[{duration:.2f}s peak {peak:.4f}] heard {heard!r} -> ignored")
            self._report_ignored(heard, duration, peak)
            return False
        self.trailing = self.remainder(heard)
        _debug(f"[{duration:.2f}s peak {peak:.4f}] heard {heard!r} -> WAKE"
               + (f", command {self.trailing!r}" if self.trailing else ""))
        return True

    def _biased_wake_supported(self, corrected, unbiased):
        """Whether a prompted name correction is grounded in raw decoding.

        The prompted pass may fix ``Vanda`` to ``Vanta``. It may not turn an
        unrelated phrase into an addressed command. This is deliberately
        lexical and local: no model call, network request or stored audio.
        """
        def words(value):
            return "".join(
                ch.lower() if (ch.isalnum() or ch.isspace()) else " "
                for ch in str(value or "")).split()

        raw_words = words(unbiased)
        if not raw_words:
            return False
        name_evidence = False
        for index, token in enumerate(raw_words[:2]):
            if (token in self._accept or token in self._loose
                    or _near_name(token, self._word)):
                name_evidence = True
                break
            if index == 0 and token in self.LEAD_IN:
                name_evidence = True
        if not name_evidence:
            return False

        command = words(self.remainder(corrected))
        if not command:
            return True
        ignored = {"a", "an", "the", "is", "are", "to", "for", "of",
                   "me", "my", "please", "could", "would", "can", "you"}
        wanted = [token for token in command if token not in ignored]
        if not wanted:
            wanted = command
        raw = set(raw_words)
        overlap = sum(1 for token in wanted if token in raw)
        return overlap >= max(1, (len(wanted) + 1) // 2)

    def _prefix_is_addressed(self):
        """Whether the first couple of seconds open with the name."""
        import numpy as np
        import stt
        samples = np.concatenate(
            [np.asarray(frame, dtype=np.float32).reshape(-1)
             for frame in self._speech], axis=0)
        head = samples[:int(PREFIX_SECONDS * SAMPLE_RATE)]
        heard = stt.transcribe_samples_local(head, hotwords=self._word)
        ok = self.matches(heard)
        _debug(f"prefix check heard {heard!r} -> {'addressed' if ok else 'not addressed'}")
        return ok

    #: At most one "heard but ignored" line per this many seconds.
    REPORT_EVERY = 4.0

    def _report_ignored(self, heard, duration, peak):
        """Say what was heard and not acted on, without flooding the log.

        "I said Vanta and nothing happened" has four causes -- nothing was
        heard, something else was heard, it was heard and rejected, or it was
        never transcribed -- and from the outside they look identical. This
        was debug-only, so the one question that tells them apart, "what did
        it actually hear?", was unanswerable unless a flag had been set
        beforehand. Rate-limited, because a gate that transcribes every
        sound in a room would otherwise print all day.
        """
        import time as _time
        now = _time.monotonic()
        if now - getattr(self, "_last_report", 0.0) < self.REPORT_EVERY:
            return
        self._last_report = now
        text = str(heard or "").strip()
        if text:
            print(f"[WakeWord] Heard {text!r} ({duration:.1f}s) -- not "
                  f"addressed to '{self._word}', ignored.")
        else:
            print(f"[WakeWord] Heard something ({duration:.1f}s, level "
                  f"{peak:.3f}) but could not make out words.")

    def _name_index(self, words):
        """Where in the first two words the name is, or -1.

        Two tiers of spelling, because one list cannot serve both jobs. The
        strict spellings are only ever the name -- "vanta" -- and count on
        their own. The loose ones ("vanda", "banta", "venta") are what a
        noisy room turns the name into, and are also real names and words, so
        they count only straight after a lead-in: "Hey vanda" is an address,
        "Wanda told me to call her" is a sentence about Wanda.
        """
        if not words:
            return -1
        if words[0] in self._accept:
            return 0
        if (len(words) > 1 and words[0] in self.LEAD_IN
                and (words[1] in self._accept or words[1] in self._loose
                     or _near_name(words[1], self._word))):
            return 1
        return -1

    def remainder(self, heard):
        """Whatever was said after the name, if anything."""
        cleaned = "".join(ch.lower() if (ch.isalnum() or ch.isspace()) else " "
                          for ch in str(heard or ""))
        words = cleaned.split()
        index = self._name_index(words)
        if index < 0:
            return ""
        return " ".join(words[index + 1:]).strip()

    #: Words that may precede the name and still leave it an address.
    #: "Hey" is the word a transcriber garbles most ("e", "he", "hay") because it
    #: is a single unstressed syllable before a stressed one.
    LEAD_IN = {"hey", "ok", "okay", "yo", "hi", "hello", "um", "uh",
               "he", "e", "eh", "hay", "hei", "heyy"}

    def matches(self, heard):
        """Whether this utterance was addressed TO the assistant.

        The test is position, not length. An earlier version capped it at three
        words, which kept "I was thinking about vanta yesterday" out but also
        threw away "vanta what is the time" -- and once a command spoken in the
        same breath is honoured, length is exactly the wrong thing to measure.
        The name has to LEAD: that is what separates addressing it from talking
        about it.
        """
        cleaned = "".join(ch.lower() if (ch.isalnum() or ch.isspace()) else " "
                          for ch in str(heard or ""))
        return self._name_index(cleaned.split()) >= 0

    def reset(self):
        self._speech = []
        self._pre_roll = []
        self._quiet_for = 0.0
        self._peak = 0.0
        self._addressed = False
        self._followup_utterance = False
        self.trailing = ""
        # self.heard is deliberately NOT cleared here: reset() runs before the
        # transcript is produced, and the caller reads it after feed() returns.

    def recalibrate(self):
        """Forget the old room/device after a disconnect or silent stream.

        ``reset`` intentionally preserves the adaptive noise history during
        ordinary utterances.  A device switch is different: its gain and noise
        floor can be unrelated, and preserving ``_skip_until_quiet`` can leave
        a recovered microphone audibly live while the wake gate ignores it.
        """
        self.reset()
        self._levels = []
        self._skip_until_quiet = False
        self.heard = ""
        self.unbiased_heard = ""
        self.heard_as_followup = False


def _loose_variants(word):
    """What a noisy room turns the name into -- accepted only after a lead-in.

    Real names and words, which is exactly why they cannot stand alone.
    """
    return {
        "vanta": {"vanda", "banta", "venta", "wanta", "vantage", "van", "vanna",
                  "vanko", "vanto", "fanta"},
    }.get(word, set())


def _near_name(candidate, expected):
    """Accept a close STT spelling only after an address lead-in.

    This deliberately does not make near spellings valid as standalone wake
    words.  "Hey Vanko" is clearly an address; an unrelated person or word in
    the room is not.  The bounded edit distance generalises to new plausible
    Whisper spellings without growing an endless hard-coded phrase list.
    """
    left = str(candidate or "").casefold()
    right = str(expected or "").casefold()
    if not left or not right or abs(len(left) - len(right)) > 2:
        return False
    # Preserve the real-name boundary: "Hey Wanda" addresses Wanda, not
    # VANTA. Plausible decoder spellings such as Vanko retain the initial.
    if left[0] != right[0]:
        return False
    previous = list(range(len(right) + 1))
    for row, char_left in enumerate(left, 1):
        current = [row]
        for column, char_right in enumerate(right, 1):
            current.append(min(
                current[-1] + 1,
                previous[column] + 1,
                previous[column - 1] + (char_left != char_right),
            ))
        previous = current
    return previous[-1] <= 2


def _spelling_variants(word):
    """What a tiny model tends to write instead of the name.

    A one-word utterance gives Whisper almost no context, so it guesses at
    spelling. Refusing those guesses would make the wake word feel broken for
    reasons Himanshu cannot see.
    """
    # Only spellings that are not somebody's name or an English word. The list
    # used to include "wanda", "banta", "venta", "vanda" and "vantage" to
    # compensate for a weak transcriber -- and they are real names and a real
    # word, so "Wanda told me to call her" woke the assistant. Measured with
    # a better model the real name comes back as exactly "vanta", so the
    # compensation became the bug: seven false wakes in thirty-six sentences
    # that were never addressed to it.
    known = {
        "vanta": {"vanta", "vantaa", "vantah", "vantar"},
        "onyx": {"onyx", "onix", "oniks"},
        "vertex": {"vertex", "vortex", "verdex"},
        "argus": {"argus", "argos"},
        "talos": {"talos", "taloss", "tallos"},
        "winter": {"winter", "vinter"},
    }
    return known.get(word, {word})


def build(word=None, prefer=None):
    """The best engine available, and why it was chosen.

    Returns (engine, note). `engine` is None only when nothing at all can run,
    which means push-to-talk stays the only way in -- said out loud rather than
    failing quietly, because a wake word that silently does not exist is the
    bug this file was written to fix.
    """
    wanted = (word or identity.WAKE_WORD)
    order = [prefer] if prefer else ["openwakeword", "porcupine", "gate"]
    if prefer:
        order += [name for name in ("openwakeword", "porcupine", "gate")
                  if name != prefer]

    notes = []
    for choice in order:
        try:
            if choice == "openwakeword":
                path = custom_model_path()
                if path:
                    return (OpenWakeWordEngine(path),
                            f"custom model {os.path.basename(path)}")
                shipped = pretrained_name(wanted)
                if shipped:
                    return (OpenWakeWordEngine(shipped),
                            f"pretrained model {shipped}")
                notes.append(f"no trained model for '{wanted}'; "
                             f"shipped phrases are {', '.join(sorted(set(PRETRAINED)))}")
                continue
            if choice == "porcupine":
                key = identity.env("PICOVOICE_ACCESS_KEY") or os.getenv("PICOVOICE_ACCESS_KEY")
                path = porcupine_keyword_path()
                if not (key and path):
                    notes.append("Porcupine needs PICOVOICE_ACCESS_KEY and a .ppn file")
                    continue
                return PorcupineEngine(key, path), f"Porcupine {os.path.basename(path)}"
            if choice == "gate":
                import stt
                if not stt.gate_ready():
                    notes.append("local speech gate could not load its model")
                    continue
                return SpeechGateEngine(wanted), "local speech gate"
        except Exception as exc:
            notes.append(f"{choice} unavailable: {exc}")
    return None, "; ".join(notes) or "no engine available"


def how_to_train():
    """What to do to get the fastest engine, printed rather than assumed."""
    return (
        f"To use a purpose-trained '{identity.WAKE_WORD}' detector instead of the "
        f"local speech gate, drop either file into {WAKEWORD_DIR}:\n"
        f"  - {identity.WAKE_WORD.lower()}.onnx  (openWakeWord; train one at "
        f"https://github.com/dscripka/openWakeWord)\n"
        f"  - {identity.WAKE_WORD.lower()}.ppn   (Porcupine; generate at "
        f"console.picovoice.ai, then set PICOVOICE_ACCESS_KEY)\n"
        f"Both are lighter on CPU and a few hundred milliseconds faster. "
        f"Neither changes the privacy position: all three run on this machine.")
