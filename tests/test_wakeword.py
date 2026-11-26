# -*- coding: utf-8 -*-
"""
Tests for the wake word, the name, and the privacy claims made about both.

Two things are being defended here.

The first is that the detector distinguishes being ADDRESSED from being TALKED
ABOUT. "Vanta, mute" is an order; "my Vanta build is broken" is a sentence with
the name in it, and a detector that cannot tell them apart will fire while
Himanshu is on a call talking about the thing he built.

The second is the privacy position, which is a claim about code and therefore
testable: audio heard before the wake word must not reach a disk or a network.
These tests assert that the pre-trigger path takes samples rather than paths,
cannot be switched online by configuration, and that the post-trigger recording
is deleted once it has been read. A promise about privacy that nothing checks is
a promise that quietly stops being true.
"""

import io
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import identity
import wakeword


class NameTests(unittest.TestCase):

    def test_the_name_has_one_source(self):
        self.assertEqual(identity.NAME, "Vanta")
        self.assertEqual(identity.UPPER, "VANTA")
        self.assertEqual(identity.LOWER, "vanta")
        self.assertEqual(identity.WAKE_WORD, "Vanta")

    def test_both_env_prefixes_are_read(self):
        """The rename must not break a working .env.

        There are 120 WINTER_* settings in Himanshu's configuration. Renaming
        the prefix in one go would have meant editing all of them correctly on
        the first try, at the cost of a silent fallback to defaults wherever it
        was missed.
        """
        os.environ.pop("VANTA_TEST_SETTING", None)
        os.environ.pop("WINTER_TEST_SETTING", None)
        self.assertEqual(identity.env("TEST_SETTING", "fallback"), "fallback")
        os.environ["WINTER_TEST_SETTING"] = "old"
        self.assertEqual(identity.env("TEST_SETTING"), "old")
        os.environ["VANTA_TEST_SETTING"] = "new"
        self.assertEqual(identity.env("TEST_SETTING"), "new",
                         "the new prefix must win over the old one")
        os.environ.pop("VANTA_TEST_SETTING")
        os.environ.pop("WINTER_TEST_SETTING")

    def test_blank_settings_do_not_count_as_set(self):
        os.environ["VANTA_TEST_BLANK"] = "   "
        try:
            self.assertEqual(identity.env("TEST_BLANK", "fallback"), "fallback")
        finally:
            os.environ.pop("VANTA_TEST_BLANK")

    def test_the_old_name_is_gone_from_what_is_spoken(self):
        import agent
        self.assertIn("VANTA", agent.SYSTEM_PROMPT)
        self.assertNotIn("WINTER", agent.SYSTEM_PROMPT)
        self.assertNotIn("Winter", agent.SYSTEM_PROMPT)

    def test_live_stream_lets_wasapi_choose_a_buffered_callback_size(self):
        import inspect
        import agent
        source = inspect.getsource(agent.make_stream)
        self.assertIn("blocksize=0", source)
        self.assertIn("latency='high'", source)


class AddressedTests(unittest.TestCase):
    """Being addressed, versus being mentioned."""

    def setUp(self):
        self.gate = wakeword.SpeechGateEngine("vanta")

    def test_the_bare_name_wakes_it(self):
        for heard in ("Vanta", "vanta.", "  VANTA  ", "Vanta!"):
            self.assertTrue(self.gate.matches(heard), heard)

    def test_a_lead_in_is_still_an_address(self):
        for heard in ("hey vanta", "ok vanta", "okay Vanta", "um vanta"):
            self.assertTrue(self.gate.matches(heard), heard)

    def test_being_talked_about_does_not_wake_it(self):
        for heard in ("my vanta build is broken",
                      "I was thinking about vanta yesterday",
                      "the vanta project",
                      "we should rename vanta",
                      "what is the weather",
                      ""):
            self.assertFalse(self.gate.matches(heard), heard)

    def test_length_is_not_the_test(self):
        """A long order is still an order.

        An earlier version capped the utterance at three words, which rejected
        "vanta what is the time" -- the exact thing a wake word exists for.
        """
        long_order = "vanta teach me about eigenvalues on the board please"
        self.assertTrue(self.gate.matches(long_order))
        self.assertEqual(self.gate.remainder(long_order),
                         "teach me about eigenvalues on the board please")

    def test_mishearings_of_the_name_still_count_when_addressed(self):
        # A noisy room turns the name into "vanda", "banta", "venta". These
        # used to count on their own, to compensate for a weak transcriber --
        # and they are real names and a real word, so "Wanda told me to call
        # her" woke the assistant (7 false wakes in 36 sentences that were
        # never addressed to it). They count after a lead-in, where "Hey
        # banta" really is someone calling it.
        for heard in ("Hey Vanda", "okay Banta", "hey Venta", "ok Wanta",
                      "Hey Vanko", "okay Vanto"):
            self.assertTrue(self.gate.matches(heard), heard)

    def test_but_not_on_their_own(self):
        for heard in ("Vanda", "Banta", "Venta", "vantage", "Wanta", "Vanko",
                      "Wanda told me to call her"):
            self.assertFalse(self.gate.matches(heard), heard)

    def test_the_strict_spellings_still_count_alone(self):
        for heard in ("Vanta", "vanta.", "Vantaa", "VANTA!"):
            self.assertTrue(self.gate.matches(heard), heard)

    def test_a_command_in_the_same_breath_is_kept(self):
        # Otherwise the old five-second recording started AFTER the command had
        # already been said, and he had to say it twice.
        for heard, expected in (("Vanta, mute.", "mute"),
                                ("vanta louder", "louder"),
                                ("hey vanta next song", "next song"),
                                ("Vanta", "")):
            self.assertEqual(self.gate.remainder(heard), expected, heard)

    def test_a_mention_yields_no_command(self):
        self.assertEqual(self.gate.remainder("my vanta build is broken"), "")

    def test_a_full_follow_up_is_not_limited_to_wake_phrase_length(self):
        """Conversation turns may be sentences, not just two-second names."""
        import numpy as np
        import stt
        self.gate.set_followup_window(True)
        spoken = np.full(1024, 0.05, dtype="float32")
        silence = np.zeros(1024, dtype="float32")
        with mock.patch.object(
                stt, "transcribe_samples_local",
                return_value="move it to tomorrow afternoon"):
            for _ in range(48):  # just over three seconds
                self.assertFalse(self.gate.feed(spoken))
            for _ in range(12):
                self.gate.feed(silence)
                if self.gate.heard:
                    break
        self.assertEqual(self.gate.heard,
                         "move it to tomorrow afternoon")

    def test_follow_up_state_is_latched_for_the_whole_utterance(self):
        self.gate.set_followup_window(True)
        self.gate._speech = [object()]
        self.gate._followup_utterance = True
        self.gate.set_followup_window(False)
        self.assertTrue(self.gate._followup_utterance)

    def test_collection_state_can_drive_the_existing_listening_notch(self):
        self.assertFalse(self.gate.collecting)
        self.gate._speech = [object()]
        self.assertTrue(self.gate.collecting)

    def test_completed_utterance_remembers_that_it_began_in_the_window(self):
        import numpy as np
        import stt
        self.gate.set_followup_window(True)
        spoken = np.full(1024, 0.05, dtype="float32")
        silence = np.zeros(1024, dtype="float32")
        with mock.patch.object(stt, "transcribe_samples_local",
                               return_value="yes move it"):
            for _ in range(4):
                self.gate.feed(spoken)
            self.gate.set_followup_window(False)
            for _ in range(12):
                self.gate.feed(silence)
                if self.gate.heard:
                    break
        self.assertTrue(self.gate.heard_as_followup)

    def test_biased_wake_correction_is_not_exposed_as_a_follow_up(self):
        """A decoder prompt may repair Vanta, never invent user commands."""
        import numpy as np
        import stt
        self.gate.set_followup_window(True)
        spoken = np.full(1024, 0.05, dtype="float32")
        silence = np.zeros(1024, dtype="float32")
        with mock.patch.object(
                stt, "transcribe_samples_local",
                return_value=("what is the time", "fan noise")):
            for _ in range(4):
                self.gate.feed(spoken)
            for _ in range(12):
                self.gate.feed(silence)
                if self.gate.heard:
                    break
        self.assertEqual(self.gate.heard, "what is the time")
        self.assertEqual(self.gate.unbiased_heard, "fan noise")

    def test_prompted_decoder_cannot_invent_an_addressed_time_command(self):
        import numpy as np
        import stt
        spoken = np.full(1024, 0.05, dtype="float32")
        silence = np.zeros(1024, dtype="float32")
        with mock.patch.object(
                stt, "transcribe_samples_local",
                return_value=("vanta what is the time", "fan noise")):
            fired = False
            for frame in [spoken] * 4 + [silence] * 12:
                fired = self.gate.feed(frame) or fired
        self.assertFalse(fired)
        self.assertEqual(self.gate.unbiased_heard, "fan noise")

    def test_prompted_name_repair_keeps_a_command_grounded_in_raw_words(self):
        self.assertTrue(self.gate._biased_wake_supported(
            "vanta what is the time", "hey what is the time"))
        self.assertTrue(self.gate._biased_wake_supported(
            "vanta open firefox", "fanta open firefox"))
        self.assertFalse(self.gate._biased_wake_supported(
            "vanta what is the time", "fan noise"))

    def test_name_bias_prompt_contains_no_executable_example_command(self):
        import inspect
        import stt
        source = inspect.getsource(stt.transcribe_samples_local)
        self.assertIn('f"Hey {title}. {title}. Okay {title}."', source)
        self.assertNotIn('f"Hey {title}. {title}, what is the time?', source)

    def test_scoring_engines_declare_they_carry_no_command(self):
        # openWakeWord and Porcupine score audio and cannot read words, so
        # callers must not expect a command from them.
        self.assertEqual(wakeword.OpenWakeWordEngine.trailing, "")
        self.assertEqual(wakeword.PorcupineEngine.trailing, "")


class EngineSelectionTests(unittest.TestCase):

    def test_the_wrong_name_is_never_substituted(self):
        """"hey jarvis" must not be a fallback.

        It is the one pretrained model available, and using it would mean the
        assistant answers to a name that is not its own -- worse than saying
        plainly that the custom model is missing.
        """
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with io.open(os.path.join(root, "wakeword.py"), encoding="utf-8") as handle:
            source = handle.read()
        self.assertNotIn('wakeword_models=["hey_jarvis"]', source)
        # Named only in the explanation of why it is not used.
        self.assertIn("deliberately NOT a fallback", source)

    def test_a_missing_engine_is_reported_not_hidden(self):
        engine, note = wakeword.build("vanta", prefer="porcupine")
        self.assertIsInstance(note, str)
        self.assertTrue(note.strip(), "a refusal must explain itself")

    def test_instructions_name_both_upgrade_paths(self):
        text = wakeword.how_to_train()
        self.assertIn("vanta.onnx", text)
        self.assertIn("vanta.ppn", text)
        self.assertIn("this machine", text)

    def test_the_threshold_is_above_the_old_default(self):
        # 0.5 false-triggers on a two-syllable name.
        self.assertGreater(wakeword.THRESHOLD, 0.5)


class PrivacyTests(unittest.TestCase):
    """The privacy claims, as assertions about code rather than prose."""

    def _source(self, name):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with io.open(os.path.join(root, name), encoding="utf-8") as handle:
            return handle.read()

    def test_the_wake_gate_takes_samples_not_a_path(self):
        # No file means nothing to leak and nothing to forget to delete.
        import stt
        self.assertTrue(hasattr(stt, "transcribe_samples_local"))
        import inspect
        args = inspect.signature(stt.transcribe_samples_local).parameters
        self.assertIn("samples", args)
        self.assertNotIn("path", args)

    def test_the_wake_gate_cannot_be_switched_online(self):
        import inspect
        import stt
        source = inspect.getsource(stt.transcribe_samples_local)
        for forbidden in ("Groq", "groq", "_transcribe_online", "online_available"):
            self.assertNotIn(forbidden, source,
                             "pre-wake-word audio must never reach a network")

    def test_the_detector_never_writes_audio_anywhere(self):
        source = self._source("wakeword.py")
        for forbidden in ("open(", "wavfile", "scipy.io", "requests", "urlopen"):
            self.assertNotIn(forbidden, source,
                             f"wakeword.py must not use {forbidden}")

    def test_the_pre_trigger_buffer_is_bounded(self):
        # A queue that grows without limit is a recording, whatever it is called.
        source = self._source("agent.py")
        self.assertIn("wakeword_queue.qsize() >= WAKE_QUEUE_CHUNKS", source)
        self.assertIn("wakeword_queue.get_nowait()", source)
        self.assertIn("wakeword_queue.put_nowait(frame)", source)
        self.assertLessEqual(wakeword.MAX_SPEECH, 5.0)

    def test_the_recording_is_deleted_once_it_is_read(self):
        source = self._source("agent.py")
        self.assertNotIn('write("temp.wav"', source,
                         "audio must not be written into the project folder")
        self.assertIn("tempfile.gettempdir()", source)
        self.assertIn("os.remove(clip)", source)

    def test_deletion_happens_even_when_transcription_fails(self):
        import inspect
        import agent
        source = inspect.getsource(agent.process_audio)
        remove_at = source.index("os.remove(clip)")
        finally_at = source.index("finally:")
        self.assertLess(finally_at, remove_at,
                        "a clip that failed to transcribe is just as private")

    def test_audio_clips_can_no_longer_be_committed(self):
        ignored = self._source(".gitignore")
        self.assertIn("*.wav", ignored)

    def test_the_detector_is_deaf_while_vanta_speaks(self):
        # Otherwise it hears itself say its own name.
        import agent
        previous = agent.current_state
        try:
            agent.current_state = agent.STATE_SPEAKING
            self.assertFalse(agent._wake_may_listen())
        finally:
            agent.current_state = previous


class EndpointingTests(unittest.TestCase):

    def test_recording_ends_on_silence_not_a_timer(self):
        """The fixed five seconds was the reason hands-free was unusable.

        A one-word order cost five seconds of waiting and a long one was cut
        off mid-sentence, so the same stopwatch was wrong in both directions.
        """
        import inspect
        import agent
        source = inspect.getsource(agent._record_until_silence)
        self.assertIn("WAKE_ENDPOINT_SILENCE", source)
        self.assertNotIn("time.sleep(5.0)", source)
        worker = inspect.getsource(agent.wakeword_worker)
        self.assertNotIn("time.sleep(5.0)", worker)

    def test_the_endpoint_allows_a_natural_thinking_pause(self):
        import agent
        self.assertGreaterEqual(agent.WAKE_ENDPOINT_SILENCE, 1.8)
        self.assertLessEqual(agent.WAKE_ENDPOINT_SILENCE, 2.5)

    def test_the_live_speech_gate_uses_the_same_endpoint(self):
        import inspect
        import agent
        source = inspect.getsource(agent.wakeword_worker)
        self.assertIn("engine.endpoint_silence = WAKE_ENDPOINT_SILENCE", source)

    def test_silence_before_any_words_is_also_bounded(self):
        import inspect
        import agent
        source = inspect.getsource(agent._record_until_silence)
        # The quiet counter must not be conditional on speech having happened;
        # saying only the name and then nothing should close after two seconds.
        self.assertIn("quiet_for += fresh_seconds", source)
        self.assertNotIn("elif heard_anything", source)

    def test_a_long_request_is_not_truncated_at_five_seconds(self):
        import agent
        self.assertGreater(agent.WAKE_MAX_UTTERANCE, 10)

    def test_the_listener_actually_runs_now(self):
        # It was False and never assigned True anywhere, so none of the code
        # above it had ever executed.
        import agent
        self.assertTrue(agent.wakeword_active)


class DeviceProbeTests(unittest.TestCase):
    """A model that loads is not a model that works.

    faster-whisper constructs happily on CUDA and then fails at the first
    inference with "cublas64_12.dll is not found", because the CUDA runtime is
    a separate install from the driver. On this machine that is exactly what
    happens -- so the wake gate reported itself ready and returned an empty
    string for every utterance, which for a wake word is indistinguishable from
    never being spoken to. The same constructor-only check was in the offline
    fallback, where it would have surfaced as silence with the network down.
    """

    def test_a_device_is_proven_by_transcribing(self):
        import stt
        self.assertTrue(hasattr(stt, "_works"))
        import inspect
        source = inspect.getsource(stt._works)
        self.assertIn("transcribe", source)
        # Generators defer the work, so the result has to be consumed or the
        # probe proves nothing.
        self.assertIn("list(segments)", source)

    def test_both_loaders_go_through_the_probe(self):
        import inspect
        import stt
        for loader in (stt._load_gate, stt._load_local):
            self.assertIn("_load_whisper", inspect.getsource(loader),
                          f"{loader.__name__} must verify its device")

    def test_a_failed_device_falls_back_rather_than_giving_up(self):
        import inspect
        import stt
        source = inspect.getsource(stt._load_whisper)
        self.assertIn('device="cuda"', source)
        self.assertIn('device="cpu"', source)
        cuda_at = source.index('device="cuda"')
        cpu_at = source.index('device="cpu"')
        self.assertLess(cuda_at, cpu_at, "the fast device should be tried first")

    def test_a_totally_broken_engine_raises_instead_of_lying(self):
        import inspect
        import stt
        self.assertIn("raise RuntimeError",
                      inspect.getsource(stt._load_whisper))

    def test_the_gate_answers_without_raising(self):
        # End to end on real numbers: whatever device it settled on, asking it
        # about audio must return a string rather than blowing up mid-turn.
        import numpy as np
        import stt
        if not stt.gate_ready():
            self.skipTest("no local transcriber available on this machine")
        heard = stt.transcribe_samples_local(np.zeros(16000, dtype="float32"))
        self.assertIsInstance(heard, str)


class ShippedDetectorTests(unittest.TestCase):
    """A phrase that works beats a phrase that matches the name.

    openWakeWord ships four trained detectors on disk. They are far more
    reliable than transcribing one invented word with no sentence around it --
    which is why "Vanta" alone can be missed while "hey jarvis" is not. Choosing
    one means the wake phrase differs from the name, which is a normal trade and
    a better one than a wake word that does not fire.
    """

    def test_the_shipped_phrases_are_recognised_as_such(self):
        self.assertEqual(wakeword.pretrained_name("jarvis"), "hey_jarvis_v0.1")
        self.assertEqual(wakeword.pretrained_name("hey jarvis"), "hey_jarvis_v0.1")
        self.assertEqual(wakeword.pretrained_name("alexa"), "alexa_v0.1")

    def test_the_configured_name_has_no_shipped_model(self):
        # If this ever starts passing, someone trained one and the gate is no
        # longer the default path.
        self.assertIsNone(wakeword.pretrained_name("vanta"))

    def test_an_unknown_phrase_falls_through_to_the_gate(self):
        engine, note = wakeword.build("vanta")
        self.assertIsNotNone(engine, "there must always be some way in")
        self.assertIsInstance(note, str)

    def test_the_refusal_names_the_phrases_that_would_work(self):
        _engine, note = wakeword.build("zzzznotaword", prefer="openwakeword")
        self.assertTrue(any(name in note for name in ("jarvis", "alexa"))
                        or "speech gate" in note,
                        f"unhelpful note: {note}")

    def test_pretrained_and_custom_are_described_differently(self):
        # The distinction matters to Himanshu: one answers to the name, the
        # other answers to Jarvis.
        import inspect
        source = inspect.getsource(wakeword.OpenWakeWordEngine.describe)
        self.assertIn("pretrained", source)
        self.assertIn("custom", source)


class CalibrationTests(unittest.TestCase):
    """"It's not listening" has three causes needing opposite fixes."""

    def test_the_tool_exists_and_imports(self):
        import calibrate_wake
        self.assertTrue(hasattr(calibrate_wake, "main"))

    def test_it_distinguishes_the_three_failure_modes(self):
        import inspect
        import calibrate_wake
        source = inspect.getsource(calibrate_wake)
        self.assertIn("VANTA_WAKE_RMS", source)       # too quiet
        self.assertIn("_spelling_variants", source)   # misheard
        self.assertIn("VANTA_WAKE_WORD", source)      # give up and use a shipped one

    def test_it_writes_no_audio_anywhere(self):
        import inspect
        import calibrate_wake
        source = inspect.getsource(calibrate_wake)
        for forbidden in ("wavfile", "scipy.io", "requests", "open("):
            self.assertNotIn(forbidden, source)


class VoiceTests(unittest.TestCase):
    """Female and unhurried, on every one of the three paths."""

    #: The free Microsoft female voices worth using here.
    FEMALE_VOICES = ("Sonia", "Libby", "Jenny", "Michelle", "Aria", "Emily",
                     "Neerja", "Natasha", "Ava", "Emma")

    def test_the_online_voice_is_female(self):
        """Named by property, not by which one.

        This asserted "Sonia" exactly, and failed the moment the voice was
        changed for a good reason -- Sonia slowed and pitched down read as
        breathy rather than efficient. What matters is that it is one of the
        female voices and not the male ones it replaced.
        """
        import voice
        self.assertNotIn("Ryan", voice.DEFAULT_VOICE)
        self.assertNotIn("Prabhat", voice.DEFAULT_VOICE)
        self.assertTrue(
            any(name in voice.DEFAULT_VOICE for name in self.FEMALE_VOICES),
            f"{voice.DEFAULT_VOICE} is not a known female voice")

    def test_the_tone_is_not_pushed_toward_breathy(self):
        """Calm is carried by pacing, not by a low pitch.

        Dropping the pitch and slowing the rate together produced intimacy,
        which is the wrong register for something that reads out a calendar.
        """
        import voice
        pitch = voice.pitch_name()
        self.assertFalse(pitch.startswith("-"),
                         "a lowered pitch plus a slowed rate reads as breathy")
        self.assertGreaterEqual(voice.DEFAULT_RATE_TRIM, -6,
                                "slowed too far to sound brisk")

    def test_the_delivery_is_slowed_a_little(self):
        import voice
        self.assertLess(voice.DEFAULT_RATE_TRIM, 0, "soothing is mostly pace")
        # The conversion itself stays pure; only the delivery is trimmed.
        self.assertEqual(voice._rate_for(1.0), "+0%")
        self.assertLess(_percent(voice._delivery_rate(1.0)), 0)
        # Asking for a faster read must still give one; the trim shifts the
        # scale rather than pinning it.
        self.assertLess(_percent(voice._delivery_rate(1.0)),
                        _percent(voice._delivery_rate(1.2)))

    def test_the_trim_cannot_reach_something_unusable(self):
        import voice
        self.assertGreaterEqual(_percent(voice._delivery_rate(0.1)), -25)
        self.assertLessEqual(_percent(voice._delivery_rate(5.0)), 40)

    def test_the_pitch_carries_an_explicit_sign(self):
        # edge-tts rejects "2Hz" and accepts "+2Hz".
        import voice
        self.assertIn(voice.pitch_name()[0], "+-")

    def test_the_cold_call_gets_more_time_than_a_warm_one(self):
        """The first call pays for DNS, TLS and the websocket handshake.

        Measured at 12.9s cold against 1.4s warm, so an 8s budget failed the
        greeting every single time and then memoised itself offline for a
        minute -- the good voice was never heard at boot.
        """
        import voice
        self.assertGreater(voice.FIRST_CALL_TIMEOUT, voice.ONLINE_TIMEOUT)
        self.assertGreaterEqual(voice.FIRST_CALL_TIMEOUT, 15)

    def test_the_handshake_is_paid_before_anything_is_said(self):
        import inspect
        import agent
        import voice
        self.assertTrue(hasattr(voice, "warm"))
        self.assertIn("voice.warm()", inspect.getsource(agent._init))

    @staticmethod
    def _playback_source():
        """Both halves of speaking, so a refactor cannot hide the answer.

        speak() was split in two when a reply had to claim the speaker for its
        whole length: speak() now waits for the floor and _speak_now does the
        synthesis. Tests that introspected speak() alone started failing on a
        change that altered nothing about the voices.
        """
        import inspect
        import agent
        return (inspect.getsource(agent.speak)
                + inspect.getsource(agent._speak_now))

    def test_the_offline_voice_is_female_with_a_working_fallback(self):
        source = self._playback_source()
        self.assertIn("af_sarah", source)
        # Voice availability varies by Kokoro release, and a missing voice must
        # not become silence.
        self.assertIn("af_bella", source)

    def test_the_last_resort_voice_is_female_too(self):
        self.assertIn("zira", self._playback_source())


def _percent(text):
    return int(str(text).replace("%", ""))


class GateHardeningTests(unittest.TestCase):
    """What calibration actually showed, turned into tests.

    Six attempts at "Vanta" on tiny.en produced "in doubt.", "and", "mind up.",
    "and off.", "grant." and "and I'm back." -- confident nonsense, not near
    misses. The audio was loud (peaks 0.07-0.14 against a 0.012 threshold), so
    the microphone and the energy gate were never the problem. Whisper is
    trained on 30-second windows; half a second of a made-up word gives it
    nothing to go on and it fills the gap with common English.
    """

    def test_the_gate_model_is_no_longer_the_weakest_one(self):
        import stt
        self.assertNotIn("tiny", stt.gate_model_name())

    def test_short_clips_are_padded(self):
        # A half-second of speech at the very start of a 30s window is the
        # pathological case.
        import stt
        self.assertGreater(stt.GATE_PAD_SECONDS, 0)
        import inspect
        source = inspect.getsource(stt.transcribe_samples_local)
        self.assertIn("np.zeros", source)

    def test_the_decoder_is_biased_toward_the_name(self):
        import inspect
        import stt
        self.assertIn("hotwords", inspect.signature(
            stt.transcribe_samples_local).parameters)
        # And the gate actually passes it -- the parameter existing is not the
        # same as it being used.
        source = inspect.getsource(wakeword.SpeechGateEngine.feed)
        self.assertIn("hotwords=self._word", source)

    def test_low_confidence_output_is_discarded(self):
        """The numbers the model already computes distinguish guessing.

        Hallucinations score badly on avg_logprob and no_speech_prob, so no
        extra model is needed to catch them.
        """
        import inspect
        import stt
        source = inspect.getsource(stt.transcribe_samples_local)
        self.assertIn("avg_logprob", source)
        self.assertIn("no_speech_prob", source)
        self.assertLess(stt.GATE_MIN_LOGPROB, 0)

    def test_biasing_does_not_make_it_hear_the_name_in_nothing(self):
        """Hotwords could have made the decoder see the name everywhere.

        This is the risk the bias introduces, so it is the thing to check:
        silence and noise must stay silent.
        """
        import numpy as np
        import stt
        if not stt.gate_ready():
            self.skipTest("no local transcriber available on this machine")
        rng = np.random.default_rng(7)
        cases = {
            "silence": np.zeros(8000, dtype="float32"),
            "white noise": (rng.standard_normal(9600) * 0.05).astype("float32"),
            "hum": (0.1 * np.sin(2 * np.pi * 120 *
                                 np.arange(8000) / 16000)).astype("float32"),
        }
        gate = wakeword.SpeechGateEngine("vanta")
        for label, audio in cases.items():
            heard = stt.transcribe_samples_local(audio, hotwords="Vanta")
            self.assertFalse(gate.matches(heard),
                             f"{label} was heard as the wake word: {heard!r}")


class MicrophoneChoiceTests(unittest.TestCase):
    """The selected input follows connected hardware and Windows defaults."""

    def choose(self, names, default=0, working=None):
        import agent
        devices = [{"name": name, "max_input_channels": 1,
                    "default_samplerate": 48000} for name in names]
        allowed = set(range(len(names)) if working is None else working)
        return agent.choose_microphone_index(
            devices, default, lambda index: index in allowed)

    def test_a_connected_headset_is_preferred(self):
        chosen, why = self.choose(
            ["Microphone Array (Intel)", "AirPods Bluetooth Headset"],
            default=0)
        self.assertEqual(chosen, 1)
        self.assertIn("connected", why)

    def test_airpods_are_external_even_when_windows_omits_headset(self):
        chosen, why = self.choose(
            ["Microphone Array (Intel)", "Himanshu's AirPods Pro"],
            default=0)
        self.assertEqual(chosen, 1)
        self.assertIn("connected", why)

    def test_windows_default_is_used_without_an_external_device(self):
        chosen, why = self.choose(
            ["Microphone Array (Intel)", "Studio microphone"], default=1)
        self.assertEqual(chosen, 1)
        self.assertEqual(why, "Windows default")

    def test_a_disconnected_headset_is_not_selected(self):
        chosen, _why = self.choose(
            ["Microphone Array (Intel)", "USB Headset"],
            default=0, working={0})
        self.assertEqual(chosen, 0)

    def test_the_docstring_and_the_code_agree(self):
        import agent
        doc = agent.find_best_mic.__doc__ or ""
        self.assertIn("windows default", doc.lower())
        self.assertIn("connected headset", doc.lower())

    def test_an_explicit_choice_overrides_the_heuristic(self):
        import inspect
        import agent
        source = inspect.getsource(agent.find_best_mic)
        self.assertIn('identity.env("MIC"', source)
        # And it is consulted before any guessing happens.
        self.assertLess(source.index('identity.env("MIC"'),
                        source.index("choose_microphone_index"))

    def test_a_device_is_tested_before_it_is_chosen(self):
        # A device that cannot open is not a candidate, however well it matches.
        import inspect
        import agent
        source = inspect.getsource(agent.choose_microphone_index)
        self.assertIn("if not usable(index):", source)

    def test_the_choice_can_be_re_resolved(self):
        """Caching the index forever meant a unplugged headset became silence.

        Which is indistinguishable from not being spoken to, so it looked like
        the wake word had broken.
        """
        import agent
        self.assertTrue(hasattr(agent, "forget_best_mic"))
        agent.forget_best_mic()
        self.assertIsNone(agent._cached_best_mic)
        self.assertFalse(agent._cached_best_mic_valid)

    def test_hotplug_changes_are_monitored(self):
        import inspect
        import agent
        source = inspect.getsource(agent._microphone_watchdog)
        self.assertIn("_microphone_inventory_signature", source)
        self.assertIn("_start_or_replace_microphone", source)

    def test_repeated_audio_status_does_not_continuously_reset_the_gate(self):
        import agent
        watch = {"last_signal": 0.0}
        self.assertTrue(agent.should_refresh_after_audio_status(100.0, watch))
        self.assertFalse(agent.should_refresh_after_audio_status(101.0, watch))
        self.assertTrue(agent.should_refresh_after_audio_status(
            100.0 + agent.MIC_STATUS_RESET_COOLDOWN, watch))

    def test_a_narrowband_device_is_called_out(self):
        # Choosing it is allowed; doing so silently is not, because it caps
        # wake-word accuracy in a way nothing else explains.
        import inspect
        import agent
        self.assertIn("narrowband", inspect.getsource(agent.find_best_mic))


class TranscriptionQualityTests(unittest.TestCase):
    """Why voice input was poor, and it was not the model.

    The online path was already whisper-large-v3-turbo. Three other things were
    degrading it, measured rather than guessed:

    * MIC_GAIN was 8.0 into a hard clip, against measured peaks of 0.07-0.1655.
      Anything above 0.125 clipped flat, so speaking up -- the natural response
      to not being understood -- distorted the audio further.
    * The local fallback was small.en, a large step down whenever the network
      wobbled.
    * ctranslate2 could only find cublas once torch had been imported, so the
      same model ran on the GPU (516ms) or the CPU (6810ms) depending on import
      order alone.
    """

    def test_audio_is_peak_normalised_rather_than_multiplied(self):
        import inspect
        import agent
        source = inspect.getsource(agent.process_audio)
        self.assertNotIn("audio_np * MIC_GAIN", source,
                         "a fixed multiplier clips loud speech")
        self.assertIn("MIC_TARGET_PEAK", source)

    def test_normalisation_cannot_clip_and_cannot_amplify_silence(self):
        import agent
        self.assertLess(agent.MIC_TARGET_PEAK, 1.0, "leave headroom")
        self.assertGreater(agent.MIC_TARGET_PEAK, 0.5)
        # Without a ceiling, a near-silent clip becomes amplified room noise,
        # which a transcriber reports as confident nonsense.
        self.assertGreater(agent.MIC_MAX_GAIN, 1)
        self.assertLess(agent.MIC_MAX_GAIN, 200)

    def test_cuda_is_made_findable_on_purpose(self):
        """Importing torch is what puts cublas on the DLL search path.

        Relying on something else having imported it first made the fast path an
        accident of import order -- a 13x difference decided by luck.
        """
        import inspect
        import stt
        self.assertIn("import torch", inspect.getsource(stt._cuda_available))
        self.assertIn("_cuda_available()", inspect.getsource(stt._load_whisper))

    def test_the_cpu_fallback_is_not_the_big_model(self):
        # large-v3-turbo took 6.8s for a 7.3s clip on this CPU: about real time,
        # which for dictation means waiting as long as you spoke.
        import stt
        self.assertEqual(stt._cpu_size("large-v3-turbo"), "small.en")
        self.assertEqual(stt._cpu_size("distil-large-v3.5"), "small.en")
        # A model that is already small is left alone.
        self.assertEqual(stt._cpu_size("base.en"), "base.en")

    def test_the_gate_stays_small_while_the_transcriber_grows(self):
        """Still true: the gate runs on every sound in the room, the main
        transcriber once per request. What changed is how small "small" is --
        base.en recognised 15 of 24 attempts, small.en 23 of 24, for ninety
        milliseconds more, and the GPU makes that affordable."""
        import stt
        # Superseded: the gate now shares the resident large model (30/30 with
        # a name prompt against 28/30 for small.en), so it costs no extra
        # memory. It must never be the tiny model, which guesses.
        self.assertNotIn("tiny", stt.gate_model_name())
        self.assertEqual(stt.gate_candidates()[-1], "base.en")

    def test_the_named_configuration_prefix_controls_high_quality_stt(self):
        import inspect
        import stt
        source = inspect.getsource(stt)
        self.assertIn('"VANTA_STT_MODEL"', source)
        self.assertIn('"whisper-large-v3"', source)
        self.assertIn('"VANTA_STT_LOCAL_MODEL"', source)
        self.assertIn('"large-v3-turbo"', source)

    def test_the_vocabulary_biases_toward_names_that_matter(self):
        """Proper nouns are where a general model reliably loses.

        Measured on one sentence: "NXT Wave"/"Havelock" without the vocabulary,
        "NxtWave"/"Haveloc" with it, and slightly faster with than without.
        """
        import stt
        self.assertIn("Vanta", stt.vocabulary())
        os.environ["VANTA_STT_VOCAB"] = "NxtWave, Haveloc, NxtWave"
        try:
            words = stt.vocabulary()
            self.assertIn("NxtWave", words)
            self.assertIn("Haveloc", words)
            # Duplicates would waste the bias on one word.
            self.assertEqual(len(words), len(set(w.lower() for w in words)))
        finally:
            os.environ.pop("VANTA_STT_VOCAB", None)

    def test_the_vocabulary_actually_reaches_the_transcriber(self):
        import inspect
        import stt
        self.assertIn("hotwords", inspect.getsource(stt._transcribe_local))

    def test_a_permissions_failure_is_not_blamed_on_cuda(self):
        """WinError 1314 is a Windows symlink privilege, not a GPU problem.

        Reporting it as "CUDA unavailable" sends the next person hunting
        drivers for a filesystem permission.
        """
        import inspect
        import stt
        source = inspect.getsource(stt._load_whisper)
        self.assertIn("WinError 1314", source)
        self.assertIn("HF_HUB_DISABLE_SYMLINKS",
                      inspect.getsource(stt).split("def ")[0])


if __name__ == "__main__":
    unittest.main()
