import os
import inspect
import tempfile
import unittest
from unittest import mock

import numpy as np

import hud_state
import stt
import wakeword

SR = wakeword.SAMPLE_RATE
FRAME = 1024


def frames(seconds, level):
    count = int(seconds * SR / FRAME)
    return [np.full(FRAME, level, dtype="float32") for _ in range(count)]


#: The gate measures the room over the last six seconds, so a test that starts
#: speaking at once has no room to measure. A realistic start is quiet.
def lead():
    return frames(6.5, 0.0005)


def run(engine, chunks):
    fired = False
    for chunk in chunks:
        if engine.feed(chunk):
            fired = True
            break
    return fired


class LongUtterances(unittest.TestCase):
    """'Hey Vanta, open my placements portal' is longer than a bare name."""

    def engine(self):
        return wakeword.SpeechGateEngine("vanta")

    def test_a_long_command_after_the_name_is_kept(self):
        calls = []

        def fake(samples, hotwords=None):
            calls.append(len(samples) / SR)
            return ("hey vanta" if len(samples) / SR <= wakeword.PREFIX_SECONDS + 0.1
                    else "hey vanta open my placements portal")

        engine = self.engine()
        with mock.patch.object(stt, "transcribe_samples_local", side_effect=fake):
            fired = run(engine, lead() + frames(5.0, 0.05)
                        + frames(1.0, 0.0005))
        self.assertTrue(fired)
        self.assertEqual(engine.trailing, "open my placements portal")
        self.assertEqual(len(calls), 2)          # prefix check, then the whole breath
        self.assertGreater(calls[-1], 4.0)

    def test_a_long_sentence_not_addressed_to_it_is_dropped_after_one_look(self):
        calls = []

        def fake(samples, hotwords=None):
            calls.append(len(samples))
            return "yeah I think that is fine let me check the schedule"

        engine = self.engine()
        with mock.patch.object(stt, "transcribe_samples_local", side_effect=fake):
            fired = run(engine, lead() + frames(6.0, 0.05) + frames(1.0, 0.0005)
                        + frames(0.5, 0.0005))
        self.assertFalse(fired)
        self.assertEqual(len(calls), 1)          # never transcribed in full

    def test_the_gate_recovers_after_a_long_unaddressed_stretch(self):
        responses = iter(["unrelated chatter here", "hey vanta"])

        def fake(samples, hotwords=None):
            return next(responses)

        engine = self.engine()
        with mock.patch.object(stt, "transcribe_samples_local", side_effect=fake):
            run(engine, lead() + frames(5.0, 0.05) + frames(7.0, 0.0005))
            fired = run(engine, frames(1.0, 0.05) + frames(1.0, 0.0005))
        self.assertTrue(fired)

    def test_short_name_alone_still_works(self):
        with mock.patch.object(stt, "transcribe_samples_local", return_value="vanta"):
            self.assertTrue(run(self.engine(), lead() + frames(0.8, 0.05) + frames(1.0, 0.0005)))


class QuietVoices(unittest.TestCase):
    def test_default_threshold_hears_a_desk_distance_voice(self):
        self.assertLessEqual(wakeword.SPEECH_RMS, 0.006)

    def test_a_quiet_clip_is_brought_up_before_transcription(self):
        seen = {}

        class FakeModel:
            def transcribe(self, audio, **kwargs):
                seen["peak"] = float(np.max(np.abs(audio)))
                seen["kwargs"] = kwargs
                return iter([]), None

        with mock.patch.object(stt, "_load_gate", return_value=FakeModel()), \
                mock.patch.object(stt, "_contains_speech", return_value=True):
            stt.transcribe_samples_local(np.full(8000, 0.02, dtype="float32"),
                                         hotwords="vanta")
        self.assertGreater(seen["peak"], 0.4)

    def test_the_name_prompt_is_a_second_pass_only_after_words_without_it(self):
        prompts = []
        biases = []

        class Segment:
            def __init__(self, text):
                self.text, self.avg_logprob, self.no_speech_prob = text, -0.2, 0.01

        class FakeModel:
            def transcribe(self, audio, **kwargs):
                prompts.append(kwargs.get("initial_prompt"))
                biases.append(kwargs.get("hotwords"))
                return iter([Segment("Vanna, check my calendar" if not kwargs.get("initial_prompt")
                                     else "Vanta, check my calendar")]), None

        with mock.patch.object(stt, "_load_gate", return_value=FakeModel()), \
                mock.patch.object(stt, "_contains_speech", return_value=True):
            heard = stt.transcribe_samples_local(np.full(16000, 0.3, dtype="float32"),
                                                 hotwords="vanta")
        self.assertEqual(heard, "Vanta, check my calendar")
        self.assertEqual(prompts[0], None)
        self.assertIn("Vanta", prompts[1])
        self.assertIsNone(biases[0])
        self.assertEqual(biases[1], "vanta")

    def test_empty_wake_recording_is_silent_but_push_to_talk_keeps_feedback(self):
        import inspect
        import agent
        source = inspect.getsource(agent.process_audio)
        self.assertIn("elif was_wake_recording:", source)
        self.assertIn("Empty wake command ignored", source)
        wake_branch = source.index("elif was_wake_recording:")
        self.assertIn("I didn't catch that", source[wake_branch:])

    def test_silence_never_gets_the_prompt(self):
        prompts = []

        class FakeModel:
            def transcribe(self, audio, **kwargs):
                prompts.append(kwargs.get("initial_prompt"))
                return iter([]), None

        with mock.patch.object(stt, "_load_gate", return_value=FakeModel()):
            self.assertEqual(stt.transcribe_samples_local(
                np.zeros(16000, dtype="float32"), hotwords="vanta"), "")
        self.assertEqual(prompts, [])           # never even decoded

    def test_noise_is_not_decoded_even_when_loud(self):
        decoded = []

        class FakeModel:
            def transcribe(self, audio, **kwargs):
                decoded.append(1)
                return iter([]), None

        noise = (np.random.default_rng(3).standard_normal(16000) * 0.2).astype("float32")
        with mock.patch.object(stt, "_load_gate", return_value=FakeModel()):
            self.assertEqual(stt.transcribe_samples_local(noise, hotwords="vanta"), "")
        self.assertEqual(decoded, [])

    def test_a_loud_clip_is_not_attenuated(self):
        seen = {}

        class FakeModel:
            def transcribe(self, audio, **kwargs):
                seen["peak"] = float(np.max(np.abs(audio)))
                return iter([]), None

        with mock.patch.object(stt, "_load_gate", return_value=FakeModel()), \
                mock.patch.object(stt, "_contains_speech", return_value=True):
            stt.transcribe_samples_local(np.full(8000, 0.8, dtype="float32"))
        self.assertAlmostEqual(seen["peak"], 0.8, places=3)


class GateModel(unittest.TestCase):
    def test_the_big_model_is_preferred_with_smaller_fallbacks(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("VANTA_WAKE_STT_MODEL", None)
            os.environ.pop("WINTER_WAKE_STT_MODEL", None)
            chain = stt.gate_candidates()
        self.assertEqual(chain[0], "large-v3-turbo")
        self.assertEqual(chain[1:], ["small.en", "base.en"])

    def test_the_gate_shares_the_local_model_instead_of_loading_a_second(self):
        sentinel = object()
        with mock.patch.dict(os.environ, {"VANTA_WAKE_STT_MODEL": "large-v3-turbo"}), \
                mock.patch.object(stt, "_gate_model", None), \
                mock.patch.object(stt, "_gate_failed", False), \
                mock.patch.object(stt, "_load_local", return_value=sentinel), \
                mock.patch.object(stt, "_load_whisper") as fresh:
            self.assertIs(stt._load_gate(), sentinel)
        fresh.assert_not_called()


class MicSilence(unittest.TestCase):
    def test_pure_silence_trips_once_after_the_window(self):
        import agent
        watch = {"quiet_since": None, "warned": False}
        self.assertEqual(agent.mic_silence_state(0.0, 100.0, watch), "")
        self.assertEqual(agent.mic_silence_state(0.0, 105.0, watch), "")
        message = agent.mic_silence_state(0.0, 100.0 + agent.MIC_SILENCE_SECONDS, watch)
        self.assertIn("pure silence", message)
        self.assertEqual(agent.mic_silence_state(0.0, 200.0, watch), "")   # once only

    def test_any_signal_resets_and_reports_recovery(self):
        import agent
        watch = {"quiet_since": 0.0, "warned": True}
        self.assertEqual(agent.mic_silence_state(0.01, 50.0, watch), "recovered")
        self.assertEqual(agent.mic_silence_state(0.01, 51.0, watch), "")
        self.assertIsNone(watch["quiet_since"])

    def test_a_quiet_room_is_not_silence(self):
        import agent
        watch = {"quiet_since": None, "warned": False}
        for second in range(0, 60):
            self.assertEqual(agent.mic_silence_state(0.0008, float(second), watch), "")

    def test_very_quiet_analogue_signal_is_not_called_a_dead_stream(self):
        import agent
        watch = {"quiet_since": None, "warned": False}
        for second in range(0, 60):
            self.assertEqual(
                agent.mic_silence_state(0.00005, float(second), watch), "")

    def test_dead_stream_requests_a_device_failover(self):
        import agent
        source = inspect.getsource(agent._microphone_watchdog)
        self.assertIn("avoid_current=True", source)
        self.assertIn("digital silence", source)


class StartupScreen(unittest.TestCase):
    def test_a_saved_route_is_not_put_back_on_screen(self):
        with tempfile.TemporaryDirectory() as base:
            hud_state.reset()
            hud_state.show(base, "Study", ["read chapter 3"], card_id="study")
            hud_state.show(base, "College to mall", [], card_id="route",
                           kind="route", payload={"ok": True})
            hud_state.reset()
            restored = hud_state.load(base)
            self.assertEqual(restored, 1)
            self.assertEqual([c["id"] for c in hud_state.cards()], ["study"])
            hud_state.reset()


if __name__ == "__main__":
    unittest.main()
