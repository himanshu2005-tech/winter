# -*- coding: utf-8 -*-
"""
vocab.py -- the words Himanshu actually says, learned as he says them.

The transcriber loses on proper nouns, because a name has almost no prior
probability: large-v3-turbo got a whole sentence right and still wrote
"NXT wave" for NxtWave and "Havelock" for Haveloc. Biasing it with hotwords
fixes that outright, but only for words somebody remembered to list. A
hand-maintained list decays the moment a new course, tool or person appears.

So the list maintains itself. With one rule that matters more than the rest.

THE RULE: SPELLINGS COME FROM GROUND TRUTH, FREQUENCY COMES FROM ANYWHERE
-------------------------------------------------------------------------
Learning spellings from transcripts would be a feedback loop. Say "Haveloc"
three times, get "Havelock" three times, learn "Havelock", bias the transcriber
toward "Havelock", and now it is wrong *more* confidently and forever. That is
exactly how replayed briefings turned VANTA's own guesses into remembered facts
earlier in this project, and it is the same shape here.

So a word only ever enters the vocabulary from a source that spells it
correctly by construction:

* text Himanshu TYPES -- he spells his own course names right
* contact names from the graph
* calendar event titles, project and file names
* anything set explicitly in VANTA_STT_VOCAB

Speech can raise a known word's score, because hearing it often is evidence it
matters. Speech can never introduce a new spelling.

WHY THE LIST STAYS SHORT
------------------------
Hotwords are a bias, not a dictionary. Fifty rare words sharpen decoding; five
hundred, most of them ordinary English, dilute it into noise. So candidates must
be rare (common words are dropped), long enough to be worth biasing, and ranked
by how often they actually come up.
"""

import json
import os
import re
import threading
import time

# Ordinary English earns no bias: nudging the decoder toward "because" helps
# nothing and spends the budget that "NxtWave" needed. This is deliberately a
# short list of the words that actually show up in spoken requests, not a
# dictionary -- anything missing merely wastes one slot.
COMMON = frozenset("""
about after again against all also always and another any anything are around
ask asked back because been before being below between both bring but call came
can cannot come could day days did does doing done down during each even ever
every everything find first for from get give going good got great had has have
having help her here him his how however into just keep know last let like
little long look made make many may maybe mean might more most much must need
never next night not nothing now off once only open other our out over own
part people place please put ran read really right said same say see seem send
set she should show side since some something sound still such take tell than
that the their them then there these they thing think this those though three
through time today together too took two under until use used using very want
was way week well went were what when where which while who why will with
within without word work would year yes your
vanta sir okay yeah yep nope thanks thank
add create delete remove open close check search look play pause stop start
write save send email message reply remind schedule cancel list show tell
calendar event task note file folder screen window browser tab page link
morning evening tomorrow yesterday tonight minute minutes hour hours
please again everything anything someone somebody
""".split())

_LOCK = threading.RLock()
_CACHE = None
_CACHE_AT = 0.0
CACHE_SECONDS = 20.0

#: How many words are handed to the transcriber. Past this the bias thins out.
MAX_HOTWORDS = int(os.getenv("VANTA_VOCAB_LIMIT", "40"))
#: Shorter than this and a mis-transcription is not what went wrong.
MIN_LENGTH = 4
#: A word has to come up this often before it is worth a slot.
MIN_SCORE = float(os.getenv("VANTA_VOCAB_MIN_SCORE", "2"))

_WORD = re.compile(r"[A-Za-z][A-Za-z0-9._-]{2,}")


def store_path(base_dir):
    return os.path.join(base_dir, "vault", "system", "spoken_vocab.json")


def _load(base_dir):
    path = store_path(base_dir)
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save(base_dir, data):
    path = store_path(base_dir)
    # Per-process name, so two VANTA instances cannot fight over one temp file.
    tmp = f"{path}.{os.getpid()}.tmp"
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, sort_keys=True)
        # Windows refuses a replace while anything else holds the target open,
        # and a file indexer is enough to cause it. A short retry turns a lost
        # write into a brief wait.
        for attempt in range(3):
            try:
                os.replace(tmp, path)
                return
            except OSError:
                if attempt == 2:
                    raise
                time.sleep(0.05)
    except OSError as exc:
        print(f"[Vocab] Could not save: {exc}")
    finally:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass


def _candidates(text):
    """Words from a piece of text that could be worth biasing toward."""
    found = {}
    for match in _WORD.finditer(str(text or "")):
        word = match.group(0).strip("._-")
        if len(word) < MIN_LENGTH:
            continue
        if word.lower() in COMMON:
            continue
        if word.isdigit():
            continue
        # Keep the spelling as written -- capitalisation is most of what makes
        # "NxtWave" different from "nxtwave", and the transcriber benefits from
        # the real form.
        found[word] = found.get(word, 0) + 1
    return found


#: Contacts alone would fill the whole budget -- 142 names at equal score left
#: no slot for a course, a tool or a filename. They get a share of it instead,
#: and the ones actually mentioned climb out of the quota on their own score.
CONTACT_SHARE = float(os.getenv("VANTA_VOCAB_CONTACT_SHARE", "0.5"))


def learn(base_dir, text, trusted=True, weight=1.0, source="typed",
          accumulate=True):
    """Record words from a piece of text.

    trusted=True means this source spells things correctly -- typed input,
    contact names, calendar titles -- and may introduce new words.

    trusted=False means it came from speech recognition. It can only raise the
    score of words already known, never add one. Otherwise a mis-transcription
    becomes a permanent bias toward its own mistake.
    """
    global _CACHE
    words = _candidates(text)
    if not words:
        return 0
    with _LOCK:
        data = _load(base_dir)
        changed = 0
        for word, count in words.items():
            key = word.lower()
            entry = data.get(key)
            if entry is None:
                if not trusted:
                    # Heard something new. Not learned: speech does not get to
                    # decide how a word is spelt.
                    continue
                entry = {"word": word, "score": 0.0, "seen": 0,
                         "first": time.time(), "source": source}
                data[key] = entry
            if accumulate:
                entry["score"] = float(entry.get("score", 0.0)) + count * weight
                entry["seen"] = int(entry.get("seen", 0)) + count
            else:
                # Seeding sets a floor rather than adding. Re-importing the
                # same 142 contacts on every boot would otherwise inflate them
                # without limit and squeeze out everything Himanshu actually
                # says -- the list would get worse the longer it ran.
                entry["score"] = max(float(entry.get("score", 0.0)),
                                     count * weight)
                entry["seen"] = max(int(entry.get("seen", 0)), count)
            entry["last"] = time.time()
            # Anything Himanshu actually uses stops counting as merely imported,
            # so a contact he talks about competes on equal terms.
            if source == "typed" and entry.get("source") == "contact":
                entry["source"] = "typed"
            # A trusted source also corrects the stored spelling, so a word
            # first seen in a filename becomes properly cased once typed.
            if trusted and word != entry.get("word") and word[:1].isupper():
                entry["word"] = word
            changed += 1
        if changed:
            _save(base_dir, data)
            _CACHE = None
        return changed


def learn_spoken(base_dir, text):
    """Frequency only. Cannot introduce a spelling."""
    return learn(base_dir, text, trusted=False, weight=0.5, source="spoken")


def seed(base_dir):
    """Pull in names that are already known to be spelt correctly.

    Contacts, the active project and indexed file names cost nothing to read and
    cover most of what a general transcriber will get wrong on day one.
    """
    added = 0
    try:
        from memory.contacts import get_all_contacts
        # One write, not one per contact. Calling learn() per name meant 142
        # full load-rewrite cycles racing each other, which was slow and
        # produced "[WinError 5] Access is denied" when two landed together.
        # Names are stored casefolded, so title-case them to give the
        # transcriber something that looks like a name.
        names = " ".join(str(name).title() for name in get_all_contacts())
        if names.strip():
            added += learn(base_dir, names, trusted=True, weight=2.0,
                           source="contact", accumulate=False)
    except Exception as exc:
        print(f"[Vocab] Contacts unavailable: {exc}")

    try:
        with open(os.path.join(base_dir, "vault", "system",
                               "active_project.json"), encoding="utf-8") as handle:
            project = json.load(handle)
        bits = " ".join(os.path.basename(value)
                        for value in (project or {}).values()
                        if isinstance(value, str) and len(value) < 120)
        if bits.strip():
            added += learn(base_dir, bits, trusted=True, accumulate=False)
    except Exception:
        pass

    explicit = " ".join(
        part.strip() for part in os.getenv("VANTA_STT_VOCAB", "").split(",")
        if part.strip())
    if explicit:
        added += learn(base_dir, explicit, trusted=True, weight=5.0,
                       source="explicit", accumulate=False)
    return added


def hotwords(base_dir, limit=None):
    """The words worth biasing the transcriber toward, best first."""
    global _CACHE, _CACHE_AT
    with _LOCK:
        if _CACHE is not None and time.time() - _CACHE_AT < CACHE_SECONDS:
            return list(_CACHE)
        data = _load(base_dir)
        scored = []
        for entry in data.values():
            if not isinstance(entry, dict):
                continue
            word = str(entry.get("word") or "").strip()
            score = float(entry.get("score") or 0.0)
            if not word or score < MIN_SCORE:
                continue
            scored.append((score, word, str(entry.get("source") or "typed")))
        scored.sort(key=lambda row: (-row[0], row[1].lower()))

        budget = limit or MAX_HOTWORDS
        contact_budget = max(1, int(budget * CONTACT_SHARE))
        chosen, contacts_taken = [], 0
        for _score, word, source in scored:
            if len(chosen) >= budget:
                break
            if source == "contact":
                if contacts_taken >= contact_budget:
                    continue
                contacts_taken += 1
            chosen.append(word)
        _CACHE, _CACHE_AT = chosen, time.time()
        return list(chosen)


#: How close a transcribed word has to be to a known one before it is treated
#: as the same word. One edit in five characters is a misheard consonant;
#: three edits is a different word.
_MAX_EDITS = 1
_MIN_FUZZY_LENGTH = 5


def _normalise(text):
    return "".join(ch for ch in str(text).lower() if ch.isalnum())


def _within_one_edit(a, b):
    """Whether two strings differ by at most one insert, delete or swap."""
    if abs(len(a) - len(b)) > _MAX_EDITS:
        return False
    if a == b:
        return True
    if len(a) == len(b):
        return sum(1 for x, y in zip(a, b) if x != y) <= _MAX_EDITS
    shorter, longer = (a, b) if len(a) < len(b) else (b, a)
    for cut in range(len(longer)):
        if longer[:cut] + longer[cut + 1:] == shorter:
            return True
    return False


def repair(text, words):
    """Put known spellings back into a transcript.

    The transcriber is biased with hotwords everywhere it safely can be, but
    not on the online path: Whisper conditions on a prompt and, when the audio
    is near-silence, emits the prompt back as the transcript -- so the
    instruction meant to suppress hallucination becomes one. The vocabulary
    was therefore collected and then thrown away for every online
    transcription, which is most of them.

    Correcting afterwards has none of that risk, because there is nothing to
    hallucinate from. Measured against the real models on real sentences, both
    whisper-large-v3 and its turbo variant wrote "NXT Wave" for NxtWave and
    "Havelock" for Haveloc -- the model size was not the problem, the absence
    of the names was.

    Conservative on purpose: a word is only replaced when it is already almost
    that word, so an ordinary sentence cannot be dragged toward the
    vocabulary. "I have a lock on it" keeps its lock.
    """
    original = str(text or "")
    if not original.strip() or not words:
        return original

    known = {}
    for word in words:
        key = _normalise(word)
        if len(key) >= 3:
            known.setdefault(key, word)
    if not known:
        return original

    # Split into words and the punctuation between them, so the joins and the
    # spacing survive a replacement.
    pieces = re.split(r"(\W+)", original)
    out = list(pieces)
    index = 0
    while index < len(out):
        if not out[index] or not out[index].isalnum():
            index += 1
            continue
        # Two adjacent words first: "NXT Wave" is one name split in half, and
        # repairing each half separately could never put it back together.
        if index + 2 < len(out) and out[index + 2].isalnum():
            joined = _normalise(out[index] + out[index + 2])
            match = known.get(joined)
            if match and _normalise(out[index] + out[index + 2]) != _normalise(
                    out[index]):
                out[index] = match
                out[index + 1] = ""
                out[index + 2] = ""
                index += 3
                continue
        key = _normalise(out[index])
        match = known.get(key)
        if match is None and len(key) >= _MIN_FUZZY_LENGTH:
            for candidate_key, candidate in known.items():
                if (len(candidate_key) >= _MIN_FUZZY_LENGTH
                        and _within_one_edit(key, candidate_key)):
                    match = candidate
                    break
        if match and match != out[index]:
            out[index] = match
        index += 1
    return "".join(out)


def summary(base_dir, limit=12):
    """What has been learned, for the dashboard and for answering questions."""
    data = _load(base_dir)
    rows = sorted(
        ((float(e.get("score") or 0), str(e.get("word") or ""), int(e.get("seen") or 0))
         for e in data.values() if isinstance(e, dict)),
        key=lambda r: -r[0])
    lines = [f"{len(data)} words known, biasing the top {MAX_HOTWORDS}:"]
    for score, word, seen in rows[:limit]:
        lines.append(f"  {word:24} score {score:5.1f}  heard/typed {seen}")
    return "\n".join(lines)


def forget(base_dir, word):
    """Drop a word -- for when something wrong got in from a trusted source."""
    global _CACHE
    with _LOCK:
        data = _load(base_dir)
        if data.pop(str(word).lower(), None) is None:
            return False
        _save(base_dir, data)
        _CACHE = None
        return True
