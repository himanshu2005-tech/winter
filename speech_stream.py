# -*- coding: utf-8 -*-
"""
speech_stream.py -- start talking before the sentence has finished being written.

VANTA said nothing until the whole reply had been generated AND synthesised.
Generation is two to six seconds and synthesis another one to two, so every
answer began with four to eight seconds of silence -- and silence is the one
thing a voice assistant cannot afford, because there is no way to tell it from
being ignored. ChatGPT's voice mode feels fast for this reason and not because
its model is quicker: it speaks the first clause while writing the rest.

So: the reply is consumed as it arrives, cut at sentence boundaries, and each
sentence is synthesised and played while the next is still being written. Time
to first word stops depending on the length of the answer.

THREE THINGS MAKE THIS HARDER THAN IT SOUNDS
--------------------------------------------
*Only some of the stream is speech.* The model emits <SAY> for speech, <ACTION>
tags, <REPORT> blocks and sometimes <think>. A naive reader would read XML
aloud. This tracks whether it is inside a speakable region as the characters
arrive, and never speaks anything it has not seen the opening tag for.

*A sentence is not a chunk.* Deltas arrive mid-word. Speaking per delta would
stutter; speaking per paragraph would reintroduce the wait. Sentence boundaries
are the unit a listener hears as natural, so text is buffered until one closes
-- with a length cap, because a model that never writes a full stop should not
mean silence.

*Order is not optional.* Synthesis of sentence two can finish before sentence
one. One worker plays strictly in order, or the answer arrives scrambled, which
is worse than slow.

If anything here fails, it fails back to silence and lets the caller speak the
whole reply the old way. Half a spoken answer is worse than a late one.
"""

import queue
import re
import threading
from concurrent.futures import ThreadPoolExecutor

#: Where a listener hears a natural break.
_SENTENCE_END = re.compile(r'(?<=[.!?])\s+|(?<=[.!?])$|\n\n')
#: Past this, speak anyway. A model that writes a long clause without
#: punctuation should not translate into a long silence.
MAX_PENDING_CHARS = 220
#: Below this a fragment is not worth a synthesis round trip of its own; it
#: waits and joins the next one.
MIN_SPEAKABLE_CHARS = 2

#: Regions whose text is meant to be heard. Everything else is machinery.
_SPEAKABLE_OPEN = re.compile(r'<\s*SAY\s*>', re.IGNORECASE)
_SPEAKABLE_CLOSE = re.compile(r'<\s*/\s*SAY\s*>', re.IGNORECASE)
#: Reasoning, if a model emits it inline rather than on its own channel.
_THINK_OPEN = re.compile(r'<\s*think\s*>', re.IGNORECASE)
_THINK_CLOSE = re.compile(r'<\s*/\s*think\s*>', re.IGNORECASE)
#: An action in the reply. Whatever is "said" after it can only be about a result
#: that does not exist yet, so it is never spoken. (Seen live: a camera lookup
#: announced "It's you -- match confirmed" in the same message that asked for
#: the lookup; the real result, a moment later, was a score of 0.12 and "unknown".
#: The reply with an action in it is protocol, and the answer is the next step's.)
_ACTION_OPEN = re.compile(r'<\s*ACTION\b', re.IGNORECASE)


class SpokenRegionReader:
    """Pulls speakable sentences out of a stream of partial text.

    Deliberately conservative: text is only ever emitted from inside a <SAY>
    that has been opened. A reply with no SAY yields nothing at all, and the
    caller speaks it the old way once it is complete.
    """

    def __init__(self):
        self._tail = ""          # characters not yet classified
        self._pending = ""       # speakable text not yet a whole sentence
        self._in_say = False
        self._in_think = False
        self._muted = False      # an ACTION has been seen: stop speaking

    def feed(self, delta):
        """Add text; return the sentences that are now complete."""
        self._tail += str(delta or "")
        sentences = []
        # A tag may be split across deltas, so anything that could still be the
        # start of a tag is held back rather than guessed at.
        while True:
            consumed = self._consume_once()
            if consumed is None:
                break
            if consumed:
                self._pending += consumed
        sentences.extend(self._cut_sentences())
        return sentences

    def _consume_once(self):
        """Classify as much of the tail as is unambiguous. None when stuck."""
        if not self._tail:
            return None

        if self._in_think:
            match = _THINK_CLOSE.search(self._tail)
            if not match:
                self._tail = self._tail[-12:] if len(self._tail) > 12 else self._tail
                return None
            self._tail = self._tail[match.end():]
            self._in_think = False
            return ""

        if self._in_say:
            close = _SPEAKABLE_CLOSE.search(self._tail)
            if close:
                text = self._tail[:close.start()]
                self._tail = self._tail[close.end():]
                self._in_say = False
                return "" if self._muted else text
            # No close tag yet. Give up everything that cannot be the start of
            # one, so speech is not held hostage to a tag that may be far off.
            safe = self._tail.rfind("<")
            if safe == -1:
                text, self._tail = self._tail, ""
                return "" if self._muted else text
            text, self._tail = self._tail[:safe], self._tail[safe:]
            if self._muted:
                return "" if text else None
            return text if text else None

        # Outside a speakable region: look for the next opening tag.
        say = _SPEAKABLE_OPEN.search(self._tail)
        think = _THINK_OPEN.search(self._tail)
        action = _ACTION_OPEN.search(self._tail)
        first = min((m for m in (say, think, action) if m), key=lambda m: m.start(),
                    default=None)
        if first is None:
            # Keep only enough to recognise a tag split across deltas.
            self._tail = self._tail[-12:] if len(self._tail) > 12 else self._tail
            return None
        self._tail = self._tail[first.end():]
        if first is say:
            self._in_say = True
        elif first is action:
            self._muted = True
        else:
            self._in_think = True
        return ""

    def _cut_sentences(self):
        out = []
        while True:
            parts = _SENTENCE_END.split(self._pending, maxsplit=1)
            if len(parts) < 2:
                break
            head, self._pending = parts[0], parts[1]
            head = head.strip()
            if len(head) >= MIN_SPEAKABLE_CHARS:
                out.append(head)
        if len(self._pending) >= MAX_PENDING_CHARS:
            # Long clause with no punctuation in sight. Break at the last space
            # so a word is never cut in half.
            cut = self._pending.rfind(" ", 0, MAX_PENDING_CHARS)
            if cut > 40:
                head, self._pending = self._pending[:cut], self._pending[cut:]
                out.append(head.strip())
        return out

    def finish(self):
        """Whatever speakable text is left over, as a final sentence."""
        if self._in_say and self._tail:
            self._pending += self._tail
        self._tail = ""
        leftover = self._pending.strip()
        self._pending = ""
        return [leftover] if len(leftover) >= MIN_SPEAKABLE_CHARS else []


class StreamingSpeaker:
    """Speaks sentences as they appear, strictly in order.

    `say_one` is injected rather than imported so this file knows nothing about
    TTS engines, playback or barge-in: the caller supplies a function that
    speaks one sentence and blocks until it is done, and that function already
    handles the voice, the fallbacks and the stop flag.
    """

    def __init__(self, say_one, should_stop=None, hold=None,
                 prepare_one=None, play_one=None, max_sentences=None):
        # Speech is short by default; the rest of a long reply is still shown
        # on screen. Unlimited when None.
        self._max_sentences = max_sentences
        self._say_one = say_one
        self._should_stop = should_stop or (lambda: False)
        # A context manager acquired for the WHOLE reply, not per sentence.
        # Without it another turn can seize the speaker between two sentences
        # and cut this reply in half -- which is what "it was speaking and
        # started speaking other stuff" turned out to be. Injected rather than
        # imported so this file still knows nothing about the audio stack.
        self._hold = hold
        self._prepare_one = prepare_one
        self._play_one = play_one
        self._preparer = (ThreadPoolExecutor(
            max_workers=2, thread_name_prefix="vanta-tts-prefetch")
            if prepare_one is not None and play_one is not None else None)
        self._reader = SpokenRegionReader()
        self._queue = queue.Queue()
        self._spoken = []          # accepted for preparation
        self._played = []          # actually completed playback
        self._lock = threading.Lock()
        # Preparation is intentionally parallel; audible playback never is.
        # The drain worker is currently single-threaded, but retaining this
        # explicit invariant prevents a later worker-pool refactor from making
        # two completed sentence files talk over each other.
        self._playback_lock = threading.Lock()
        self._worker = threading.Thread(target=self._run, daemon=True,
                                        name="vanta-speech-stream")
        self._started = False
        self._failed = False

    def _run(self):
        if self._hold is None:
            self._drain()
            return
        # Held for the whole reply. A barge-in still interrupts, because the
        # stop check below runs inside this and abandons the queue.
        try:
            with self._hold():
                self._drain()
        except Exception as exc:
            print(f"[Speech] Could not hold the speaker ({exc}); "
                  f"speaking without it.")
            self._drain()

    def _drain(self):
        while True:
            item = self._queue.get()
            if item is None:
                return
            sentence, prepared = item
            if self._should_stop():
                # Barge-in: drop the rest rather than talking over him.
                continue
            try:
                if prepared is not None:
                    with self._playback_lock:
                        self._play_one(sentence, prepared.result())
                else:
                    with self._playback_lock:
                        self._say_one(sentence)
                if not self._should_stop():
                    with self._lock:
                        self._played.append(sentence)
            except Exception as exc:
                print(f"[Speech] Streaming failed mid-sentence ({exc}); "
                      f"the rest will be spoken normally.")
                with self._lock:
                    self._failed = True
                return

    def feed(self, delta):
        for sentence in self._reader.feed(delta):
            self._enqueue(sentence)

    def _enqueue(self, sentence):
        if not sentence:
            return
        with self._lock:
            if self._failed:
                return
            if (self._max_sentences is not None
                    and len(self._spoken) >= self._max_sentences):
                return
            self._spoken.append(sentence)
            if not self._started:
                self._started = True
                self._worker.start()
        prepared = (self._preparer.submit(self._prepare_one, sentence)
                    if self._preparer is not None else None)
        self._queue.put((sentence, prepared))

    def finish(self, timeout=30.0):
        """Flush the tail and wait for playback to drain."""
        for sentence in self._reader.finish():
            self._enqueue(sentence)
        with self._lock:
            started = self._started
        if not started:
            return
        self._queue.put(None)
        self._worker.join(timeout=timeout)
        if self._preparer is not None:
            self._preparer.shutdown(wait=False, cancel_futures=True)

    def spoke_anything(self):
        with self._lock:
            return bool(self._played)

    def spoken_text(self):
        with self._lock:
            return " ".join(self._played).strip()
