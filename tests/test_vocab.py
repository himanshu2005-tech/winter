# -*- coding: utf-8 -*-
"""
Tests for the self-expanding speech vocabulary.

Biasing the transcriber toward words Himanshu actually uses is the difference
between "Havelock" and "Haveloc", measured. The danger is the obvious
implementation: learn from what the transcriber produces. Do that and every
mis-hearing becomes a permanent bias toward itself -- say "Haveloc", get
"Havelock", learn "Havelock", and now it is wrong more confidently forever.

That is the same feedback loop that once turned VANTA's own replayed briefings
into remembered facts, so most of this file is about the boundary between
sources that may introduce a spelling and sources that may only count one.
"""

import io
import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import vocab


class _Isolated(unittest.TestCase):
    """Each test gets its own store, so none of them see the real one."""

    def setUp(self):
        self.base = tempfile.mkdtemp(prefix="vocab-test-")
        vocab._CACHE = None

    def tearDown(self):
        vocab._CACHE = None
        shutil.rmtree(self.base, ignore_errors=True)

    def store(self):
        with io.open(vocab.store_path(self.base), encoding="utf-8") as handle:
            return json.load(handle)


class FeedbackLoopTests(_Isolated):
    """The rule the whole design rests on."""

    def test_speech_cannot_introduce_a_spelling(self):
        vocab.learn_spoken(self.base, "open Havelock and check NXT wave")
        self.assertEqual(vocab.hotwords(self.base), [],
                         "a mis-hearing must never become a learned word")

    def test_typing_can_introduce_a_spelling(self):
        vocab.learn(self.base, "open Haveloc", trusted=True)
        self.assertIn("haveloc", self.store())

    def test_speech_raises_a_known_word(self):
        # Hearing a word often is real evidence it matters; it just is not
        # evidence about how it is spelt.
        vocab.learn(self.base, "Haveloc", trusted=True)
        before = self.store()["haveloc"]["score"]
        vocab._CACHE = None
        vocab.learn_spoken(self.base, "Haveloc Haveloc Haveloc")
        self.assertGreater(self.store()["haveloc"]["score"], before)

    def test_the_wrong_spelling_never_displaces_the_right_one(self):
        vocab.learn(self.base, "Haveloc", trusted=True)
        for _ in range(20):
            vocab.learn_spoken(self.base, "Havelock")
        words = [w.lower() for w in vocab.hotwords(self.base)]
        self.assertNotIn("havelock", words)


class SelectionTests(_Isolated):

    def test_common_words_never_take_a_slot(self):
        vocab.learn(self.base, "because there should always be something "
                               "that could be another thing", trusted=True)
        self.assertEqual(vocab.hotwords(self.base), [])

    def test_ordinary_request_words_are_excluded_too(self):
        # "remind me to open the calendar" is every other sentence; biasing
        # toward it spends the budget on nothing.
        vocab.learn(self.base, "remind open calendar event message reply",
                    trusted=True)
        self.assertEqual(vocab.hotwords(self.base), [])

    def test_short_words_are_skipped(self):
        vocab.learn(self.base, "abc xy q", trusted=True)
        self.assertEqual(vocab.hotwords(self.base), [])

    def test_a_word_must_come_up_more_than_once(self):
        vocab.learn(self.base, "Kokoro", trusted=True)
        self.assertNotIn("Kokoro", vocab.hotwords(self.base))
        vocab._CACHE = None
        vocab.learn(self.base, "Kokoro", trusted=True)
        self.assertIn("Kokoro", vocab.hotwords(self.base))

    def test_casing_is_preserved(self):
        # "NxtWave" and "nxtwave" are different words to a transcriber, and the
        # real form is the useful one.
        vocab.learn(self.base, "NxtWave NxtWave", trusted=True)
        self.assertIn("NxtWave", vocab.hotwords(self.base))

    def test_the_list_is_capped(self):
        """Hotwords are a bias, not a dictionary.

        Past a few dozen rare words the bias thins out and starts costing more
        than it buys.
        """
        many = " ".join(f"Zzqword{n}" for n in range(200))
        vocab.learn(self.base, many, trusted=True, weight=5.0)
        self.assertLessEqual(len(vocab.hotwords(self.base)),
                             vocab.MAX_HOTWORDS)


class ContactQuotaTests(_Isolated):
    """142 contacts would otherwise be the entire vocabulary."""

    def _seed_contacts(self, count=100):
        names = " ".join(f"Contactname{n}" for n in range(count))
        vocab.learn(self.base, names, trusted=True, weight=5.0,
                    source="contact", accumulate=False)

    def test_contacts_cannot_fill_the_whole_budget(self):
        self._seed_contacts()
        vocab.learn(self.base, "NxtWave Haveloc Kokoro", trusted=True, weight=5.0)
        words = vocab.hotwords(self.base)
        for expected in ("NxtWave", "Haveloc", "Kokoro"):
            self.assertIn(expected, words,
                          "contacts squeezed out a word actually used")

    def test_the_share_is_respected(self):
        self._seed_contacts()
        words = vocab.hotwords(self.base)
        contacts = [w for w in words if w.startswith("Contactname")]
        self.assertLessEqual(len(contacts),
                             int(vocab.MAX_HOTWORDS * vocab.CONTACT_SHARE) + 1)

    def test_a_contact_actually_talked_about_stops_being_quota_limited(self):
        self._seed_contacts()
        vocab.learn(self.base, "Contactname7", trusted=True, weight=50.0)
        self.assertIn("Contactname7", vocab.hotwords(self.base))


class SeedingTests(_Isolated):
    """Re-importing the same names on every boot must change nothing."""

    def test_seeding_is_idempotent(self):
        names = "Karthikeya Harshith Koushik"
        for _boot in range(5):
            vocab.learn(self.base, names, trusted=True, weight=2.0,
                        source="contact", accumulate=False)
        scores = {e["word"]: e["score"] for e in self.store().values()}
        for word in ("Karthikeya", "Harshith", "Koushik"):
            self.assertEqual(scores[word], 2.0,
                             "seeding must set a floor, not accumulate")

    def test_use_outranks_the_seed_floor(self):
        vocab.learn(self.base, "Karthikeya", trusted=True, weight=2.0,
                    source="contact", accumulate=False)
        for _ in range(4):
            vocab.learn(self.base, "Karthikeya", trusted=True)
        self.assertGreater(self.store()["karthikeya"]["score"], 2.0)

    def test_seeding_writes_once_not_once_per_name(self):
        """142 separate load-rewrite cycles raced each other on Windows.

        The symptom was "[WinError 5] Access is denied" partway through.
        """
        import inspect
        source = inspect.getsource(vocab.seed)
        self.assertIn('" ".join', source)
        self.assertNotIn("for name in get_all_contacts():", source)


class DurabilityTests(_Isolated):

    def test_a_missing_store_is_not_an_error(self):
        self.assertEqual(vocab.hotwords(self.base), [])

    def test_a_corrupt_store_is_not_an_error(self):
        os.makedirs(os.path.dirname(vocab.store_path(self.base)), exist_ok=True)
        with io.open(vocab.store_path(self.base), "w", encoding="utf-8") as handle:
            handle.write("{ not json at all")
        self.assertEqual(vocab.hotwords(self.base), [])
        # And it recovers rather than staying broken.
        vocab.learn(self.base, "Kokoro Kokoro", trusted=True)
        self.assertIn("Kokoro", vocab.hotwords(self.base))

    def test_temp_files_are_per_process(self):
        # Two VANTA instances must not fight over one temp name.
        import inspect
        self.assertIn("os.getpid()", inspect.getsource(vocab._save))

    def test_a_word_can_be_removed(self):
        vocab.learn(self.base, "Mistake Mistake", trusted=True)
        self.assertIn("Mistake", vocab.hotwords(self.base))
        vocab._CACHE = None
        self.assertTrue(vocab.forget(self.base, "Mistake"))
        self.assertNotIn("Mistake", vocab.hotwords(self.base))

    def test_forgetting_something_unknown_says_so(self):
        self.assertFalse(vocab.forget(self.base, "neverexisted"))

    def test_the_summary_reads_cleanly(self):
        vocab.learn(self.base, "Kokoro Kokoro NxtWave NxtWave", trusted=True)
        text = vocab.summary(self.base)
        self.assertIn("Kokoro", text)
        self.assertIn("words known", text)


class WiringTests(unittest.TestCase):

    def test_the_transcriber_actually_uses_it(self):
        import inspect
        import stt
        self.assertIn("vocab.hotwords", inspect.getsource(stt.vocabulary))

    def test_typed_and_spoken_are_recorded_differently(self):
        """The distinction has to survive into the caller, or it is decorative."""
        import inspect
        import agent
        source = inspect.getsource(agent.run_agent_loop)
        self.assertIn("vocab.learn(BASE_DIR, text, trusted=True)", source)
        self.assertIn("vocab.learn_spoken(BASE_DIR, text)", source)
        # Typed input is the trusted branch.
        typed_at = source.index("vocab.learn(BASE_DIR, text, trusted=True)")
        self.assertIn("is_text_input", source[max(0, typed_at - 200):typed_at])

    def test_recording_cannot_break_a_turn(self):
        import inspect
        import agent
        source = inspect.getsource(agent.run_agent_loop)
        start = source.index("import vocab")
        self.assertIn("except Exception", source[start:start + 500])


if __name__ == "__main__":
    unittest.main()
