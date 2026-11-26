# -*- coding: utf-8 -*-
"""
stt.py — speech to text, online first, local when it has to be.

VANTA transcribed with a local `small.en` Whisper. That model costs about a
minute of startup before push-to-talk works at all, and `small.en` is the
weakest Whisper worth running -- it is why so many utterances came back as
empty strings or near-misses.

Groq serves whisper-large-v3-turbo in well under a second, and the key is
already configured. So: try the network, and keep the local model as the thing
that answers when there is no network.

Three properties matter here.

The local model loads lazily and in the background. Boot no longer waits for a
model that may never be used.

The online call is bounded. An unbounded transcription request is the same bug
as an unbounded chat request: VANTA appears to hang while holding the
microphone.

Falling back is silent to Himanshu and loud in the log. He should not have to
know which engine ran; the log should.
"""

import os
import threading
import time

# HuggingFace materialises a downloaded model by symlinking from its blob store
# into a snapshot directory, which on Windows needs Developer Mode or an admin
# shell. Without it the download succeeds and then the load fails with
# "WinError 1314: A required privilege is not held by the client" -- which looks
# like a broken model rather than a permissions problem. Copying instead costs
# disk and works unprivileged.
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS", "1")
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")


_MODE = os.getenv("VANTA_STT", os.getenv("WINTER_STT", "auto")).strip().lower()
# The full model, not the turbo distillation. Measured on this account over
# real sentences they are within 0.03s of each other per clip, so there is no
# latency argument for the smaller one.
_ONLINE_MODEL = os.getenv(
    "VANTA_STT_MODEL", os.getenv("WINTER_STT_MODEL", "whisper-large-v3"))
_TIMEOUT = float(os.getenv(
    "VANTA_STT_TIMEOUT", os.getenv("WINTER_STT_TIMEOUT", "20")))

_local_model = None
_local_lock = threading.RLock()
_local_failed = False
_online_broken_until = 0.0


def _groq_key():
    return os.getenv("GROQ_API_KEY", "").strip()


def online_available():
    return _MODE != "local" and bool(_groq_key()) and time.monotonic() >= _online_broken_until


def _load_local():
    """The fallback engine. Loaded on first need, not at boot."""
    global _local_model, _local_failed
    with _local_lock:
        if _local_model is not None or _local_failed:
            return _local_model
        try:
            # The offline fallback had the same constructor-only check, so on
            # this machine it would have reported itself ready and then failed
            # on the first word spoken with the network down -- the one moment
            # it exists for.
            # small.en was the fallback for a machine with no GPU in mind.
            # There is a 3050 here, and large-v3-turbo is the same model the
            # online path uses -- so the local route stops being a downgrade and
            # becomes a private, network-independent equal. distil-large-v3.5 is
            # the alternative if VRAM gets tight.
            size = os.getenv("VANTA_STT_LOCAL_MODEL",
                             os.getenv("WINTER_STT_LOCAL_MODEL", "large-v3-turbo"))
            _local_model = _load_whisper(size, "Local fallback")
        except Exception as exc:
            _local_failed = True
            print(f"[STT] Local engine could not be loaded: {exc}")
        return _local_model


def warm_local(background=True):
    """Prepare the fallback without blocking startup."""
    if _MODE == "online":
        return
    if background:
        threading.Thread(target=_load_local, daemon=True, name="stt-warm").start()
    else:
        _load_local()


def ready():
    """Whether anything can transcribe right now.

    Push-to-talk used to be gated on the local model being loaded, which meant
    a minute of dead keypresses at every boot even when the network was fine.
    """
    if online_available():
        return True
    return _load_local() is not None


def _transcribe_online(path):
    from groq import Groq
    client = Groq(api_key=_groq_key(), timeout=_TIMEOUT)
    with open(path, "rb") as handle:
        payload = handle.read()
    # Deliberately no `prompt`. Whisper conditions on it and, when the audio is
    # near-silence, simply emits the prompt back as the transcript -- so an
    # instruction meant to suppress hallucination becomes one. Silence is
    # handled after the fact instead.
    result = client.audio.transcriptions.create(
        file=(os.path.basename(path), payload),
        model=_ONLINE_MODEL,
        language="en",
        temperature=0,
    )
    return (getattr(result, "text", "") or "").strip()


def vocabulary():
    """Words worth biasing the transcriber toward.

    Proper nouns are where a general model reliably loses: large-v3-turbo got
    an entire sentence right and still wrote "NXT wave" for NxtWave, because the
    name has almost no prior probability. Hotwords cost nothing and fix that
    class of error.

    Configured rather than hard-coded, because whose names matter is not
    something this file can know. The assistant's own name is included because
    it opens a great many utterances.
    """
    import identity
    words = [identity.NAME]
    raw = os.getenv("VANTA_STT_VOCAB", "")
    words.extend(part.strip() for part in raw.split(",") if part.strip())
    # Whatever has been learned from typed input, contacts and project names.
    # Wrapped because a transcription must never fail over its own vocabulary.
    try:
        import vocab
        base = os.path.dirname(os.path.abspath(__file__))
        words.extend(vocab.hotwords(base))
    except Exception as exc:
        print(f"[Vocab] Unavailable: {exc}")
    # Order preserved, duplicates dropped.
    seen, unique = set(), []
    for word in words:
        if word.lower() not in seen:
            seen.add(word.lower())
            unique.append(word)
    return unique


def _transcribe_local(path):
    model = _load_local()
    if model is None:
        return ""
    extra = {}
    words = vocabulary()
    if words:
        extra["hotwords"] = " ".join(words)
    segments, _info = model.transcribe(
        path, beam_size=5, language="en",
        vad_filter=True, vad_parameters={"min_silence_duration_ms": 300},
        **extra,
    )
    return "".join(segment.text for segment in segments).strip()


# ------------------------------------------------------------------ #
# The wake-word gate.
#
# Deliberately separate from everything above, and deliberately narrower: it is
# LOCAL ONLY, IN MEMORY ONLY, and uses its own tiny model. Audio heard before
# Himanshu has said the wake word must never reach a network or a disk, so this
# path cannot fall back to Groq, cannot be switched online by configuration,
# and never takes a file path. It also uses a tiny model rather than small.en,
# because recognising one word every time somebody speaks in the room has to be
# cheap or it is not worth having.
# ------------------------------------------------------------------ #
_gate_model = None
_gate_failed = False
_gate_failed_at = 0.0
_gate_lock = threading.Lock()
GATE_RETRY_SECONDS = float(os.getenv("VANTA_WAKE_MODEL_RETRY_SECONDS", "60"))


def _works(model):
    """Whether this model can actually transcribe, not merely be constructed.

    faster-whisper builds happily on CUDA and then fails at the first inference
    with "cublas64_12.dll is not found", because the CUDA runtime libraries are
    a separate install from the driver. Checking only the constructor meant the
    engine reported itself ready and then returned an empty string for every
    utterance -- which, for a wake word, is indistinguishable from never being
    spoken to. One second of silence is enough to find out.
    """
    try:
        import numpy as np
        segments, _info = model.transcribe(np.zeros(16000, dtype="float32"),
                                           beam_size=1, language="en",
                                           vad_filter=False)
        list(segments)      # generators defer the work until consumed
        return True
    except Exception as exc:
        print(f"[STT] Device rejected at first use ({exc}); falling back.")
        return False


def _cuda_available():
    """Whether CUDA can really be used, having first made its libraries findable.

    ctranslate2 needs cublas64_12.dll and cudnn on the DLL search path. On this
    machine they ship inside the torch wheel rather than a system CUDA install,
    so they are only discoverable once torch has been imported -- which is why
    the GPU worked inside the app (torch loads early for Kokoro) and failed in
    any standalone script. That made the fast path depend on import order, so
    the same model ran on the GPU or 25x slower on the CPU depending on what
    else happened to have been imported. Importing torch here on purpose makes
    it deterministic.
    """
    try:
        import torch
        return bool(torch.cuda.is_available())
    except Exception:
        return False


def _cpu_size(size):
    """A model that is actually usable without a GPU.

    large-v3-turbo on this CPU took 6.8 seconds for a 7.3 second clip -- about
    real time, which for dictation means waiting as long as you spoke. Falling
    back to the big model is worse than falling back to a smaller one.
    """
    if any(token in str(size) for token in ("large", "medium", "turbo")):
        return os.getenv("VANTA_STT_CPU_MODEL", "small.en")
    return size


def _load_whisper(size, label, compute=None):
    """A working model on the fastest device that can actually run it.

    compute defaults to int8_float16 on the GPU rather than float16. On a 4 GB
    card that is the difference between large-v3-turbo fitting alongside the
    wake gate and not: roughly 0.9 GB against 1.6 GB, for a difference in
    accuracy that is not audible on dictation.
    """
    from faster_whisper import WhisperModel
    wanted = compute or os.getenv("VANTA_STT_COMPUTE", "int8_float16")
    try:
        if not _cuda_available():
            raise RuntimeError("no usable CUDA device")
        model = WhisperModel(size, device="cuda", compute_type=wanted)
        if _works(model):
            print(f"[STT] {label} ready ({size}, CUDA, {wanted}).")
            return model
    except Exception as exc:
        # The retry below is CPU, so it only helps if the GPU was the problem.
        # A download or permissions failure will fail there too, and saying
        # "CUDA unavailable" for it sends the next person hunting drivers.
        detail = str(exc)
        if "WinError 1314" in detail or "privilege" in detail.lower():
            print(f"[STT] {label}: the model could not be unpacked -- Windows "
                  f"refused the symlink HuggingFace wanted. Retrying; if this "
                  f"persists, enable Developer Mode. ({detail[:120]})")
        elif any(word in detail.lower() for word in
                 ("cuda", "cublas", "cudnn", "gpu", "device", "memory")):
            print(f"[STT] CUDA unavailable for {label} ({detail[:160]}).")
        else:
            print(f"[STT] {label} could not load on the GPU ({detail[:160]}).")
    fallback = _cpu_size(size)
    if fallback != size:
        print(f"[STT] {label}: {size} needs a GPU to be usable, so the CPU "
              f"fallback is {fallback}.")
    model = WhisperModel(fallback, device="cpu", compute_type="int8")
    if not _works(model):
        raise RuntimeError("neither CUDA nor CPU could transcribe")
    print(f"[STT] {label} ready ({fallback}, CPU).")
    return model


def gate_model_name():
    # tiny.en was measured hallucinating filler on sub-second clips of a made-up
    # name -- "in doubt", "and", "grant", "and I'm back" for six attempts at
    # "Vanta". It is not mishearing, it is guessing, because Whisper is trained
    # on 30-second windows and a half-second clip gives it nothing to go on.
    # base.en is roughly three times the compute and dramatically better on
    # single words; small.en is better still if the CPU can spare it.
    # Deliberately NOT the big model: this runs on every utterance in the
    # room, where the main transcriber runs once per deliberate request.
    #
    # small.en since measured against base.en on this machine, on twelve voices
    # saying "Vanta", "Hey Vanta" and a command after it:
    #
    #     base.en    15/24 recognised   median  90 ms
    #     small.en   23/24 recognised   median 180 ms
    #
    # The failures were not random. base.en wrote "Hey" as "k" ("kvanta",
    # "k vanta"), dropped the name from "Hey Vanta, what's the time?" and
    # returned nothing at all for one voice -- worst on Indian-accented speech,
    # which is the one that matters. The comment above predates the GPU: ninety
    # milliseconds more, spent only when somebody speaks, is the cheapest
    # accuracy in the system.
    #
    # large-v3-turbo since, measured the same way on a harder set (five
    # accents, ordinary desk-distance speech, a command after the name):
    #
    #     small.en          25/30   (28/30 with a name prompt)
    #     large-v3-turbo    26/30   (30/30 with a name prompt)
    #
    # The big model is already resident for the local transcription fallback,
    # so the gate SHARES that instance (see _load_gate) -- no second copy in
    # a 4 GB card, and the cost is a few hundred milliseconds per utterance
    # that begins with someone speaking.
    return os.getenv("VANTA_WAKE_STT_MODEL",
                     os.getenv("WINTER_WAKE_STT_MODEL", "large-v3-turbo"))


#: What to fall back to if the preferred gate model cannot be loaded.
#:
#: This machine has 15.7 GB of RAM and runs with about 2 GB free: an IDE's
#: type checker alone holds 2.2 GB. A larger gate model is worth having --
#: small.en recognised 23 of 24 attempts where base.en recognised 15 -- but
#: not at the price of no wake word at all, which is what "cannot allocate
#: memory" would otherwise mean. A weaker listener that loads beats a better
#: one that does not.
GATE_FALLBACKS = ("small.en", "base.en")


def gate_candidates():
    """The models to try, in order, without repeats."""
    chain = [gate_model_name()]
    for name in GATE_FALLBACKS:
        if name not in chain:
            chain.append(name)
    return chain


def _load_gate():
    """The local model used only to listen for the wake word."""
    global _gate_model, _gate_failed, _gate_failed_at
    import time
    # A startup memory/driver/device failure is not permanent.  Previously one
    # failed load disabled the hands-free listener until the whole assistant
    # was restarted, even after memory freed or Windows resumed.
    if (_gate_failed and _gate_failed_at
            and time.monotonic() - _gate_failed_at >= GATE_RETRY_SECONDS):
        _gate_failed = False
    if _gate_model is not None or _gate_failed:
        return _gate_model
    with _gate_lock:
        if _gate_model is not None or _gate_failed:
            return _gate_model
        last_error = None
        for index, name in enumerate(gate_candidates()):
            try:
                local_name = os.getenv(
                    "VANTA_STT_LOCAL_MODEL",
                    os.getenv("WINTER_STT_LOCAL_MODEL", "large-v3-turbo"))
                if name == local_name:
                    # One copy of the big model serves both jobs.
                    _gate_model = _load_local()
                    if _gate_model is None:
                        raise RuntimeError("the shared local model did not load")
                    print(f"[STT] Wake gate shares the local model ({name}).")
                else:
                    _gate_model = _load_whisper(name, "Wake gate")
                if index:
                    print(f"[STT] Wake gate is running on {name}, not "
                          f"{gate_candidates()[0]}: the preferred model "
                          f"could not be loaded ({last_error}). Recognition "
                          f"will be weaker until memory frees up.")
                return _gate_model
            except Exception as exc:
                last_error = exc
                print(f"[STT] Wake gate model {name} did not load: {exc}")
        _gate_failed = True
        _gate_failed_at = time.monotonic()
        print(f"[STT] Wake gate unavailable: {last_error}")
        return _gate_model


def retry_gate_after_failure():
    """Allow the audio watchdog to retry a detector that failed at runtime."""
    global _gate_model, _gate_failed, _gate_failed_at
    with _gate_lock:
        _gate_model = None
        _gate_failed = False
        _gate_failed_at = 0.0


def warm_gate(background=True):
    if background:
        threading.Thread(target=_load_gate, daemon=True, name="stt-gate").start()
    else:
        _load_gate()


def gate_ready():
    return _load_gate() is not None


#: Below this average log-probability the model was guessing, not hearing.
GATE_MIN_LOGPROB = float(os.getenv("VANTA_WAKE_MIN_LOGPROB", "-1.1"))
#: Above this probability of "nobody actually spoke", discard the transcript.
GATE_MAX_NO_SPEECH = float(os.getenv("VANTA_WAKE_MAX_NO_SPEECH", "0.6"))
#: Whisper behaves badly on very short audio, so pad it out.
GATE_PAD_SECONDS = float(os.getenv("VANTA_WAKE_PAD", "1.0"))


def _contains_speech(audio, threshold=0.35, min_ms=100):
    """Whether a voice-activity detector hears a person in this clip.

    Uses the Silero model that ships with faster-whisper, so nothing is added
    to install. Fails open: if the detector itself errors, the clip is
    decoded rather than silently never heard.
    """
    try:
        from faster_whisper.vad import VadOptions, get_speech_timestamps
        options = VadOptions(threshold=threshold, min_speech_duration_ms=min_ms)
        return bool(get_speech_timestamps(audio, options))
    except Exception as exc:
        print(f"[STT] Voice-activity check unavailable ({exc}); decoding anyway.")
        return True


def transcribe_samples_local(samples, hotwords=None, return_unbiased=False):
    """Text for float32 mono 16 kHz samples, using the local gate model.

    Takes an array rather than a path on purpose: nothing heard before the wake
    word is ever written to disk, so there is no file for anything else to read
    and nothing to forget to delete.

    Three things beyond a plain transcribe, all aimed at one failure -- a short
    clip of an invented word, where the model reliably produced confident
    nonsense:

    * The clip is padded with silence. Whisper is trained on 30-second windows
      and a half-second of speech at the very start of one is pathological.
    * `hotwords` biases decoding toward the name. A made-up word has almost no
      prior probability, so without a nudge the decoder prefers any common
      English phrase that fits the sounds -- which is exactly what "and I'm
      back" was.
    * Low-confidence output is discarded rather than returned. Hallucinations
      score badly on avg_logprob and no_speech_prob, so the numbers the model
      already computes are enough to tell guessing from hearing.
    """
    model = _load_gate()
    if model is None:
        return ("", "") if return_unbiased else ""
    try:
        import numpy as np
        audio = np.asarray(samples, dtype="float32")
        # A quiet voice is not a different word, but Whisper's accuracy falls
        # off sharply on low-level input. Bring the clip's peak up to a normal
        # level (never down, and never by more than 40x so silence is not
        # turned into a roar).
        peak = float(np.max(np.abs(audio))) if audio.size else 0.0
        if peak < 1e-3:
            # Nothing there. A large model given a hotword and digital silence
            # will happily write the hotword, which is the name -- so silence
            # is never decoded at all.
            return ("", "") if return_unbiased else ""
        if peak < 0.3:
            audio = audio * min(0.6 / peak, 40.0)
        pad = int(max(0.0, GATE_PAD_SECONDS) * 16000)
        if pad:
            quiet = np.zeros(pad, dtype="float32")
            audio = np.concatenate([quiet, audio, quiet])
        if not _contains_speech(audio):
            # Noise, a hum, a door. A large model given the name as a hotword
            # will write the name for them -- measured: white noise decoded as
            # "Vanta" -- so only audio that a voice-activity detector calls
            # speech is decoded at all.
            return ("", "") if return_unbiased else ""

        def decode(prompt=None, use_hotwords=False):
            extra = {}
            # The first pass must answer "did a person say words?" without
            # being told the desired answer. Supplying Vanta as a hotword on
            # that pass made fan/public noise occasionally decode as exactly
            # "Vanta". Bias is reserved for the spelling-correction pass,
            # after an unbiased pass has already heard real words.
            if hotwords and use_hotwords:
                extra["hotwords"] = str(hotwords)
            if prompt:
                extra["initial_prompt"] = prompt
            segments, info = model.transcribe(
                audio, beam_size=5, language="en", vad_filter=False,
                condition_on_previous_text=False,
                no_speech_threshold=GATE_MAX_NO_SPEECH,
                temperature=0.0, **extra)
            kept = []
            for segment in segments:
                logprob = getattr(segment, "avg_logprob", 0.0) or 0.0
                no_speech = getattr(segment, "no_speech_prob", 0.0) or 0.0
                if logprob < GATE_MIN_LOGPROB or no_speech > GATE_MAX_NO_SPEECH:
                    continue
                kept.append(segment.text)
            return "".join(kept).strip()

        first = decode()
        if not hotwords or not first:
            return (first, first) if return_unbiased else first
        name = str(hotwords).strip()
        if name.lower() in first.lower():
            return (first, first) if return_unbiased else first
        # Words were heard but the name was not among them: the usual case is
        # a made-up word written as "Vanda", "Vanna" or "van". Spelling it in a
        # prompt is the strongest nudge Whisper takes (28/30 -> 30/30 measured
        # on small.en -> large-v3-turbo). It is a SECOND pass, and only when
        # the first pass heard real words, because a prompt on its own makes the
        # decoder echo the name into silence and noise -- measured, and the
        # reason it is not used on every clip.
        title = name.title()
        # This prompt must contain the NAME and nothing that could be executed
        # as a command.  Including "what is the time" here caused Whisper to
        # reproduce that example over fan/TV noise; the wake layer then saw a
        # perfectly formed addressed fast command and announced the time even
        # though nobody had asked.  Repeating only the spelling gives Whisper
        # the same name correction without supplying an executable sentence.
        second = decode(
            f"Hey {title}. {title}. Okay {title}.",
            use_hotwords=True)
        final = second or first
        # The spelling-biased pass exists solely to recover the assistant's
        # invented name.  It must never be treated as an ordinary command:
        # its prompt contains example words and, on fan/TV noise, Whisper can
        # echo one of those examples.  The wake engine uses ``final`` to match
        # Vanta and exposes ``first`` for nameless conversational follow-ups.
        return (final, first) if return_unbiased else final
    except Exception as exc:
        print(f"[STT] Wake gate transcription failed: {exc}")
        return ("", "") if return_unbiased else ""


def recover_fast_command(samples):
    """Recover a closed-set command when the primary STT returns no text.

    Short control words are exactly where hosted transcription is most likely
    to return an empty result: there is almost no linguistic context. The wake
    gate is already local and warm, so give that same clip one bounded second
    opinion. Only text accepted by ``fast_commands`` is returned; ordinary
    speech can never escape this function and reach the agent/model.
    """
    try:
        import fast_commands
        hotwords = "mute unmute pause resume stop next previous louder quieter"
        heard = transcribe_samples_local(samples, hotwords=hotwords)
        heard = str(heard or "").strip()
        if heard and fast_commands.resolve(heard) is not None:
            print(f"[STT] local control recovery: {heard!r}")
            return heard
    except Exception as exc:
        print(f"[STT] local control recovery failed: {exc}")
    return ""


# Whisper hallucinates these over silence rather than returning nothing.
_HALLUCINATIONS = {
    "you", "thank you", "thanks for watching", "thank you for watching",
    "bye", "bye.", ".", "...", "thanks", "okay", "uh", "um",
    "please subscribe", "subscribe", "you're welcome",
}


def _is_noise(text):
    cleaned = " ".join(str(text or "").lower().split()).strip(" .,!?")
    return not cleaned or cleaned in _HALLUCINATIONS


def _repaired(text):
    """Put known spellings back, after the model has done its guessing.

    The hotword list is used everywhere it safely can be, but not on the
    online path -- Whisper conditions on a prompt and echoes it back when the
    audio is near-silence, so the instruction against hallucination becomes
    one. The vocabulary was therefore collected and discarded for most
    transcriptions.

    Repairing afterwards carries none of that risk. Measured on real
    sentences, whisper-large-v3 and its turbo variant both wrote "NXT Wave"
    for NxtWave and "Havelock" for Haveloc: the names were missing, not the
    model's capacity.
    """
    try:
        import vocab
        return vocab.repair(text, vocabulary())
    except Exception as exc:
        print(f"[STT] Vocabulary repair unavailable: {exc}")
        return text


def transcribe(path):
    """Text for an audio file, or "" if there was nothing worth hearing."""
    global _online_broken_until
    if not path or not os.path.exists(path):
        return ""

    if online_available():
        started = time.perf_counter()
        try:
            text = _transcribe_online(path)
            elapsed = (time.perf_counter() - started) * 1000
            if _is_noise(text):
                print(f"[STT] online: nothing said ({elapsed:.0f}ms)")
                return ""
            text = _repaired(text)
            print(f"[STT] online ({elapsed:.0f}ms): '{text}'")
            return text
        except Exception as exc:
            # Back off briefly so every utterance in an outage does not pay the
            # timeout again before reaching the local engine.
            _online_broken_until = time.monotonic() + 60
            print(f"[STT] Online transcription failed ({type(exc).__name__}: {exc}); "
                  "using the local model for the next minute.")

    if _MODE == "online":
        return ""
    started = time.perf_counter()
    try:
        text = _transcribe_local(path)
    except Exception as exc:
        print(f"[STT error] {exc}")
        return ""
    elapsed = (time.perf_counter() - started) * 1000
    if _is_noise(text):
        print(f"[STT] local: nothing said ({elapsed:.0f}ms)")
        return ""
    text = _repaired(text)
    print(f"[STT] local ({elapsed:.0f}ms): '{text}'")
    return text


def describe():
    local = os.getenv("VANTA_STT_LOCAL_MODEL",
                      os.getenv("WINTER_STT_LOCAL_MODEL", "large-v3-turbo"))
    if _MODE == "local":
        return f"local only ({local})"
    if _MODE == "online":
        return f"online only ({_ONLINE_MODEL})"
    if _groq_key():
        return f"online ({_ONLINE_MODEL}), local {local} as fallback"
    return f"local {local} (no online key configured)"
