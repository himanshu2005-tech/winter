# -*- coding: utf-8 -*-
"""
Tests for speaking before the sentence has finished being written.

Nothing was said until the whole reply had been generated AND synthesised.
Generation is two to six seconds and synthesis another one to two, so every
answer opened with four to eight seconds of silence -- and silence is the one
thing a voice assistant cannot afford, because there is no way to tell it from
being ignored. Measured on a four-second reply, both paths warm: 4.8s to first
audio before, 1.2s after, and now independent of how long the answer is.

Most of this file guards the three things that make it harder than it sounds:
only part of the stream is speech, a delta is not a sentence, and sentence two
can finish synthesising before sentence one.
"""

import os
import sys
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import speech_stream


def feed_in_chunks(reader, text, size=7):
    """Deltas arrive mid-word, so that is how the tests deliver them."""
    out = []
    for index in range(0, len(text), size):
        out.extend(reader.feed(text[index:index + size]))
    out.extend(reader.finish())
    return out


class SpeakableRegionTests(unittest.TestCase):
    """Only <SAY> is speech. Everything else is machinery."""

    def test_sentences_come_out_one_at_a_time(self):
        said = feed_in_chunks(speech_stream.SpokenRegionReader(),
                              "<SAY>Good morning, sir. It reports at nine.</SAY>")
        self.assertEqual(said, ["Good morning, sir.", "It reports at nine."])

    def test_an_action_is_never_read_aloud(self):
        said = feed_in_chunks(
            speech_stream.SpokenRegionReader(),
            '<SAY>Setting that now.</SAY>\n'
            '<ACTION type="SET_ALARM" time="07:15" label="x" />')
        self.assertEqual(said, ["Setting that now."])
        for spoken in said:
            self.assertNotIn("ACTION", spoken)

    def test_speech_after_an_action_is_never_spoken(self):
        """It can only be about a result that does not exist yet.

        Seen live: a camera lookup announced "match confirmed" in the same
        message that requested the lookup; the real answer was "unknown".
        """
        text = ('<ACTION type="WHO_IS_THIS" speech="Checking the camera" />\n'
                "<SAY>It's you, sir. Match confirmed.</SAY>")
        for size in (1, 3, 7, 50):
            said = feed_in_chunks(speech_stream.SpokenRegionReader(), text, size)
            self.assertEqual(said, [], f"size={size}")

    def test_an_opening_line_before_the_action_is_still_spoken(self):
        text = ('<SAY>Checking now.</SAY><ACTION type="WHO_IS_THIS" />'
                "<SAY>It's you, sir.</SAY>")
        said = feed_in_chunks(speech_stream.SpokenRegionReader(), text, 5)
        self.assertEqual(said, ["Checking now."])

    def test_a_reply_with_no_action_is_untouched(self):
        said = feed_in_chunks(speech_stream.SpokenRegionReader(),
                              "<SAY>Fine. And you?</SAY><DONE />", 4)
        self.assertEqual(said, ["Fine.", "And you?"])

    def test_the_word_action_inside_speech_does_not_mute_it(self):
        said = feed_in_chunks(speech_stream.SpokenRegionReader(),
                              "<SAY>Taking action now, sir. Back soon.</SAY>", 6)
        self.assertEqual(said, ["Taking action now, sir.", "Back soon."])

    def test_reasoning_is_never_read_aloud(self):
        # Speaking a model's private deliberation would be both wrong and
        # alarming.
        said = feed_in_chunks(
            speech_stream.SpokenRegionReader(),
            "<think>He wants the time. Keep it short.</think>"
            "<SAY>It is half past four.</SAY>")
        self.assertEqual(said, ["It is half past four."])

    def test_a_report_block_is_not_speech(self):
        said = feed_in_chunks(
            speech_stream.SpokenRegionReader(),
            "<SAY>Done, sir.</SAY><REPORT>**Details**\n- one\n- two</REPORT>")
        self.assertEqual(said, ["Done, sir."])

    def test_a_reply_with_no_say_speaks_nothing(self):
        """The caller then delivers it the old way.

        Guessing at what was speakable would eventually read XML aloud.
        """
        said = feed_in_chunks(speech_stream.SpokenRegionReader(),
                              '<ACTION type="RUN_COMMAND" cmd="Get-Process" />')
        self.assertEqual(said, [])

    def test_a_tag_split_across_deltas_still_works(self):
        # Deltas land mid-tag as often as mid-word.
        for size in (1, 2, 3, 5, 11):
            said = feed_in_chunks(speech_stream.SpokenRegionReader(),
                                  "<SAY>First one. Second one.</SAY>", size)
            self.assertEqual(said, ["First one.", "Second one."], f"size={size}")

    def test_a_long_clause_is_not_held_forever(self):
        """A model that never writes a full stop must not mean silence."""
        body = ("and then it keeps going without punctuation of any kind for a "
                "good deal longer than the cap allows so that something has to "
                "be spoken before the end arrives, because waiting for a full "
                "stop that never comes is the failure this guards against")
        said = feed_in_chunks(speech_stream.SpokenRegionReader(),
                              "<SAY>" + body + "</SAY>")
        self.assertGreaterEqual(len(said), 2, "the cap never fired")
        for spoken in said:
            self.assertLessEqual(len(spoken),
                                 speech_stream.MAX_PENDING_CHARS + 60)

    def test_words_are_never_cut_in_half(self):
        body = "x" * 10 + " " + "supercalifragilistic " * 20
        said = feed_in_chunks(speech_stream.SpokenRegionReader(),
                              "<SAY>" + body + "</SAY>")
        rejoined = " ".join(said).replace("  ", " ").strip()
        self.assertEqual(rejoined.split(), body.split())


class OrderingTests(unittest.TestCase):
    """Sentence two can synthesise faster than sentence one."""

    def test_playback_is_strictly_in_order(self):
        played = []
        lock = threading.Lock()

        def say_one(sentence):
            # The first sentence is made deliberately slow, so anything that
            # played concurrently would land out of order.
            time.sleep(0.25 if "First" in sentence else 0.01)
            with lock:
                played.append(sentence)

        speaker = speech_stream.StreamingSpeaker(say_one)
        for part in ("<SAY>First one. ", "Second one. ", "Third one.</SAY>"):
            speaker.feed(part)
        speaker.finish()
        self.assertEqual(played, ["First one.", "Second one.", "Third one."])

    def test_next_sentence_is_prepared_while_current_one_plays(self):
        events = []
        lock = threading.Lock()

        def prepare(sentence):
            with lock:
                events.append(("prepared", sentence, time.monotonic()))
            return sentence

        def play(sentence, prepared):
            with lock:
                events.append(("play-start", sentence, time.monotonic()))
            time.sleep(0.15)
            with lock:
                events.append(("play-end", sentence, time.monotonic()))

        speaker = speech_stream.StreamingSpeaker(
            lambda _sentence: None, prepare_one=prepare, play_one=play)
        speaker.feed("<SAY>First sentence. Second sentence.</SAY>")
        speaker.finish()
        second_ready = next(t for kind, sentence, t in events
                            if kind == "prepared" and sentence.startswith("Second"))
        first_end = next(t for kind, sentence, t in events
                         if kind == "play-end" and sentence.startswith("First"))
        self.assertLess(second_ready, first_end)
        played = [sentence for kind, sentence, _t in events
                  if kind == "play-start"]
        self.assertEqual(played, ["First sentence.", "Second sentence."])

    def test_prefetched_playback_never_overlaps(self):
        active = 0
        maximum = 0
        lock = threading.Lock()

        def prepare(sentence):
            return sentence

        def play(_sentence, _prepared):
            nonlocal active, maximum
            with lock:
                active += 1
                maximum = max(maximum, active)
            time.sleep(0.04)
            with lock:
                active -= 1

        speaker = speech_stream.StreamingSpeaker(
            lambda _sentence: None, prepare_one=prepare, play_one=play)
        speaker.feed("<SAY>One. Two. Three. Four.</SAY>")
        speaker.finish()
        self.assertEqual(maximum, 1)
        self.assertEqual(active, 0)

    def test_nothing_starts_until_there_is_something_to_say(self):
        played = []
        speaker = speech_stream.StreamingSpeaker(played.append)
        speaker.feed('<ACTION type="X" />')
        speaker.finish()
        self.assertEqual(played, [])
        self.assertFalse(speaker.spoke_anything())

    def test_what_was_spoken_is_reported_back(self):
        # The caller needs this to avoid delivering the same reply twice.
        speaker = speech_stream.StreamingSpeaker(lambda s: None)
        speaker.feed("<SAY>One. Two.</SAY>")
        speaker.finish()
        self.assertTrue(speaker.spoke_anything())
        self.assertIn("One.", speaker.spoken_text())
        self.assertIn("Two.", speaker.spoken_text())


class InterruptionTests(unittest.TestCase):

    def test_barge_in_drops_the_rest(self):
        """Talking over him is worse than stopping mid-answer."""
        played = []
        stop = threading.Event()

        def say_one(sentence):
            played.append(sentence)
            stop.set()          # he interrupts after the first sentence

        speaker = speech_stream.StreamingSpeaker(say_one, should_stop=stop.is_set)
        speaker.feed("<SAY>One. Two. Three. Four.</SAY>")
        speaker.finish()
        self.assertEqual(played, ["One."])

    def test_a_synthesis_failure_does_not_half_speak_the_answer(self):
        # Half a spoken answer is worse than a late one, so a failure gives up
        # and lets the caller deliver the whole thing normally.
        def say_one(sentence):
            raise RuntimeError("no audio device")

        speaker = speech_stream.StreamingSpeaker(say_one)
        speaker.feed("<SAY>One. Two.</SAY>")
        speaker.finish()
        self.assertFalse(speaker.spoke_anything(),
                         "a failed stream must not claim it spoke")

    def test_partial_success_reports_only_completed_playback(self):
        calls = []

        def play(sentence, prepared):
            calls.append(sentence)
            if sentence.startswith("Two"):
                raise RuntimeError("device disappeared")

        speaker = speech_stream.StreamingSpeaker(
            lambda _sentence: None,
            prepare_one=lambda sentence: sentence,
            play_one=play)
        speaker.feed("<SAY>One. Two. Three.</SAY>")
        speaker.finish()
        self.assertTrue(speaker.spoke_anything())
        self.assertEqual(speaker.spoken_text(), "One.")


class CompletionDeduplicationTests(unittest.TestCase):

    def test_only_unplayed_suffix_is_returned(self):
        import agent
        agent._already_spoken.clear()
        try:
            agent._already_spoken.add("First sentence. Second sentence.")
            self.assertEqual(
                agent._unspoken_remainder(
                    "First sentence. Second sentence. Third sentence."),
                "Third sentence.")
        finally:
            agent._already_spoken.clear()

    def test_fully_played_response_has_no_remainder(self):
        import agent
        agent._already_spoken.clear()
        try:
            agent._already_spoken.add("One. Two.")
            self.assertEqual(agent._unspoken_remainder("One. Two."), "")
        finally:
            agent._already_spoken.clear()


class WiringTests(unittest.TestCase):

    def test_the_router_forwards_content_deltas(self):
        import inspect
        import llm_router
        self.assertIn("on_delta", inspect.signature(llm_router.call_llm).parameters)
        source = inspect.getsource(llm_router._stream)
        self.assertIn("on_delta(content)", source)
        # Content only: reasoning has its own branch and must not be spoken.
        reasoning_at = source.index("if reasoning:")
        delta_at = source.index("on_delta(content)")
        self.assertLess(reasoning_at, delta_at)

    def test_the_executor_streams_its_reply(self):
        import inspect
        import agent
        source = inspect.getsource(agent._inner_run_agent_loop)
        self.assertIn("_begin_streaming_speech(", source)
        self.assertIn("on_delta=", source)

    def test_a_streamed_reply_is_not_spoken_again(self):
        import agent
        agent._already_spoken.clear()
        try:
            agent._already_spoken.add("Good morning, sir. It reports at nine.")
            self.assertTrue(agent._was_already_spoken(
                "Good morning, sir. It reports at nine."))
            # Reassembled from sentences, so whitespace differs while the words
            # do not.
            self.assertTrue(agent._was_already_spoken(
                "Good morning, sir.    It reports at nine."))
            self.assertFalse(agent._was_already_spoken("Something else."))
        finally:
            agent._already_spoken.clear()

    def test_a_streamed_url_and_clean_final_are_not_spoken_twice(self):
        import agent
        agent._already_spoken.clear()
        try:
            agent._already_spoken.add(
                "Your repository is https://github.com/example/project.")
            self.assertTrue(agent._was_already_spoken(
                "Your repository is the link shown in Details."))
            self.assertEqual(agent._unspoken_remainder(
                "Your repository is the link shown in Details."), "")
        finally:
            agent._already_spoken.clear()

    def test_the_record_is_cleared_each_turn(self):
        # Otherwise last turn's sentences suppress an identical reply this turn.
        import inspect
        import agent
        self.assertIn("_already_spoken.clear()",
                      inspect.getsource(agent.run_agent_loop))

    def test_a_remote_turn_is_not_streamed(self):
        """Telegram gets one audio file.

        Six fragments arriving on a phone is worse than waiting for one.
        """
        import inspect
        import agent
        source = inspect.getsource(agent._begin_streaming_speech)
        self.assertIn("turn_context.is_remote()", source)
        self.assertIn("return None", source)

    def test_it_can_be_switched_off(self):
        import agent
        self.assertIsInstance(agent.STREAM_SPEECH, bool)
        import inspect
        self.assertIn("STREAM_SPEECH",
                      inspect.getsource(agent._begin_streaming_speech))

    def test_streaming_does_not_spam_the_dashboard(self):
        # One reply must not arrive there as six fragments.
        import inspect
        import agent
        self.assertIn("show", inspect.signature(agent.speak).parameters)
        self.assertIn("show=False",
                      inspect.getsource(agent._begin_streaming_speech))


class VoiceRegisterTests(unittest.TestCase):
    """What the voice is built for, not what an adjective suggested."""

    def test_the_voice_is_built_for_conversation(self):
        """This test previously asserted the opposite, and was wrong.

        It ruled Ava out for being tagged Expressive and Caring -- "the
        register of a companion" -- and preferred Aria for being the only one
        tagged Confident. Microsoft publishes a second field, ContentCategories,
        which says Aria is for News and Novel: a newsreader. That is precisely
        why it arrived with no emotion in it. Expressive was the point all
        along; the breathiness that caused the whole detour was prosody.
        """
        import voice
        for broadcast in ("Aria", "Michelle"):      # tagged News, Novel
            self.assertNotIn(broadcast, voice.DEFAULT_VOICE)
        for toy in ("Maisie", "Ana"):               # tagged Cute / Cartoon
            self.assertNotIn(toy, voice.DEFAULT_VOICE)
        conversational = ("Ava", "Emma", "Jenny")   # Conversation, Copilot
        self.assertTrue(
            any(name in voice.DEFAULT_VOICE for name in conversational),
            f"{voice.DEFAULT_VOICE} is not a conversation voice")

    def test_the_choice_can_be_made_by_ear(self):
        # Four changes were argued from adjectives nobody involved could hear.
        import audition_voice
        self.assertGreaterEqual(len(audition_voice.SHORTLIST), 5)
        for row in audition_voice.SHORTLIST:
            self.assertEqual(len(row), 3, "each row carries its metadata")

    def test_emotional_styles_are_honestly_out_of_reach(self):
        """edge-tts takes rate, volume and pitch and nothing else.

        Azure's empathetic/gentle/calm styles go through mstts:express-as in
        SSML, which edge-tts does not expose. Claiming emotional control would
        be claiming something the library cannot do.
        """
        import inspect
        import edge_tts
        params = inspect.signature(edge_tts.Communicate.__init__).parameters
        self.assertNotIn("style", params)
        self.assertIn("pitch", params)

    def test_the_prosody_stays_neutral(self):
        # Slowing the rate and dropping the pitch together is what produced
        # breathiness; neither is allowed to drift back.
        import voice
        self.assertFalse(voice.pitch_name().startswith("-"))
        self.assertGreaterEqual(voice.DEFAULT_RATE_TRIM, -6)


class SpeechFloorTests(unittest.TestCase):
    """One reply owns the speaker until it finishes.

    _tts_lock only guards the moment playback starts, and every speak() opens
    with pygame.mixer.music.stop() -- so new speech silently cut off whatever
    was talking. With one speak() per turn that was rare. Streaming made it a
    per-sentence opportunity: between two sentences of one reply another turn
    could seize the mixer, and VANTA was heard starting a greeting over the
    middle of its own answer.
    """

    def _timeline(self, hold):
        import contextlib
        import threading
        import time

        order, lock, floor = [], threading.Lock(), threading.RLock()

        @contextlib.contextmanager
        def holder():
            got = floor.acquire(timeout=10)
            try:
                yield
            finally:
                if got:
                    floor.release()

        def make(label):
            def say_one(sentence):
                with lock:
                    order.append(label)
                time.sleep(0.08)
            return say_one

        def run(label):
            speaker = speech_stream.StreamingSpeaker(
                make(label), hold=holder if hold else None)
            speaker.feed(f"<SAY>{label} one. {label} two. {label} three.</SAY>")
            speaker.finish()

        first = threading.Thread(target=run, args=("A",))
        second = threading.Thread(target=run, args=("B",))
        first.start()
        time.sleep(0.03)
        second.start()
        first.join()
        second.join()
        return order

    def test_without_a_floor_two_replies_interleave(self):
        """The failure, demonstrated, so the fix is not taken on trust."""
        order = self._timeline(hold=False)
        switches = sum(1 for i in range(1, len(order)) if order[i] != order[i - 1])
        self.assertGreater(switches, 1, "expected interleaving without a floor")

    def test_with_the_floor_each_reply_finishes_first(self):
        order = self._timeline(hold=True)
        switches = sum(1 for i in range(1, len(order)) if order[i] != order[i - 1])
        self.assertEqual(switches, 1, f"expected one clean handover, got {order}")

    def test_the_floor_is_reentrant(self):
        """The worker holds it, then calls speak() on that same thread.

        A plain Lock would deadlock on the first sentence.
        """
        import agent
        import threading
        self.assertIsInstance(agent._speech_floor,
                              type(threading.RLock()))

    def test_speak_waits_for_the_floor(self):
        import inspect
        import agent
        source = inspect.getsource(agent.speak)
        self.assertIn("_speech_floor.acquire", source)
        self.assertIn("SPEECH_FLOOR_WAIT", source)

    def test_it_speaks_rather_than_going_mute_if_the_floor_sticks(self):
        # A reply that never releases must not silence VANTA permanently.
        import agent
        self.assertGreater(agent.SPEECH_FLOOR_WAIT, 5)
        import inspect
        # A phrase that sits inside ONE string literal. The message is split
        # across two adjacent literals, so "rather than staying silent" is
        # never contiguous in the source however the whitespace is normalised
        # -- the quote marks sit in the middle of it.
        self.assertIn("staying silent", inspect.getsource(agent.speak))

    def test_the_streamed_reply_holds_it_for_its_whole_length(self):
        import inspect
        import agent
        self.assertIn("hold=hold_the_speaker",
                      inspect.getsource(agent._begin_streaming_speech))

    def test_a_barge_in_still_interrupts_a_held_reply(self):
        """Holding the speaker must not make VANTA uninterruptible.

        The stop check runs inside the held section, so an interruption
        abandons the queue instead of waiting for it.
        """
        import contextlib
        import threading
        played, stop = [], threading.Event()
        floor = threading.RLock()

        @contextlib.contextmanager
        def holder():
            floor.acquire()
            try:
                yield
            finally:
                floor.release()

        def say_one(sentence):
            played.append(sentence)
            stop.set()

        speaker = speech_stream.StreamingSpeaker(
            say_one, should_stop=stop.is_set, hold=holder)
        speaker.feed("<SAY>One. Two. Three.</SAY>")
        speaker.finish()
        self.assertEqual(played, ["One."])


class UniformVoiceTests(unittest.TestCase):
    """One reply, one voice.

    "The voice is not uniform, it's cutting in between" was two faults. The
    first was the mixer: any new speech called pygame.mixer.music.stop(), so a
    second turn cut this one off -- fixed by the speech floor above. The second
    was engine flapping: a single 8-second timeout set a 60-second memo and the
    rest of the reply came out in Kokoro, a different voice entirely. Streaming
    made that far likelier, because one reply is now several synthesis calls
    and each gets its own chance to trip the budget.
    """

    def test_a_mid_reply_failure_does_not_demote_the_engine(self):
        import voice
        voice._offline_until = 0.0
        voice.begin_utterance()
        try:
            voice._note_offline("TimeoutError after 8s")
            self.assertTrue(voice.online_voice_enabled(),
                            "one slow sentence must not change the voice for "
                            "the rest of the reply")
        finally:
            voice.end_utterance()
            voice._offline_until = 0.0

    def test_a_failure_between_replies_still_demotes_it(self):
        # The memo exists so a genuine outage does not re-pay the timeout on
        # every sentence. Suppressing it always would be the opposite bug.
        import voice
        voice._offline_until = 0.0
        voice.end_utterance()
        try:
            voice._note_offline("TimeoutError")
            self.assertFalse(voice.online_voice_enabled())
        finally:
            voice._offline_until = 0.0

    def test_the_utterance_scope_is_per_thread(self):
        """Two turns speaking at once must not share the flag.

        A proactive reply marking itself mid-utterance would otherwise suppress
        a real outage signal for a user turn on another thread.
        """
        import threading
        import voice
        seen = {}

        def other():
            seen["value"] = voice.in_utterance()

        voice.begin_utterance()
        try:
            thread = threading.Thread(target=other)
            thread.start()
            thread.join(timeout=5)
            self.assertFalse(seen["value"])
            self.assertTrue(voice.in_utterance())
        finally:
            voice.end_utterance()

    def test_the_timeout_has_room_for_a_slow_sentence(self):
        # 8s was measured too tight once synthesis became per-sentence.
        import voice
        self.assertGreaterEqual(voice.ONLINE_TIMEOUT, 12)

    def test_a_blip_does_not_cost_a_whole_minute(self):
        import voice
        self.assertLessEqual(voice.OFFLINE_MEMO_SECONDS, 30)

    def test_the_streamed_reply_declares_its_scope(self):
        import inspect
        import agent
        source = inspect.getsource(agent._begin_streaming_speech)
        self.assertIn("begin_utterance()", source)
        self.assertIn("end_utterance()", source)


class ActionTagToleranceTests(unittest.TestCase):
    """A tag the model meant must not vanish without a word.

    The scan matched <ACTION only, case-sensitively. Observed live: three
    <Action type="WATCH_STATUS" /> tags dropped with no receipt, a whole step
    wasted, and the model re-sent them in capitals on the next step having been
    told nothing about why the first attempt produced nothing.
    """

    def _parses(self, tag):
        import queue
        import actions
        actions.execute_actions._last_output = ""
        actions.execute_actions(tag, {}, queue.Queue(), ".", origin="user")
        out = str(getattr(actions.execute_actions, "_last_output", "") or "")
        return "No executable action" not in out

    def test_every_casing_is_read(self):
        """VOLUME with no step, because this tests PARSING.

        LIST_ALARMS was the first choice and it needs Neo4j, so the test failed
        when the database was merely off -- measuring the environment instead
        of the thing under test. VOLUME reaches its handler and is refused on
        policy, which still proves the tag was parsed.
        """
        for tag in ('<ACTION type="VOLUME" direction="up" steps="0" />',
                    '<Action type="VOLUME" direction="up" steps="0" />',
                    '<action type="VOLUME" direction="up" steps="0" />',
                    '< ACTION type="VOLUME" direction="up" steps="0" />'):
            self.assertTrue(self._parses(tag), tag)

    def test_the_scan_is_case_insensitive(self):
        import action_protocol
        import action_attrs
        for tag in ('ACTION', 'Action', 'action', 'aCtIoN'):
            calls = action_protocol.extract(f'<{tag} type="VOLUME" direction="up" />')
            self.assertEqual(len(calls), 1)
            self.assertEqual(action_attrs.parse(calls[0].attributes)['type'], 'VOLUME')


if __name__ == "__main__":
    unittest.main()
