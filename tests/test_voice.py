# -*- coding: utf-8 -*-
"""
Tests for which voice VANTA speaks in.

Kokoro was doing all the speaking. It is the right choice with no network -- it
is local and it always works -- and the wrong one when there is a network, where
a free neural voice is available for nothing.

The part worth testing is not the audio. It is the fallback discipline:
Himanshu's connection drops several times an hour, and an implementation that
re-pays an eight second timeout on every sentence to rediscover that would be
worse than sounding local. A failure is remembered for a minute.

Network tests are opt-in so a dropped wifi does not fail the suite.
"""

import os
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import voice

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class VoiceChoiceTests(unittest.TestCase):

    def setUp(self):
        self.saved_voice = os.environ.pop("WINTER_TTS_VOICE", None)
        self.saved_offline = os.environ.pop("WINTER_TTS_OFFLINE", None)
        voice._offline_until = 0.0

    def tearDown(self):
        for key, value in (("WINTER_TTS_VOICE", self.saved_voice),
                           ("WINTER_TTS_OFFLINE", self.saved_offline)):
            os.environ.pop(key, None)
            if value is not None:
                os.environ[key] = value
        voice._offline_until = 0.0

    def test_there_is_a_default_voice(self):
        self.assertTrue(voice.voice_name())

    def test_the_voice_can_be_changed_without_touching_code(self):
        os.environ["WINTER_TTS_VOICE"] = "en-IN-PrabhatNeural"
        self.assertEqual(voice.voice_name(), "en-IN-PrabhatNeural")

    def test_the_online_voice_can_be_switched_off(self):
        os.environ["WINTER_TTS_OFFLINE"] = "1"
        self.assertFalse(voice.online_voice_enabled())

    def test_online_is_the_default(self):
        self.assertTrue(voice.online_voice_enabled())


class FallbackMemoTests(unittest.TestCase):
    """The part that matters on a flaky connection."""

    def setUp(self):
        voice._offline_until = 0.0

    def tearDown(self):
        voice._offline_until = 0.0

    def test_a_failure_stops_further_attempts(self):
        voice._note_offline("test")
        self.assertFalse(voice.online_voice_enabled())

    def test_the_memo_expires_rather_than_sticking(self):
        # Otherwise a single drop would mean a local voice until restart.
        voice._note_offline("test")
        voice._offline_until = time.time() - 1
        self.assertTrue(voice.online_voice_enabled())

    def test_the_memo_is_about_a_minute(self):
        self.assertGreaterEqual(voice.OFFLINE_MEMO_SECONDS, 30)
        self.assertLessEqual(voice.OFFLINE_MEMO_SECONDS, 300)

    def test_the_online_attempt_is_bounded(self):
        # An unbounded wait would hang the whole turn on a dead connection.
        self.assertLessEqual(voice.ONLINE_TIMEOUT, 15)


class RateTests(unittest.TestCase):
    """Kokoro takes a multiplier, edge-tts takes a percentage."""

    def test_normal_speed_is_no_change(self):
        self.assertEqual(voice._rate_for(1.0), "+0%")

    def test_slower_and_faster_convert(self):
        self.assertEqual(voice._rate_for(0.8), "-20%")
        self.assertEqual(voice._rate_for(1.25), "+25%")

    def test_rubbish_does_not_raise(self):
        self.assertEqual(voice._rate_for(None), "+0%")
        self.assertEqual(voice._rate_for("fast"), "+0%")


class ExpressiveDeliveryTests(unittest.TestCase):

    def test_emotional_conversation_uses_warm_delivery(self):
        profile = voice.delivery_for("That sounds painful. I'll stay with you.")
        self.assertEqual(profile.name, "warm")
        self.assertLess(profile.rate_offset, 0)

    def test_success_uses_brighter_delivery(self):
        profile = voice.delivery_for("Done — everything passed!")
        self.assertEqual(profile.name, "bright")
        self.assertGreater(profile.pitch, "+0Hz")

    def test_urgent_speech_stays_clear_not_slow(self):
        profile = voice.delivery_for("Danger. Call 112 immediately.")
        self.assertEqual(profile.name, "urgent")
        self.assertGreater(profile.rate_offset, 0)

    def test_ordinary_speech_remains_neutral(self):
        self.assertEqual(voice.delivery_for("The file is open.").name, "neutral")


class LocalFallbackTests(unittest.TestCase):

    def setUp(self):
        voice._offline_until = 0.0
        os.environ["WINTER_TTS_OFFLINE"] = "1"

    def tearDown(self):
        os.environ.pop("WINTER_TTS_OFFLINE", None)
        voice._offline_until = 0.0

    def test_the_local_synth_is_used_when_online_is_off(self):
        calls = []

        def fake_kokoro(body, speed):
            calls.append((body, speed))
            return "C:/fake/audio.wav"

        path, engine = voice.speak_to_file("hello", 1.0,
                                          kokoro_synth=fake_kokoro)
        self.assertEqual(engine, "kokoro")
        self.assertEqual(path, "C:/fake/audio.wav")
        self.assertEqual(calls, [("hello", 1.0)])

    def test_a_local_failure_is_survived(self):
        def broken(body, speed):
            raise RuntimeError("no torch")

        path, engine = voice.speak_to_file("hello", 1.0, kokoro_synth=broken)
        self.assertIsNone(path)
        self.assertIsNone(engine)

    def test_empty_text_is_not_synthesised(self):
        called = []
        voice.speak_to_file("   ", 1.0,
                            kokoro_synth=lambda b, s: called.append(b))
        self.assertEqual(called, [])

    def test_markdown_is_cleaned_at_the_voice_boundary(self):
        heard = []
        with mock.patch.object(voice, "online_voice_enabled", return_value=False):
            voice.speak_to_file("- **Memory matters**", 1.0,
                                kokoro_synth=lambda body, speed: heard.append(body))
        self.assertEqual(heard, ["Memory matters"])


class WiringTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        with open(os.path.join(ROOT, "agent.py"), encoding="utf-8") as handle:
            cls.agent = handle.read()

    def test_the_agent_asks_voice_which_engine_to_use(self):
        self.assertIn("voice.speak_to_file(", self.agent)

    def test_kokoro_is_passed_in_as_the_local_option(self):
        self.assertIn("kokoro_synth=_kokoro_to_file", self.agent)

    def test_kokoro_still_exists(self):
        # Removing it would leave VANTA mute without a network.
        self.assertIn("KPipeline(lang_code='a', device='cpu')", self.agent)

    def test_silence_is_never_the_answer(self):
        self.assertIn("not silence", self.agent)


@unittest.skipUnless(os.getenv("WINTER_NETWORK_TESTS"),
                     "set WINTER_NETWORK_TESTS=1 to synthesise for real")
class LiveTests(unittest.TestCase):

    def setUp(self):
        voice._offline_until = 0.0

    def test_the_online_voice_produces_audio(self):
        path, engine = voice.speak_to_file(
            "Good afternoon, sir. Your exam is on Monday.", 1.0)
        if engine is None:
            self.skipTest("no network")
        self.assertEqual(engine, "edge")
        try:
            self.assertGreater(os.path.getsize(path), 512)
        finally:
            if path and os.path.exists(path):
                os.remove(path)


if __name__ == "__main__":
    unittest.main()
