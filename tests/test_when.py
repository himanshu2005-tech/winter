# -*- coding: utf-8 -*-
"""
Tests for date phrasing, and for the calendar lines that carry it.

The bug these exist for: a reminder at 07:30 that morning was announced as due
tomorrow. The model was handed a raw ISO timestamp and the current time in a
different format, and asked to work out the day by inference. It is arithmetic,
so it belongs in code, and these check that it stays there.
"""

import datetime
import os
import re
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import when

IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
NOW = datetime.datetime(2026, 9, 22, 15, 37, tzinfo=IST)


class DayNameTests(unittest.TestCase):

    def describe(self, stamp):
        return when.describe(stamp, NOW)

    def test_this_morning_is_today_not_tomorrow(self):
        # The exact failure: 07:30 seen at 15:37 read as tomorrow morning.
        phrase = self.describe("2026-09-22T07:30:00+05:30")
        self.assertIn("today", phrase)
        self.assertNotIn("tomorrow", phrase)

    def test_an_event_that_has_passed_says_so(self):
        # "Today 07:30" at half past three is still ambiguous about whether it
        # has happened; the relative phrase is what settles it.
        self.assertIn("ago", self.describe("2026-09-22T07:30:00+05:30"))

    def test_later_today_is_today(self):
        phrase = self.describe("2026-09-22T19:00:00+05:30")
        self.assertIn("today", phrase)
        self.assertIn("in ", phrase)

    def test_tomorrow_is_tomorrow(self):
        self.assertIn("tomorrow", self.describe("2026-09-23T07:30:00+05:30"))

    def test_yesterday_is_yesterday(self):
        self.assertIn("yesterday", self.describe("2026-09-21T09:00:00+05:30"))

    def test_further_out_gets_a_dated_weekday(self):
        phrase = self.describe("2026-09-25T14:00:00+05:30")
        self.assertIn("25 Sep", phrase)
        self.assertNotIn("today", phrase)

    def test_midnight_tonight_is_still_today(self):
        self.assertIn("today", self.describe("2026-09-22T23:59:00+05:30"))

    def test_one_minute_later_is_tomorrow(self):
        self.assertIn("tomorrow", self.describe("2026-09-23T00:01:00+05:30"))

    def test_all_day_events_have_no_clock(self):
        phrase = self.describe("2026-09-22")
        self.assertIn("today", phrase)
        self.assertIn("all day", phrase)

    def test_a_different_timezone_is_compared_fairly(self):
        # 03:00 UTC on the 22nd is 08:30 IST the same morning, not the day
        # before, which is what a naive string comparison would give.
        self.assertIn("today", self.describe("2026-09-22T03:00:00Z"))

    def test_a_trailing_z_parses(self):
        self.assertIsNotNone(when.parse("2026-09-22T03:00:00Z"))

    def test_unparseable_input_comes_back_untouched(self):
        # A slightly ugly line beats a dropped event.
        self.assertEqual(self.describe("sometime next week"), "sometime next week")
        self.assertEqual(self.describe(""), "")

    def test_is_past_agrees_with_the_phrase(self):
        self.assertTrue(when.is_past("2026-09-22T07:30:00+05:30", NOW))
        self.assertFalse(when.is_past("2026-09-22T19:00:00+05:30", NOW))


class RelativePhraseTests(unittest.TestCase):

    def phrase(self, stamp):
        return when.relative(when.parse(stamp), NOW)

    def test_minutes_hours_and_days(self):
        self.assertIn("minutes", self.phrase("2026-09-22T16:07:00+05:30"))
        self.assertIn("hours", self.phrase("2026-09-22T18:37:00+05:30"))
        self.assertIn("days", self.phrase("2026-09-25T15:37:00+05:30"))

    def test_the_immediate_present_is_just_now(self):
        self.assertEqual(self.phrase("2026-09-22T15:37:30+05:30"), "now")

    def test_past_and_future_read_differently(self):
        self.assertTrue(self.phrase("2026-09-22T13:37:00+05:30").endswith("ago"))
        self.assertTrue(self.phrase("2026-09-22T17:37:00+05:30").startswith("in "))

    def test_a_gap_that_rounds_up_is_not_forced_to_one(self):
        # An exam 1.76 days away was announced as "in 1 day": the old form
        # was "n units if n >= 2 else 1 unit", so anything under two became
        # the singular no matter what it rounded to. The model spotted it and
        # obeyed the instruction not to recompute, which is exactly what that
        # instruction is for.
        self.assertEqual(self.phrase("2026-09-22T15:37:00+05:30"), "now")
        base = datetime.datetime(2026, 9, 26, 13, 16, tzinfo=IST)
        cases = {
            datetime.timedelta(days=1, hours=18, minutes=14): "in 2 days",
            datetime.timedelta(days=1, hours=22): "in 2 days",
            datetime.timedelta(days=1, hours=5): "in 1 day",
            datetime.timedelta(days=1): "in 1 day",
            datetime.timedelta(hours=1, minutes=36): "in 2 hours",
            datetime.timedelta(hours=1): "in 1 hour",
        }
        for gap, expected in cases.items():
            self.assertEqual(when.relative(base + gap, base), expected,
                             f"{gap} should read {expected!r}")

    def test_a_single_unit_is_not_pluralised_wrongly(self):
        self.assertEqual(self.phrase("2026-09-22T16:37:00+05:30"), "in 1 hour")
        self.assertEqual(self.phrase("2026-09-23T15:37:00+05:30"), "in 1 day")


class CalendarLineTests(unittest.TestCase):
    """The formatters must not go back to emitting bare timestamps."""

    @classmethod
    def setUpClass(cls):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "providers", "google_api.py"),
                  encoding="utf-8") as handle:
            cls.google = handle.read()
        with open(os.path.join(root, "agent.py"), encoding="utf-8") as handle:
            cls.agent = handle.read()

    def test_both_calendar_listings_resolve_the_day(self):
        self.assertEqual(self.google.count("when.describe(start)"), 2,
                         "a calendar listing is still emitting a raw timestamp")

    def test_no_calendar_line_emits_a_bare_start(self):
        self.assertNotIn('f"- {start}: {summary}', self.google)
        self.assertNotIn('f"- [{event_id}] {start}: {summary}', self.google)

    def test_alarms_say_which_day_they_are_for(self):
        self.assertIn("today at {item['time']}", self.agent,
                      "the alarm line dropped its day again")

    def test_the_context_tells_the_model_not_to_recompute(self):
        import agent
        with mock.patch("alarms.get_upcoming_today", return_value=[]):
            context = agent.gather_context(resources=("alarms",))
        self.assertIn("already resolved to today / tomorrow", context)


class AnchorTests(unittest.TestCase):
    """A stored note must not rot into a wrong claim overnight."""

    WRITTEN = datetime.datetime(2026, 9, 21, 19, 0, tzinfo=IST)

    def anchor(self, text):
        return when.anchor(text, self.WRITTEN)

    def test_the_note_that_caused_the_bug(self):
        # Written the evening before, injected into every turn afterwards,
        # and still saying "tomorrow" the next evening.
        out = self.anchor("Himanshu's TQM exam is scheduled for tomorrow")
        self.assertNotIn("tomorrow", out.lower())
        self.assertIn("22 Sep 2026", out)

    def test_today_and_yesterday_resolve_too(self):
        self.assertIn("21 Sep 2026", self.anchor("he said so today"))
        self.assertIn("20 Sep 2026", self.anchor("he said so yesterday"))

    def test_longer_phrases_win_over_shorter_ones(self):
        # "the day after tomorrow" must not resolve as "tomorrow".
        out = self.anchor("he travels the day after tomorrow")
        self.assertIn("23 Sep 2026", out)
        self.assertNotIn("22 Sep", out)

    def test_time_of_day_survives_the_rewrite(self):
        out = self.anchor("he mentioned it this morning")
        self.assertIn("the morning of", out)
        self.assertIn("21 Sep 2026", out)

    def test_text_without_day_words_is_untouched(self):
        original = "Use GET_TRAVEL_TIME for distances."
        self.assertEqual(self.anchor(original), original)

    def test_empty_input_is_safe(self):
        self.assertEqual(when.anchor(""), "")
        self.assertIsNone(when.anchor(None))

    def test_matching_is_case_insensitive_but_only_whole_words(self):
        self.assertIn("22 Sep 2026", self.anchor("Tomorrow is the exam"))
        # Not a day word in the middle of another token.
        self.assertEqual(self.anchor("todays_plan.md"), "todays_plan.md")


class StoredFactTests(unittest.TestCase):
    """The write path and the render path, checked at the source."""

    @classmethod
    def setUpClass(cls):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        cls.root = root
        with open(os.path.join(root, "reflect.py"), encoding="utf-8") as handle:
            cls.reflect = handle.read()
        with open(os.path.join(root, "providers", "google_api.py"),
                  encoding="utf-8") as handle:
            cls.google = handle.read()

    def test_facts_are_anchored_before_being_stored(self):
        self.assertIn('when.anchor(entry["fact"]', self.reflect)

    def test_precedents_say_when_they_were_concluded(self):
        self.assertIn("[noted {noted}]", self.reflect)

    def test_no_stored_fact_still_carries_a_relative_day(self):
        # The repair pass ran; nothing in the file should say "tomorrow"
        # about a specific commitment any more.
        import json
        path = os.path.join(self.root, "vault", "memory",
                            "derived_defaults.json")
        if not os.path.exists(path):
            self.skipTest("no derived defaults on this machine")
        with open(path, encoding="utf-8") as handle:
            items = json.load(handle)
        offenders = [it.get("fact") for it in items
                     if "tomorrow" in (it.get("fact") or "").lower()
                     and "scheduled for" in (it.get("fact") or "").lower()]
        self.assertEqual(offenders, [])

    def test_both_listings_separate_finished_events(self):
        # A heading saying "upcoming" over an event that has already happened
        # is the contradiction the model resolved by inventing tomorrow.
        self.assertEqual(self.google.count("Already happened"), 2)
        self.assertNotIn('output = "Upcoming Events (with IDs', self.google)
        self.assertNotIn('output = "Upcoming Calendar Events:', self.google)

    def test_an_all_day_event_is_never_filed_as_finished(self):
        self.assertEqual(self.google.count("all_day = 'T' not in str(start)"), 2)


class BriefingFeedbackTests(unittest.TestCase):
    """The loop that kept the bug alive after the calendar was already right.

    Every proactive turn replays VANTA's own recent briefings. One of them
    said "the reminder is set for 7:30 AM tomorrow", which was true the
    evening it was said and wrong every evening after -- and because each new
    briefing was saved and replayed in turn, the error reproduced itself until
    the single correct briefing was outnumbered five to one.
    """

    @classmethod
    def setUpClass(cls):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        cls.root = root
        with open(os.path.join(root, "autonomy.py"), encoding="utf-8") as handle:
            cls.source = handle.read()

    def test_a_briefing_is_anchored_before_it_is_stored(self):
        record = self.source[self.source.find("def record_briefing"):]
        record = record[:record.find("\ndef ", 1) if "\ndef " in record[1:] else len(record)]
        self.assertIn("when.anchor(cleaned)", record,
                      "briefings are being stored with raw day words again")

    def test_replayed_briefings_are_dated(self):
        render = self.source[self.source.find("def recent_briefings"):]
        self.assertIn("(said {said})", render)

    def test_the_header_no_longer_presents_them_as_facts(self):
        # "do not repeat unchanged facts" read as "these are facts".
        self.assertNotIn("RECENT BRIEFINGS (do not repeat unchanged facts)",
                         self.source)
        self.assertIn("WHAT YOU SAID IN RECENT BRIEFINGS", self.source)

    def test_the_live_context_is_declared_authoritative(self):
        self.assertIn("authoritative where the two disagree", self.source)

    def test_the_stored_ledger_carries_no_bare_day_words(self):
        import json
        path = os.path.join(self.root, "vault", "system",
                            "briefing_ledger.json")
        if not os.path.exists(path):
            self.skipTest("no briefing ledger on this machine")
        with open(path, encoding="utf-8") as handle:
            items = json.load(handle)
        offenders = [it.get("time") for it in items
                     if re.search(r"\b(tomorrow|yesterday)\b",
                                  it.get("text") or "", re.IGNORECASE)]
        self.assertEqual(offenders, [],
                         f"briefings still carrying a relative day: {offenders}")


if __name__ == "__main__":
    unittest.main()
