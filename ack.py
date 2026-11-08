# -*- coding: utf-8 -*-
"""
ack.py -- the instant "on it" before a task, in a voice that is not canned.

THE PROBLEM
-----------
A request that needs work takes a while before VANTA says anything: the router,
the planner and the executor are model calls, and on this gateway a model call
is measured in tens of seconds. Silence for that long reads as being ignored.
A spoken "on it" fixes it -- but only if it is spoken immediately, and nothing
that waits on a model can be immediate.

THE ANSWER
----------
Write the acknowledgements AHEAD of time, not at the moment they are needed.
While the machine is idle the model is asked for a pool of short lines in the
tone VANTA is meant to have (relaxed, a bit dry, an office colleague rather
than a butler), each is rendered to audio once and kept on disk, and playing
one is then a file read. Nothing is hardcoded: every line is the model's, shaped
by the persona, refreshed when the persona changes or the pool runs low, and
rotated so the same one is not heard twice in a row.

If no audio could be prepared (offline, voice engine down) there is no
acknowledgement at all. A late "on it" is worse than none, so there is no
fallback that synthesises on demand.

WHAT MAY BE SAID
----------------
A line is an acknowledgement and nothing else: no result, no promise of one, no
question. "Done" or "found it" said before any work would be a false report, so
lines that sound like completions are rejected at generation time; the model's
cooperation is not relied on for that.
"""

import hashlib
import json
import os
import random
import re
import shutil
import threading
import time

POOL_FILE = os.path.join("vault", "ack_pool.json")
AUDIO_DIR = os.path.join("vault", "ack_audio")

TARGET_LINES = 24
MIN_READY = 8
MAX_LINES = 48
MAX_WORDS = 6
MAX_CHARS = 44
REFRESH_AFTER_SECONDS = 14 * 24 * 3600
#: Never two acknowledgements in quick succession, and not the same one again
#: until several others have been used.
COOLDOWN_SECONDS = 6.0
REMEMBER_RECENT = 6

_COMPLETION = re.compile(
    r"\b(done|finished|complete[d]?|found (it|them|that)|here (it|you) (is|are)|"
    r"all set|sorted|ready|success|failed|sorry|error|cannot|can't|won't)\b",
    re.IGNORECASE)
_NOT_SPEECH = re.compile(r"[0-9@#<>{}\[\]\\/_*]|https?:|www\.")

_lock = threading.Lock()
_recent = []
_last_played = 0.0


# --------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------- #
def clean_line(value):
    """A usable acknowledgement, or '' if the text is not one."""
    text = " ".join(str(value or "").replace("’", "'").split()).strip(" \"'`")
    if not text or len(text) > MAX_CHARS:
        return ""
    words = re.findall(r"[A-Za-z']+", text)
    if not 1 <= len(words) <= MAX_WORDS:
        return ""
    if "?" in text or _NOT_SPEECH.search(text) or _COMPLETION.search(text):
        return ""
    # Non-Latin letters and emoji are not read well by every voice.
    if re.search(r"[^\x20-\x7e]", text):
        return ""
    if text[-1] not in ".!":
        text += "."
    return text


def _key(text):
    return re.sub(r"[^a-z]+", " ", text.lower()).strip()


def parse_lines(raw, existing=()):
    """Validated, de-duplicated lines out of a model reply (JSON or lines)."""
    data = None
    try:
        import inference
        data = inference._extract_json(raw)
    except Exception:
        data = None
    candidates = []
    if isinstance(data, dict):
        candidates = data.get("lines") or []
    elif isinstance(data, list):
        candidates = data
    if not candidates:
        candidates = str(raw or "").splitlines()
    seen = {_key(item) for item in existing}
    out = []
    for item in candidates:
        if not isinstance(item, str):
            continue
        line = clean_line(re.sub(r"^\s*(?:[-*\d.)]+\s*)", "", item))
        if line and _key(line) not in seen:
            seen.add(_key(line))
            out.append(line)
    return out


# --------------------------------------------------------------------- #
# Generation
# --------------------------------------------------------------------- #
_SYSTEM = (
    "You write very short spoken acknowledgements for a voice assistant. "
    "Reply with JSON only.")

_PROMPT = """The assistant has just been asked to do something that will take a
little while, and it wants to say one tiny thing straight away so the person
knows it heard them.

How it should sound:
{tone}

Write {count} different acknowledgements.
- 1 to {max_words} words each, said out loud, no punctuation tricks.
- Relaxed, alive, and a bit dry, the way a sharp colleague answers across a
  desk. Some should carry quick situational wit, while the rest feel naturally
  conversational. Never silly, theatrical, or submissive.
- They acknowledge the request only. Do not say anything is done, found or
  ready, do not apologise, do not ask a question, do not mention the task.
- No digits, no emoji, no names, no quotation marks.
- Vary the wording; avoid starting more than a few with the same word.
{existing}
Return {{"lines": ["...", "..."]}}"""


def _tone_text(base_dir):
    try:
        import persona
        return persona.prompt_block(base_dir) or ""
    except Exception:
        return ""


def _tone_hash(base_dir):
    return hashlib.sha1(_tone_text(base_dir).encode("utf-8")).hexdigest()[:12]


def generate(base_dir, call_llm, count=TARGET_LINES, existing=()):
    """New lines from the model, validated. Empty list if it cannot."""
    existing_block = ""
    if existing:
        existing_block = ("Already in use (do not repeat or paraphrase closely):\n"
                          + "\n".join(f"- {line}" for line in list(existing)[:30]) + "\n")
    prompt = _PROMPT.format(
        tone=_tone_text(base_dir) or "Easygoing and friendly.",
        count=count, max_words=MAX_WORDS, existing=existing_block)
    try:
        raw = call_llm([{"role": "system", "content": _SYSTEM},
                        {"role": "user", "content": prompt}],
                       task_level="routine")
    except TypeError:
        raw = call_llm([{"role": "system", "content": _SYSTEM},
                        {"role": "user", "content": prompt}])
    except Exception as exc:
        print(f"[Ack] Could not write acknowledgements: {exc}")
        return []
    return parse_lines(raw, existing)


# --------------------------------------------------------------------- #
# Storage
# --------------------------------------------------------------------- #
def _pool_path(base_dir):
    return os.path.join(base_dir, POOL_FILE)


def load(base_dir):
    try:
        with open(_pool_path(base_dir), encoding="utf-8") as handle:
            data = json.load(handle)
        if isinstance(data, dict) and isinstance(data.get("lines"), list):
            return data
    except (OSError, ValueError):
        pass
    return {"lines": [], "tone": "", "voice": "", "generated_at": 0}


def save(base_dir, pool):
    path = _pool_path(base_dir)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temp = path + ".tmp"
    with open(temp, "w", encoding="utf-8") as handle:
        json.dump(pool, handle, indent=1)
    os.replace(temp, path)


def _audio_name(text, voice):
    return hashlib.sha1(f"{voice}|{text}".encode("utf-8")).hexdigest()[:16]


def ready_entries(base_dir, pool=None):
    pool = pool if pool is not None else load(base_dir)
    ready = []
    for entry in pool.get("lines", []):
        audio = entry.get("audio")
        if audio and os.path.exists(os.path.join(base_dir, audio)):
            ready.append(entry)
    return ready


def needs_refresh(base_dir, voice="", now=None):
    pool = load(base_dir)
    now = now if now is not None else time.time()
    if len(ready_entries(base_dir, pool)) < MIN_READY:
        return True
    if pool.get("tone") != _tone_hash(base_dir):
        return True
    if pool.get("voice") != voice:
        return True
    return now - float(pool.get("generated_at") or 0) > REFRESH_AFTER_SECONDS


def refresh(base_dir, call_llm, synth, voice="", force=False):
    """Bring the pool up to date. Returns how many lines have audio.

    `synth(text)` returns a path to an audio file or None; it is injected so
    this module knows nothing about voice engines.
    """
    with _lock:
        if not force and not needs_refresh(base_dir, voice):
            return len(ready_entries(base_dir))
        pool = load(base_dir)
        tone, changed_voice = _tone_hash(base_dir), pool.get("voice") != voice
        if pool.get("tone") != tone or changed_voice:
            # A new persona or voice makes the old lines wrong, not just old.
            for entry in pool.get("lines", []):
                _forget_audio(base_dir, entry)
            keep = [] if pool.get("tone") != tone else pool.get("lines", [])
            for entry in keep:
                entry.pop("audio", None)
            pool["lines"] = keep
        existing = [entry["text"] for entry in pool["lines"]]
        wanted = TARGET_LINES - len([e for e in pool["lines"]])
        if wanted > 0:
            for line in generate(base_dir, call_llm, max(wanted, 8), existing):
                pool["lines"].append({"text": line})
        pool["lines"] = pool["lines"][-MAX_LINES:]
        os.makedirs(os.path.join(base_dir, AUDIO_DIR), exist_ok=True)
        for entry in pool["lines"]:
            audio = entry.get("audio")
            if audio and os.path.exists(os.path.join(base_dir, audio)):
                continue
            made = None
            try:
                made = synth(entry["text"])
            except Exception as exc:
                print(f"[Ack] Could not voice '{entry['text']}': {exc}")
            if not made or not os.path.exists(made):
                continue
            extension = os.path.splitext(made)[1] or ".mp3"
            relative = os.path.join(
                AUDIO_DIR, _audio_name(entry["text"], voice) + extension)
            shutil.copyfile(made, os.path.join(base_dir, relative))
            entry["audio"] = relative
        pool["tone"], pool["voice"] = tone, voice
        pool["generated_at"] = time.time()
        save(base_dir, pool)
        return len(ready_entries(base_dir, pool))


def _forget_audio(base_dir, entry):
    audio = entry.get("audio")
    if audio:
        try:
            os.remove(os.path.join(base_dir, audio))
        except OSError:
            pass


# --------------------------------------------------------------------- #
# Choosing and playing
# --------------------------------------------------------------------- #
def pick(base_dir, now=None, rng=random):
    """The next acknowledgement to play, or None.

    None when the pool is empty, when one was played moments ago, or when
    every line would repeat a recent one.
    """
    global _last_played
    now = now if now is not None else time.time()
    with _lock:
        if now - _last_played < COOLDOWN_SECONDS:
            return None
        entries = ready_entries(base_dir)
        if not entries:
            return None
        fresh = [e for e in entries if e["text"] not in _recent] or entries
        chosen = rng.choice(fresh)
        _recent.append(chosen["text"])
        del _recent[:-REMEMBER_RECENT]
        _last_played = now
        return {"text": chosen["text"],
                "path": os.path.join(base_dir, chosen["audio"])}


def reset_state():
    """For tests."""
    global _last_played
    with _lock:
        _recent.clear()
        _last_played = 0.0


#: Words that carry no request. "thanks" or "hi" is not a task and must not be
#: answered with "on it". The test is deliberately about the utterance's shape,
#: not about topics: anything that is more than a pleasantry gets an
#: acknowledgement.
_PLEASANTRY = re.compile(
    r"^(?:hi|hey|hello|yo|thanks|thank you|thx|ok|okay|cool|nice|great|got it|"
    r"sure|yes|no|yeah|yep|nope|good (?:morning|night|evening|afternoon)|bye|"
    r"goodbye|never ?mind|stop|cancel|wait|hmm+|lol|haha|please|sorry)\W*$",
    re.IGNORECASE)


def worth_acknowledging(text):
    words = re.findall(r"[\w']+", str(text or ""))
    if len(words) < 3:
        return False
    return not _PLEASANTRY.match(" ".join(words))
