# -*- coding: utf-8 -*-
"""Three faults that made the system feel broken regardless of how it thought.

None of these is a reasoning problem. Each one was visible in a single real
transcript, on every line of it.

*The voice.* Every reply was preceded by "Online voice unavailable
(WSServerHandshakeError after 25s); using Kokoro for the next 30s." The memo
was a flat 30 seconds, so against an endpoint that was simply unreachable the
cycle was: wait 25 seconds, fail, fall back for 30, wait 25 seconds again.
Every reply carried 25 seconds of dead air before a word came out.

*Telegram.* "Failed to send message: ('Connection aborted.',
ConnectionResetError(10054 ...))" after the answer had already been spoken, on
every single reply. No pooling, no retry: one reset lost the message, and
nothing told him it had gone missing.

*The vault index.* onnxruntime asked the allocator for 10,185,867,264 bytes
and the indexing thread died with it. Every run. So nothing new was embedded,
and semantic recall kept answering from a stale index without ever saying so.
"""

import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class TheVoiceBacksOff(unittest.TestCase):
    """A blip costs 30 seconds. An outage must not cost 25 seconds a line."""

    def setUp(self):
        import voice
        self.voice = voice
        self._saved = (voice._offline_until, voice._consecutive_failures)
        voice._offline_until = 0.0
        voice._consecutive_failures = 0

    def tearDown(self):
        self.voice._offline_until, self.voice._consecutive_failures = self._saved

    def test_the_first_failure_is_the_short_memo(self):
        self.assertEqual(self.voice._offline_memo_seconds(1),
                         self.voice.OFFLINE_MEMO_SECONDS)

    def test_repeated_failures_back_off(self):
        spans = [self.voice._offline_memo_seconds(n) for n in range(1, 6)]
        self.assertEqual(spans, sorted(spans), spans)
        self.assertGreater(spans[-1], spans[0] * 4,
                           "the backoff barely grows, so a dead endpoint is "
                           "still retried every half minute")

    def test_the_backoff_is_capped(self):
        """It must keep rechecking; an outage ends without telling anyone."""
        self.assertEqual(self.voice._offline_memo_seconds(50),
                         self.voice.OFFLINE_MEMO_CEILING)

    def test_a_failure_records_the_streak(self):
        with patch.object(self.voice, "in_utterance", return_value=False):
            self.voice._note_offline("test")
            self.voice._note_offline("test")
        self.assertEqual(self.voice.offline_state()["failures"], 2)

    def test_a_success_clears_the_streak(self):
        with patch.object(self.voice, "in_utterance", return_value=False):
            self.voice._note_offline("test")
            self.voice._note_offline("test")
        self.voice._note_online()
        state = self.voice.offline_state()
        self.assertEqual(state["failures"], 0)
        self.assertEqual(state["seconds_left"], 0.0,
                         "it recovered but is still being treated as offline")

    def test_a_mid_reply_failure_does_not_change_the_voice(self):
        """Sentence one in Ava and sentence three in Kokoro was the complaint."""
        with patch.object(self.voice, "in_utterance", return_value=True):
            self.voice._note_offline("test")
        self.assertEqual(self.voice.offline_state()["failures"], 0)
        self.assertEqual(self.voice.offline_state()["seconds_left"], 0.0)

    def test_an_empty_file_counts_as_a_failure(self):
        """It plays as silence, which looks exactly like being ignored.

        An earlier draft reset the streak as soon as the call returned, so an
        endpoint answering with empty files would never have escalated.
        """
        import inspect
        source = inspect.getsource(self.voice._edge_tts)
        self.assertLess(source.index('_note_offline("empty audio")'),
                        source.index("_note_online()"),
                        "the success reset runs before the empty-audio check")


class TelegramSurvivesAReset(unittest.TestCase):
    def setUp(self):
        from providers import telegram_bot
        self.bot = telegram_bot

    def test_there_is_one_pooled_session(self):
        self.assertIs(self.bot._session(), self.bot._session(),
                      "a new connection per send is what was being reset")

    def test_the_session_retries_connection_errors(self):
        adapter = self.bot._session().get_adapter("https://api.telegram.org")
        retries = adapter.max_retries
        self.assertGreaterEqual(retries.total, 2)
        self.assertGreaterEqual(retries.connect or 0, 2,
                                "a reset mid-connection is the exact failure "
                                "that was losing messages")

    def test_it_backs_off_between_attempts(self):
        adapter = self.bot._session().get_adapter("https://api.telegram.org")
        self.assertGreater(adapter.max_retries.backoff_factor, 0)

    def test_sending_reports_whether_it_worked(self):
        """It returned None either way, so nothing could tell."""
        import inspect
        source = inspect.getsource(self.bot.send_telegram_message)
        self.assertIn("return True", source)
        self.assertIn("return False", source)

    def test_nothing_sends_outside_the_session(self):
        """A bare requests.post gets no pooling and no retry."""
        import inspect
        source = inspect.getsource(self.bot)
        self.assertNotIn("requests.post(", source)
        self.assertNotIn("requests.get(", source)


class EmbeddingIsBatched(unittest.TestCase):
    """The 10 GB allocation, and why the number was that number.

    Self-attention at 256 tokens needs about batch x 12 heads x 256 x 256 x 4
    bytes for the scores alone -- roughly 3 MB per item. 10,185,867,264 bytes
    is about 3,200 items, which is what one vault re-index passed in a single
    call.
    """

    def setUp(self):
        from memory import embeddings
        self.embeddings = embeddings

    def test_the_batch_is_bounded(self):
        self.assertLessEqual(self.embeddings.BATCH_SIZE, 128)
        self.assertGreaterEqual(self.embeddings.BATCH_SIZE, 1)

    def test_a_large_input_is_split(self):
        """The whole point: 3,000 items must not become one tensor."""
        calls = []

        def fake_batch(session, tokenizer, batch):
            calls.append(len(batch))
            return [[0.0] * self.embeddings.DIMENSIONS for _ in batch]

        with patch.object(self.embeddings, "_load",
                          return_value=(None, None)), \
                patch.object(self.embeddings, "_embed_batch", fake_batch):
            vectors = self.embeddings.embed_texts(["x"] * 3000)
        self.assertEqual(len(vectors), 3000)
        self.assertGreater(len(calls), 1, "it was still one call")
        self.assertLessEqual(max(calls), self.embeddings.BATCH_SIZE)

    def test_an_allocator_refusal_halves_the_batch(self):
        """A machine with less memory should get slower, not broken."""
        attempts = []

        def fussy(session, tokenizer, batch):
            attempts.append(len(batch))
            if len(batch) > 4:
                raise RuntimeError(
                    "Failed to allocate memory for requested buffer of size "
                    "10185867264")
            return [[0.0] * self.embeddings.DIMENSIONS for _ in batch]

        with patch.object(self.embeddings, "_load", return_value=(None, None)), \
                patch.object(self.embeddings, "_embed_batch", fussy):
            vectors = self.embeddings.embed_texts(["x"] * 10)
        self.assertEqual(len(vectors), 10)
        self.assertLessEqual(attempts[-1], 4)

    def test_a_real_bug_is_not_retried_as_if_it_were_memory(self):
        def broken(session, tokenizer, batch):
            raise ValueError("tokenizer is misconfigured")

        with patch.object(self.embeddings, "_load", return_value=(None, None)), \
                patch.object(self.embeddings, "_embed_batch", broken):
            with self.assertRaises(RuntimeError) as caught:
                self.embeddings.embed_texts(["x"] * 10)
        self.assertIn("misconfigured", str(caught.exception))

    def test_the_failure_says_where_it_happened(self):
        """"The index failed" with several thousand candidates is unactionable."""
        def broken(session, tokenizer, batch):
            raise ValueError("nope")

        with patch.object(self.embeddings, "_load", return_value=(None, None)), \
                patch.object(self.embeddings, "_embed_batch", broken):
            with self.assertRaises(RuntimeError) as caught:
                self.embeddings.embed_texts(["x"] * 100)
        self.assertIn("of 100", str(caught.exception))

    def test_nothing_in_means_nothing_out(self):
        self.assertEqual(self.embeddings.embed_texts([]), [])

    def test_an_allocator_message_is_recognised(self):
        for message in (
                "Failed to allocate memory for requested buffer of size 1",
                "std::bad_alloc",
                "CUDA out of memory"):
            with self.subTest(message=message):
                self.assertTrue(
                    self.embeddings._is_memory_error(RuntimeError(message)))

    def test_an_ordinary_error_is_not(self):
        self.assertFalse(
            self.embeddings._is_memory_error(ValueError("bad input")))


class OneBadFileDoesNotStopTheIndex(unittest.TestCase):
    """The thread died on the first failure and the tracker was written last,
    so everything already embedded was re-embedded on the next run."""

    def test_each_file_is_contained(self):
        import inspect
        import memory
        source = inspect.getsource(memory.index_vault)
        self.assertIn("upsert_memory_batch", source)
        batch_at = source.index("upsert_memory_batch")
        self.assertIn("try:", source[:batch_at],
                      "one unindexable file still kills the whole run")

    def test_a_failed_file_is_retried_next_time(self):
        """Marking it done would hide the failure forever."""
        import inspect
        import memory
        source = inspect.getsource(memory.index_vault)
        self.assertIn("current.pop(path", source)


if __name__ == "__main__":
    unittest.main()
