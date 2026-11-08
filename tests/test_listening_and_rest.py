# -*- coding: utf-8 -*-
"""Hearing him, answering him, and leaving him alone.

Four complaints, four different causes.

*The transcriber lost his proper nouns.* Measured against both Groq models on
real sentences: whisper-large-v3 and its turbo variant are within 0.03s of
each other per clip and make the same mistakes -- "NXT Wave" for NxtWave,
"Havelock" for Haveloc. The model was never the problem. The hotword list
that would fix it was collected and then discarded, because Whisper
conditions on a prompt and echoes it back as a transcript when the audio is
near-silence.

*The voice fell back to Kokoro every single line.* edge-tts 7.2.8 was getting
403 Invalid response status on every handshake -- Microsoft changed its token
scheme, which the library tracks. Upgrading fixed it outright; warm calls
measure 1.5s where Kokoro was loading an 82M model.

*Every sentence needed the wake word.* WAKE_FOLLOW_UP was declared and never
read anywhere -- dead configuration -- so the only way to avoid saying
"Vanta" twenty times was to hold F9 down instead.

*The screen stayed on all night.* He fell asleep working and it was still
showing the same windows in the morning. Nothing watched for how long the
picture had been the same.
"""

import os
import sys
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import screen_rest
import vocab

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KNOWN = ["NxtWave", "Haveloc", "Amrita", "Neo4j", "Himanshu", "Mavo", "Madhu"]


class TheVocabularyIsPutBack(unittest.TestCase):
    """Repairing after the model, where there is nothing to hallucinate from."""

    def repair(self, text):
        return vocab.repair(text, KNOWN)

    def test_a_name_split_in_two_is_rejoined(self):
        """Repairing each half separately could never put it back together."""
        self.assertEqual(
            self.repair("Remind me about the NXT Wave assignment."),
            "Remind me about the NxtWave assignment.")

    def test_a_misheard_consonant_is_corrected(self):
        self.assertEqual(self.repair("Check HaveLock for drives."),
                         "Check Haveloc for drives.")
        self.assertEqual(self.repair("Check Havelock for drives."),
                         "Check Haveloc for drives.")

    def test_casing_is_corrected(self):
        self.assertEqual(self.repair("open the mavo project"),
                         "open the Mavo project")

    def test_an_ordinary_sentence_is_left_alone(self):
        """A sentence must not be dragged toward the vocabulary."""
        for sentence in ("I have a lock on the door.",
                         "the wave was big",
                         "put it in the bowl on the table"):
            with self.subTest(sentence=sentence):
                self.assertEqual(self.repair(sentence), sentence)

    def test_short_words_are_never_fuzzy_matched(self):
        """Two edits on a four-letter word is a different word."""
        self.assertEqual(vocab.repair("the mavo and the move", ["Mavo"]),
                         "the Mavo and the move")

    def test_punctuation_and_spacing_survive(self):
        self.assertEqual(self.repair("Is it NXT Wave, or Haveloc?"),
                         "Is it NxtWave, or Haveloc?")

    def test_nothing_in_nothing_out(self):
        self.assertEqual(vocab.repair("", KNOWN), "")
        self.assertEqual(vocab.repair("anything", []), "anything")
        self.assertEqual(vocab.repair(None, KNOWN), "")

    def test_the_edit_distance_helper_is_right(self):
        self.assertTrue(vocab._within_one_edit("haveloc", "havelock"))
        self.assertTrue(vocab._within_one_edit("mavo", "mavo"))
        self.assertFalse(vocab._within_one_edit("amrutha", "amrita"))
        self.assertFalse(vocab._within_one_edit("cat", "dog"))


class TheTranscriberUsesIt(unittest.TestCase):
    def test_the_repair_runs_on_both_paths(self):
        import inspect
        import stt
        source = inspect.getsource(stt.transcribe)
        self.assertEqual(source.count("_repaired(text)"), 2,
                         "online and local must both be repaired")

    def test_the_full_model_is_the_default(self):
        """Not the turbo distillation: measured, they cost the same."""
        import stt
        self.assertEqual(stt._ONLINE_MODEL, "whisper-large-v3")

    def test_a_broken_vocabulary_does_not_lose_the_transcript(self):
        import stt
        with patch.object(stt, "vocabulary", side_effect=RuntimeError("no")):
            self.assertEqual(stt._repaired("hello there"), "hello there")

    def test_the_prompt_is_still_not_sent(self):
        """Whisper echoes a prompt back as the transcript on near-silence,
        which is why the hotwords are applied afterwards instead."""
        import inspect
        import stt
        self.assertNotIn("prompt=",
                         inspect.getsource(stt._transcribe_online))


class TheOnlineVoiceWorksAgain(unittest.TestCase):
    def test_the_library_is_new_enough(self):
        """7.2.8 got 403 on every handshake; Microsoft had changed its token
        scheme and the library tracks it."""
        import importlib.metadata as metadata
        version = metadata.version("edge-tts")
        parts = tuple(int(p) for p in version.split(".")[:3] if p.isdigit())
        self.assertGreaterEqual(parts, (7, 3, 0), f"edge-tts {version}")

    def test_kokoro_is_the_fallback_not_the_default(self):
        import inspect
        import voice
        source = inspect.getsource(voice)
        self.assertIn("_edge_tts", source)
        self.assertIn("online_voice_enabled", source)


class AFollowUpNeedsNoName(unittest.TestCase):
    """WAKE_FOLLOW_UP was declared and read by nothing."""

    @classmethod
    def setUpClass(cls):
        import agent
        cls.agent = agent

    def setUp(self):
        self.agent._close_follow_up()
        self.addCleanup(self.agent._close_follow_up)

    def test_the_window_is_actually_used_now(self):
        with open(os.path.join(ROOT, "agent.py"), encoding="utf-8") as handle:
            source = handle.read()
        self.assertGreater(source.count("_in_follow_up()"), 0,
                           "the window is still dead configuration")

    def test_it_is_open_by_default(self):
        self.assertGreater(self.agent.WAKE_FOLLOW_UP, 0)

    def test_nothing_is_open_before_a_turn(self):
        self.assertFalse(self.agent._in_follow_up())

    def test_a_turn_opens_it(self):
        self.agent._open_follow_up()
        self.assertTrue(self.agent._in_follow_up())

    def test_it_closes(self):
        self.agent._open_follow_up()
        self.agent._close_follow_up()
        self.assertFalse(self.agent._in_follow_up())

    def test_only_his_turns_open_it(self):
        """VANTA speaking on its own is not an invitation to anyone."""
        with open(os.path.join(ROOT, "agent.py"), encoding="utf-8") as handle:
            source = handle.read()
        start = source.index("_open_follow_up()\n        try:")
        self.assertIn('origin == "user"', source[start - 300:start])

    def test_a_real_sentence_is_treated_as_addressed(self):
        for said in ("remind me at six", "open the mavo project",
                     "what about tomorrow", "no, the other one"):
            with self.subTest(said=said):
                self.assertTrue(self.agent._is_addressed_follow_up(said))

    def test_short_replies_are_part_of_the_open_conversation(self):
        for said in ("yeah", "ok", "thanks", "hmm", "no", "tomorrow"):
            with self.subTest(said=said):
                self.assertTrue(self.agent._is_addressed_follow_up(said))

    def test_conversation_stays_open_while_a_follow_up_is_processed(self):
        import inspect
        source = inspect.getsource(self.agent.wakeword_worker)
        follow_up = source.split(
            "and _is_addressed_follow_up(heard))", 1)[1].split(
                "continue", 1)[0]
        self.assertIn("_open_follow_up()", follow_up)
        self.assertIn("_queue_interruption", follow_up)
        self.assertNotIn("_close_follow_up()", follow_up)

    def test_a_sentence_that_begins_before_expiry_is_not_lost_at_expiry(self):
        import inspect
        source = inspect.getsource(self.agent.wakeword_worker)
        self.assertIn("began_as_followup or _in_follow_up()", source)

    def test_a_transcribed_voice_turn_opens_the_window_before_the_model(self):
        import inspect
        source = inspect.getsource(self.agent.process_audio)
        self.assertLess(source.index("_open_follow_up()"),
                        source.index("Thread(target=run_agent_loop"))

    def test_silence_is_not_a_follow_up(self):
        for said in ("", "   ", "...", "!?"):
            with self.subTest(said=said):
                self.assertFalse(self.agent._is_addressed_follow_up(said))

    def test_open_and_close_publish_the_notch_window_state(self):
        while not self.agent.ui_queue.empty():
            self.agent.ui_queue.get_nowait()
        self.agent._open_follow_up()
        opened = self.agent.ui_queue.get_nowait()
        self.assertEqual(opened[0], "conversation_window")
        self.assertGreater(opened[1], 0)
        self.agent._close_follow_up()
        self.assertEqual(
            self.agent.ui_queue.get_nowait(), ("conversation_window", 0.0))


class AbortOneActionWithoutLosingTheTask(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import agent
        cls.agent = agent

    def test_only_whole_utterance_safety_phrases_match(self):
        for said in ("abort that", "don't do that", "cancel that action",
                     "skip that action"):
            with self.subTest(said=said):
                self.assertTrue(self.agent._is_action_abort(said))
        for said in ("how do I abort a git merge", "don't do that later",
                     "abortions"):
            with self.subTest(said=said):
                self.assertFalse(self.agent._is_action_abort(said))

    def test_abort_prompt_forbids_retry_but_preserves_unaffected_work(self):
        plan = type("Plan", (), {"goal": "email and update calendar"})()
        prompt = self.agent._barge_in_prompt("skip that action", plan)
        self.assertIn("Do not retry the aborted action", prompt)
        self.assertIn("continue only unaffected", prompt)

    def test_wake_worker_checks_abort_before_open_commands_and_followups(self):
        import inspect
        source = inspect.getsource(self.agent.wakeword_worker)
        abort = source.index("_is_action_abort(heard)")
        fast = source.index("if heard and WAKE_OPEN_COMMANDS")
        followup = source.index("began_as_followup")
        self.assertLess(abort, fast)
        self.assertLess(abort, followup)

    def test_local_followup_listening_state_keeps_receiving_audio(self):
        old_state = self.agent.current_state
        old_recording = self.agent.is_recording
        try:
            self.agent.current_state = self.agent.STATE_LISTENING
            self.agent.is_recording = False
            self.assertTrue(self.agent._wake_may_listen())
            self.agent.is_recording = True
            # The outer worker guard owns push-to-talk and will skip it.
            self.assertFalse(self.agent._wake_may_listen())
        finally:
            self.agent.current_state = old_state
            self.agent.is_recording = old_recording

    def test_abort_sets_the_dispatch_brake_and_queues_one_redirect(self):
        while not self.agent.interruption_queue.empty():
            self.agent._take_interruption()
        self.agent._barge_in_event.clear()
        acquired = self.agent._agent_lock.acquire(blocking=False)
        self.assertTrue(acquired, "another test left the agent lock held")
        try:
            self.assertTrue(self.agent._abort_pending_action("skip that action"))
            self.assertTrue(self.agent._barge_in_event.is_set())
            source, text = self.agent._take_interruption()
            self.assertEqual((source, text), ("voice", "skip that action"))
        finally:
            self.agent._barge_in_event.clear()
            self.agent._agent_lock.release()


class LiveTaskControls(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import agent
        cls.agent = agent

    def tearDown(self):
        self.agent.cancel_flag = False
        self.agent._task_pause_event.clear()
        self.agent._barge_in_event.clear()

    def test_abort_cancels_the_whole_active_task_without_a_model(self):
        acquired = self.agent._agent_lock.acquire(blocking=False)
        self.assertTrue(acquired)
        try:
            with patch.object(self.agent, "_get_conversation_runtime") as runtime:
                self.assertTrue(self.agent.handle_live_control("abort", "voice"))
            self.assertTrue(self.agent.cancel_flag)
            self.assertTrue(self.agent._barge_in_event.is_set())
            runtime.return_value.cancel.assert_called_once_with()
        finally:
            self.agent._agent_lock.release()

    def test_pause_and_resume_use_an_execution_boundary(self):
        acquired = self.agent._agent_lock.acquire(blocking=False)
        self.assertTrue(acquired)
        try:
            self.assertTrue(self.agent.handle_live_control("pause task", "text"))
            self.assertTrue(self.agent._task_pause_event.is_set())
            self.assertTrue(self.agent.handle_live_control("resume task", "text"))
            self.assertFalse(self.agent._task_pause_event.is_set())
        finally:
            self.agent._agent_lock.release()

    def test_mute_persists_until_unmute_without_muting_windows(self):
        self.agent._assistant_voice_muted.clear()
        try:
            with patch.object(self.agent, "_stop_speech_for_barge_in") as stop:
                self.assertTrue(self.agent.handle_live_control("mute", "voice"))
            self.assertTrue(self.agent.assistant_voice_muted())
            stop.assert_called_once_with()
            self.assertTrue(self.agent.handle_live_control("unmute", "voice"))
            self.assertFalse(self.agent.assistant_voice_muted())
        finally:
            self.agent._assistant_voice_muted.clear()

    def test_muted_voice_keeps_answer_visible_but_never_starts_tts(self):
        self.agent._assistant_voice_muted.set()
        try:
            with patch.object(self.agent, "_speak_now") as output:
                self.agent.speak("A visible answer", show=True)
            output.assert_not_called()
        finally:
            self.agent._assistant_voice_muted.clear()

    def test_open_palm_stops_voice_without_cancelling_the_task(self):
        previous = self.agent.current_state
        self.agent.current_state = self.agent.STATE_SPEAKING
        try:
            with patch.object(self.agent, "_gesture_may_listen", return_value=True), \
                    patch.object(self.agent, "_stop_speech_for_barge_in") as stop, \
                    patch.object(self.agent, "set_state"):
                handled = self.agent._on_gesture_wake({
                    "gesture": "Open_Palm", "confidence": .96})
            self.assertTrue(handled)
            stop.assert_called_once_with()
        finally:
            self.agent.current_state = previous

    def test_closed_fist_requires_a_second_deliberate_event_to_abort(self):
        self.agent._gesture_abort_armed_until = 0.0
        self.agent._agent_lock.acquire()
        try:
            with patch.object(self.agent, "_gesture_may_listen", return_value=True), \
                    patch.object(self.agent, "_cancel_active_task",
                                 return_value=True) as cancel:
                armed = self.agent._on_gesture_wake({
                    "gesture": "Closed_Fist", "confidence": .94})
                self.assertTrue(armed)
                cancel.assert_not_called()
                handled = self.agent._on_gesture_wake({
                    "gesture": "Closed_Fist", "confidence": .94})
            self.assertTrue(handled)
            cancel.assert_called_once_with("confirmed closed-fist gesture")
        finally:
            self.agent._gesture_abort_armed_until = 0.0
            self.agent._agent_lock.release()

    def test_one_ambiguous_closed_fist_never_cancels_work(self):
        self.agent._gesture_abort_armed_until = 0.0
        self.agent._agent_lock.acquire()
        try:
            with patch.object(self.agent, "_gesture_may_listen", return_value=True), \
                    patch.object(self.agent, "_cancel_active_task") as cancel:
                handled = self.agent._on_gesture_wake({
                    "gesture": "Closed_Fist", "confidence": .87})
            self.assertFalse(handled)
            cancel.assert_not_called()
        finally:
            self.agent._gesture_abort_armed_until = 0.0
            self.agent._agent_lock.release()

    def test_mode_transition_is_published_once(self):
        while not self.agent.ui_queue.empty():
            self.agent.ui_queue.get_nowait()

        def mirror_once(message, *_args, **_kwargs):
            self.agent.ui_queue.put(("agent_response", message))

        with patch.object(self.agent, "speak", side_effect=mirror_once):
            self.agent._announce_mode_switch("health")
        messages = []
        while not self.agent.ui_queue.empty():
            kind, payload = self.agent.ui_queue.get_nowait()
            if kind == "agent_response":
                messages.append(payload)
        self.assertEqual(messages, ["Switching to Health mode."])

    def test_thumbs_only_resolve_an_existing_pending_prompt(self):
        with patch.object(self.agent, "_gesture_may_listen", return_value=True), \
                patch.object(self.agent, "handle_approval_input",
                             return_value=True) as approval:
            handled = self.agent._on_gesture_wake({
                "gesture": "Thumb_Up", "confidence": .95})
        self.assertTrue(handled)
        approval.assert_called_once_with("approve", source="gesture")

    def test_victory_toggles_the_dashboard_without_a_model_call(self):
        with patch.object(self.agent, "_gesture_may_listen", return_value=True):
            handled = self.agent._on_gesture_wake({
                "gesture": "Victory", "confidence": .95})
        self.assertTrue(handled)
        messages = []
        while not self.agent.ui_queue.empty():
            messages.append(self.agent.ui_queue.get_nowait())
        self.assertIn(("toggle_dashboard", ""), messages)


class TheScreenRests(unittest.TestCase):
    def test_identity_check_needs_five_idle_minutes_and_a_still_screen(self):
        self.assertFalse(screen_rest.should_verify_identity(
            idle=299, still=True, minutes=5))
        self.assertFalse(screen_rest.should_verify_identity(
            idle=301, still=False, minutes=5))
        self.assertTrue(screen_rest.should_verify_identity(
            idle=301, still=True, minutes=5))
        self.assertFalse(screen_rest.should_verify_identity(
            idle=301, still=True, already_checked=True, minutes=5))

    def setUp(self):
        screen_rest.end(None, "test setup")
        screen_rest._suspended_until = 0.0
        self.addCleanup(lambda: screen_rest.end(None, "test cleanup"))

    def test_a_still_screen_and_idle_hands_rest(self):
        self.assertTrue(screen_rest.should_rest(idle=2400, still=True))

    def test_a_moving_screen_does_not(self):
        """Watching a film is forty minutes of no keyboard. Blacking it out
        would be worse than the problem."""
        self.assertFalse(screen_rest.should_rest(idle=2400, still=False))

    def test_recent_input_does_not(self):
        self.assertFalse(screen_rest.should_rest(idle=5, still=True))

    def test_it_does_not_rest_twice(self):
        screen_rest.begin(None)
        self.assertFalse(screen_rest.should_rest(idle=2400, still=True))

    def test_it_can_be_suspended(self):
        screen_rest.suspend(minutes=10)
        self.assertTrue(screen_rest.suspended())
        self.assertFalse(screen_rest.should_rest(idle=2400, still=True))

    def test_resting_goes_quiet(self):
        import quiet
        screen_rest.begin(None)
        self.assertTrue(quiet.active())

    def test_coming_back_ends_the_quiet(self):
        import quiet
        screen_rest.begin(None)
        screen_rest.end(None, "input")
        self.assertFalse(quiet.active())

    def test_the_ui_is_told_both_ways(self):
        sent = []
        queue = type("Q", (), {"put": lambda _s, item: sent.append(item)})()
        screen_rest.begin(queue)
        screen_rest.end(queue)
        self.assertEqual([kind for kind, _ in sent],
                         ["rest_begin", "rest_end"])

    def test_beginning_twice_changes_nothing(self):
        self.assertTrue(screen_rest.begin(None))
        self.assertFalse(screen_rest.begin(None))

    def test_ending_when_awake_changes_nothing(self):
        self.assertFalse(screen_rest.end(None))


class RestingIsNotStandby(unittest.TestCase):
    """Three different things, and he asked for the third."""

    def test_it_does_not_touch_the_power_state(self):
        """Standby stops the intelligence pipeline; resting must not, or the
        emails he wants recorded stop being recorded."""
        import inspect
        source = inspect.getsource(screen_rest)
        body = source.split('"""', 2)[-1]
        self.assertNotIn("runtime_power", body)

    def test_background_work_continues_while_resting(self):
        import runtime_power
        screen_rest.begin(None)
        self.addCleanup(screen_rest.end, None)
        self.assertTrue(runtime_power.background_allowed(),
                        "resting switched the background off")

    def test_silence_comes_from_quiet_not_a_second_flag(self):
        """A second suppression path is a promise the workers never heard."""
        import inspect
        self.assertIn("quiet.begin", inspect.getsource(screen_rest.begin))


class ComparingFrames(unittest.TestCase):
    def frame(self, values):
        from PIL import Image
        img = Image.new("L", (4, 2))
        img.putdata(values)
        return img

    def test_identical_frames_are_identical(self):
        a = self.frame([10, 20, 30, 40, 50, 60, 70, 80])
        self.assertEqual(screen_rest.frame_difference(a, a), 0.0)

    def test_a_completely_different_frame_is_one(self):
        a = self.frame([0] * 8)
        b = self.frame([255] * 8)
        self.assertEqual(screen_rest.frame_difference(a, b), 1.0)

    def test_a_blinking_cursor_counts_as_still(self):
        a = self.frame([10, 20, 30, 40, 50, 60, 70, 80])
        b = self.frame([10, 20, 30, 40, 50, 60, 70, 90])
        self.assertLessEqual(screen_rest.frame_difference(a, b),
                             screen_rest.STILL_THRESHOLD)

    def test_frames_of_different_sizes_are_treated_as_different(self):
        """A resize mid-comparison must not read as "nothing changed"."""
        from PIL import Image
        a = self.frame([0] * 8)
        b = Image.new("L", (3, 2))
        self.assertEqual(screen_rest.frame_difference(a, b), 1.0)

    def test_two_edits_is_a_different_word(self):
        """Measured: the transcriber writes "Amrutha", which is not close
        enough to correct without inventing."""
        self.assertEqual(vocab.repair("meet me at Amrutha", ["Amrita"]),
                         "meet me at Amrutha")


class TheDarkScreen(unittest.TestCase):
    def test_the_breath_is_a_cycle(self):
        from rest_screen import RestScreenMixin as R
        self.assertAlmostEqual(R._rest_breath(0.0), 0.0, places=5)
        half = R._rest_breath(rest_half())
        self.assertAlmostEqual(half, 1.0, places=5)

    def test_it_never_leaves_the_range(self):
        from rest_screen import RestScreenMixin as R
        for step in range(0, 200):
            value = R._rest_breath(step * 0.1)
            self.assertGreaterEqual(value, 0.0)
            self.assertLessEqual(value, 1.0)

    def test_it_is_slow_enough_not_to_strobe(self):
        import rest_screen
        self.assertGreaterEqual(rest_screen.BREATH_SECONDS, 4.0)

    def test_it_says_it_is_not_a_crash(self):
        import inspect
        import rest_screen
        source = inspect.getsource(rest_screen.RestScreenMixin._rest_tick)
        self.assertIn("still working", source)
        self.assertIn("press any key", source)

    def test_every_kind_of_input_dismisses_it(self):
        import inspect
        import rest_screen
        source = inspect.getsource(
            rest_screen.RestScreenMixin._ensure_rest_screen)
        for sequence in ("<Key>", "<Button>", "<Motion>"):
            with self.subTest(sequence=sequence):
                self.assertIn(sequence, source)

    def test_the_ui_handles_both_messages(self):
        with open(os.path.join(ROOT, "ui.py"), encoding="utf-8") as handle:
            source = handle.read()
        self.assertIn('"rest_begin"', source)
        self.assertIn('"rest_end"', source)

    def test_the_watcher_is_started(self):
        with open(os.path.join(ROOT, "agent.py"), encoding="utf-8") as handle:
            source = handle.read()
        self.assertIn("screen_rest.worker", source)


def rest_half():
    import rest_screen
    return rest_screen.BREATH_SECONDS / 2.0


if __name__ == "__main__":
    unittest.main()
