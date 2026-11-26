import datetime
import json
import os
import tempfile
import unittest
from unittest import mock

import ack
import conversation_output as co
import speech_stream

TODAY = datetime.date(2026, 10, 16)


class Digest(unittest.TestCase):
    def test_default_is_two_sentences(self):
        text = "Running at 82%. Temperature is climbing. Coolant is fine. Also more."
        self.assertEqual(co.spoken_digest(text), "Running at 82%. Temperature is climbing.")

    def test_one_long_sentence_is_clipped_to_the_word_budget(self):
        out = co.spoken_digest("word " * 100, word_limit=10)
        self.assertLessEqual(len(out.split()), 10)

    def test_second_sentence_dropped_when_it_would_blow_the_budget(self):
        text = "Short one. " + "Long " * 60 + "end."
        self.assertEqual(co.spoken_digest(text, word_limit=20), "Short one.")

    def test_expanded_allows_more(self):
        text = " ".join(f"Sentence number {i}." for i in range(8))
        self.assertEqual(len(co.spoken_digest(
            text, 140, co.EXPANDED_SPOKEN_SENTENCES).split(". ")), 6)

    def test_lists_stay_on_screen(self):
        text = "Here are your tasks:\n- pay rent\n- call mom\n- gym\n- laundry"
        out = co.spoken_digest(text, screen_available=True)
        self.assertNotIn("rent", out)
        self.assertIn("on your screen", out)

    def test_pure_list_gets_a_pointer_not_silence(self):
        self.assertEqual(co.spoken_digest("- a\n- b\n- c", screen_available=True),
                         "The details are on your screen.")

    def test_tables_are_not_read(self):
        out = co.spoken_digest("Prices below.\n| a | b |\n| 1 | 2 |")
        self.assertNotIn("|", out)

    def test_empty(self):
        self.assertEqual(co.spoken_digest(""), "")

    def test_asking_for_more_is_detected(self):
        for phrase in ("tell me more", "can you explain that", "walk me through it",
                       "give me the full briefing"):
            self.assertTrue(co.wants_detail(phrase), phrase)
        for phrase in ("what's the weather", "delete the event"):
            self.assertFalse(co.wants_detail(phrase), phrase)


class SpokenDates(unittest.TestCase):
    def test_timestamps_become_speech(self):
        out = co.speak_dates("Gate pass at 2026-10-17T05:00", TODAY)
        self.assertEqual(out, "Gate pass tomorrow at 5 AM")

    def test_weekday_and_far_dates(self):
        self.assertEqual(co.speak_dates("2026-10-19 17:30", TODAY), "Monday at 5:30 PM")
        self.assertEqual(co.speak_dates("due on 2026-12-01", TODAY), "due on 1 December")
        self.assertEqual(co.speak_dates("2027-01-05", TODAY), "5 January 2027")

    def test_nonsense_dates_untouched(self):
        self.assertEqual(co.speak_dates("2026-13-45", TODAY), "2026-13-45")

    def test_tts_path_applies_it(self):
        self.assertNotIn("2026-", co.speech_safe_text("Call at 2026-11-30T09:00"))


class Streamer(unittest.TestCase):
    def test_streamer_stops_after_the_sentence_cap(self):
        said = []
        speaker = speech_stream.StreamingSpeaker(said.append, max_sentences=2)
        for sentence in ("One. ", "Two. ", "Three. ", "Four. "):
            speaker._enqueue(sentence.strip())
        speaker.finish(timeout=5)
        self.assertEqual(said, ["One.", "Two."])


class Lines(unittest.TestCase):
    def test_accepts_short_acknowledgements(self):
        for line in ("On it", "Give me a sec", "Yep, one moment!", "Let me look."):
            self.assertTrue(ack.clean_line(line), line)

    def test_rejects_anything_that_is_not_a_bare_acknowledgement(self):
        for line in ("Done.", "Found it!", "All set", "What do you need?",
                     "Checking 3 things", "see https://x.y", "Sorry, can't",
                     "This one is much too long to be a quick acknowledgement"):
            self.assertEqual(ack.clean_line(line), "", line)

    def test_parse_dedupes_and_validates(self):
        raw = json.dumps({"lines": ["On it", "on it!", "Done.", "Give me a sec", 5]})
        self.assertEqual(ack.parse_lines(raw), ["On it.", "Give me a sec."])

    def test_parse_survives_plain_lines(self):
        self.assertEqual(ack.parse_lines("- On it\n- Hold on"), ["On it.", "Hold on."])

    def test_pleasantries_are_not_requests(self):
        for text in ("thanks", "hi", "ok cool", "good morning", "yes"):
            self.assertFalse(ack.worth_acknowledging(text), text)
        for text in ("what's the weather in chennai", "delete the event",
                     "show me all routes to the mall"):
            self.assertTrue(ack.worth_acknowledging(text), text)


class Pool(unittest.TestCase):
    def setUp(self):
        ack.reset_state()
        self.dir = tempfile.TemporaryDirectory()
        self.base = self.dir.name

    def tearDown(self):
        self.dir.cleanup()

    def make(self, lines):
        def call_llm(messages, task_level=None):
            return json.dumps({"lines": lines})

        def synth(text):
            path = os.path.join(self.base, "tmp.mp3")
            with open(path, "wb") as handle:
                handle.write(b"audio:" + text.encode())
            return path
        return call_llm, synth

    LINES = [f"Hold on {word}" for word in
             "a b c d e f g h i j k l".split()]

    def test_refresh_writes_lines_and_audio(self):
        llm, synth = self.make(["On it", "Give me a sec", "Yep, moment", "Hang on",
                                "Let me see", "Right away", "Sure thing", "Looking",
                                "One sec", "Coming up"])
        ready = ack.refresh(self.base, llm, synth, voice="v1")
        self.assertGreaterEqual(ready, 8)
        pool = ack.load(self.base)
        for entry in pool["lines"]:
            self.assertTrue(os.path.exists(os.path.join(self.base, entry["audio"])))

    def test_no_audio_means_no_acknowledgement_not_a_slow_one(self):
        llm, _ = self.make(["On it"])
        ack.refresh(self.base, llm, lambda text: None, voice="v1")
        self.assertIsNone(ack.pick(self.base))

    def test_pick_rotates_and_respects_cooldown(self):
        llm, synth = self.make(["On it", "Give me a sec", "Yep, moment", "Hang on",
                                "Let me see", "Right away", "Sure thing", "Looking",
                                "One sec", "Coming up"])
        ack.refresh(self.base, llm, synth, voice="v1")
        first = ack.pick(self.base, now=1000.0)
        self.assertIsNotNone(first)
        self.assertIsNone(ack.pick(self.base, now=1001.0))
        seen = {first["text"]}
        for step in range(1, 6):
            seen.add(ack.pick(self.base, now=1000.0 + step * 10)["text"])
        self.assertEqual(len(seen), 6)

    def test_new_persona_discards_old_lines(self):
        llm, synth = self.make(["On it", "Give me a sec", "Yep, moment", "Hang on",
                                "Let me see", "Right away", "Sure thing", "Looking",
                                "One sec", "Coming up"])
        with mock.patch.object(ack, "_tone_text", return_value="breezy"):
            ack.refresh(self.base, llm, synth, voice="v1")
            self.assertFalse(ack.needs_refresh(self.base, "v1"))
        with mock.patch.object(ack, "_tone_text", return_value="formal"):
            self.assertTrue(ack.needs_refresh(self.base, "v1"))

    def test_voice_change_triggers_refresh(self):
        llm, synth = self.make(["On it", "Give me a sec", "Yep, moment", "Hang on",
                                "Let me see", "Right away", "Sure thing", "Looking",
                                "One sec", "Coming up"])
        ack.refresh(self.base, llm, synth, voice="v1")
        self.assertTrue(ack.needs_refresh(self.base, "v2"))

    def test_model_failure_leaves_the_pool_empty_and_does_not_raise(self):
        def boom(*args, **kwargs):
            raise RuntimeError("down")
        self.assertEqual(ack.refresh(self.base, boom, lambda t: None, voice="v"), 0)


if __name__ == "__main__":
    unittest.main()
