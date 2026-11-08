# -*- coding: utf-8 -*-
"""Noticing that a stated plan lands on something already booked.

"I'm going to study tomorrow morning", said by someone who has a film in the
calendar at 9am tomorrow, should get the film mentioned. It did not: the
calendar was read only when an action read it, so tomorrow's events were not
in front of the model at all when the sentence arrived. Tracked commitments
were in context, but nothing asked whether a stated plan collided with one.
The reply was "nice, what are you studying?" and the clash surfaced the next
morning.

The work splits across a line that matters:

    which window was meant, and what is in it   -> arithmetic, tested here
    whether that is a problem, and what to say   -> judgment, left to the model

So these tests check windows and overlaps exactly, and check that the note
handed to the model is phrased as a fact rather than as an instruction to warn
about films. A module that decided "the words study and film together mean
warn" would be a thousand rules that each cover one sentence.
"""

import datetime
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import schedule

NOW = datetime.datetime(2026, 10, 15, 20, 0)        # a Thursday evening
TOMORROW = (NOW + datetime.timedelta(days=1)).date().isoformat()


def event(what, start, end, all_day=False, source="calendar"):
    return {"what": what, "start": start, "end": end, "all_day": all_day,
            "source": source}


def movie(hour=9, minutes=150):
    start = f"{TOMORROW}T{hour:02d}:00:00+05:30"
    finish = (datetime.datetime.fromisoformat(f"{TOMORROW}T{hour:02d}:00:00")
              + datetime.timedelta(minutes=minutes))
    return event("Movie with Madhu", start,
                 finish.isoformat() + "+05:30")


class WhichWindowWasMeant(unittest.TestCase):
    def window(self, text):
        return schedule.window_for(text, now=NOW)

    def test_tomorrow_morning(self):
        found = self.window("i am going to study tomorrow morning")
        self.assertEqual(found.start, datetime.datetime(2026, 10, 16, 6, 0))
        self.assertEqual(found.end, datetime.datetime(2026, 10, 16, 12, 0))

    def test_the_shorthand_he_actually_writes(self):
        """"tmr" once dropped the day entirely and sent a Saturday reminder
        to an alarm that fired the next morning."""
        for word in ("tmr", "tmrw", "tomorow", "2moro", "tmw"):
            with self.subTest(word=word):
                found = self.window(f"studying {word} morning")
                self.assertIsNotNone(found, f"{word} was not read as a day")
                self.assertEqual(found.start.date(),
                                 datetime.date(2026, 10, 16))

    def test_tonight_is_not_said_twice(self):
        self.assertEqual(self.window("free tonight").said, "tonight")

    def test_an_explicit_clock_makes_a_window_around_it(self):
        found = self.window("remind me at 9 tomorrow")
        self.assertEqual(found.start, datetime.datetime(2026, 10, 16, 7, 30))
        self.assertEqual(found.end, datetime.datetime(2026, 10, 16, 10, 30))

    def test_pm_is_respected(self):
        found = self.window("call me at 9pm tomorrow")
        self.assertEqual(found.start.hour, 19)

    def test_a_weekday_means_the_next_one(self):
        found = self.window("meeting saturday afternoon")
        self.assertEqual(found.start.date(), datetime.date(2026, 10, 17))

    def test_the_same_weekday_means_next_week_not_today(self):
        """Said on a Thursday, "thursday" is not today."""
        found = self.window("lunch on thursday")
        self.assertEqual(found.start.date(), datetime.date(2026, 10, 22))

    def test_this_weekday_means_today(self):
        found = self.window("lunch this thursday")
        self.assertEqual(found.start.date(), datetime.date(2026, 10, 15))

    def test_a_sentence_with_no_time_is_not_a_window(self):
        """Otherwise every second turn carries a schedule nobody asked for."""
        for text in ("i should study more", "what can you do",
                     "tell madhu i'll be late", ""):
            with self.subTest(text=text):
                self.assertIsNone(self.window(text))

    def test_a_bare_number_is_not_a_clock(self):
        """"20 roles" and "5 files" are not nine o'clock."""
        for text in ("i applied to 20 roles", "there are 5 files",
                     "read chapter 9"):
            with self.subTest(text=text):
                found = self.window(text)
                self.assertTrue(found is None or "around" not in found.said,
                                f"{text!r} was read as a clock time")

    def test_in_the_morning_said_at_night_means_tomorrow(self):
        """Without this it resolved to a window that closed eight hours ago,
        found nothing in it, and said nothing -- the same as not looking."""
        found = self.window("i'll do it in the morning")
        self.assertEqual(found.start.date(), datetime.date(2026, 10, 16))

    def test_this_morning_said_at_night_still_means_this_morning(self):
        found = self.window("what did i do this morning")
        self.assertEqual(found.start.date(), datetime.date(2026, 10, 15))

    def test_a_window_still_open_is_not_rolled_forward(self):
        morning = datetime.datetime(2026, 10, 15, 7, 0)
        found = schedule.window_for("studying this morning", now=morning)
        self.assertEqual(found.start.date(), datetime.date(2026, 10, 15))

    def test_a_whole_day_reads_as_a_whole_day(self):
        """It used to print "Thu 15 Oct 00:00 to 00:00", which looks empty."""
        found = self.window("busy today")
        self.assertIn("16 Oct", found.describe())


class WhatIsAlreadyInIt(unittest.TestCase):
    def booked(self, text, events, commitments=()):
        with patch.object(schedule, "_calendar_events", return_value=events), \
                patch.object(schedule, "_commitment_items",
                             return_value=list(commitments)):
            window = schedule.window_for(text, now=NOW)
            return schedule.booked_in(".", window, now=NOW)

    def test_an_overlapping_event_is_found(self):
        found = self.booked("studying tomorrow morning", [movie()])
        self.assertEqual([row["what"] for row in found], ["Movie with Madhu"])

    def test_an_event_outside_the_window_is_not(self):
        found = self.booked("studying tomorrow morning", [movie(hour=19)])
        self.assertEqual(found, [])

    def test_an_event_that_finishes_before_the_window_opens_is_not(self):
        found = self.booked("studying tomorrow afternoon", [movie(hour=9)])
        self.assertEqual(found, [])

    def test_an_event_straddling_the_start_is_found(self):
        """A film from 11:00 to 13:30 does collide with the afternoon."""
        found = self.booked("studying tomorrow afternoon",
                            [movie(hour=11, minutes=150)])
        self.assertEqual(len(found), 1)

    def test_a_timezone_offset_does_not_break_the_comparison(self):
        """Comparing an offset-bearing ISO string against a local datetime by
        inference is the mistake when.py exists to prevent."""
        found = self.booked(
            "studying tomorrow morning",
            [event("Standup", f"{TOMORROW}T09:00:00+05:30",
                   f"{TOMORROW}T09:15:00+05:30")])
        self.assertEqual(len(found), 1)

    def test_an_all_day_event_covers_its_whole_day(self):
        """It parses to midnight; taken literally it would clash with nothing."""
        found = self.booked("studying tomorrow morning",
                            [event("Diwali", TOMORROW, TOMORROW,
                                   all_day=True)])
        self.assertEqual(len(found), 1)

    def test_a_zero_length_entry_still_occupies_a_moment(self):
        found = self.booked(
            "studying tomorrow morning",
            [event("Ping", f"{TOMORROW}T09:00:00+05:30",
                   f"{TOMORROW}T09:00:00+05:30")])
        self.assertEqual(len(found), 1)

    def test_commitments_count_as_well_as_the_calendar(self):
        found = self.booked(
            "studying tomorrow morning", [],
            commitments=[event("Submit the TQM assignment",
                               f"{TOMORROW}T10:00:00+05:30",
                               f"{TOMORROW}T10:00:00+05:30",
                               source="commitment")])
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["source"], "commitment")

    def test_entries_come_back_in_time_order(self):
        found = self.booked("busy tomorrow",
                            [movie(hour=18), movie(hour=9)])
        self.assertLess(found[0]["parsed_start"], found[1]["parsed_start"])

    def test_an_unparseable_time_is_skipped_not_fatal(self):
        found = self.booked("studying tomorrow morning",
                            [event("Broken", "not a date", "also not"),
                             movie()])
        self.assertEqual(len(found), 1)

    def test_no_window_means_nothing_is_looked_up(self):
        self.assertEqual(schedule.booked_in(".", None), [])


class WhatTheModelIsTold(unittest.TestCase):
    def note(self, text, events, commitments=()):
        with patch.object(schedule, "_calendar_events", return_value=events), \
                patch.object(schedule, "_commitment_items",
                             return_value=list(commitments)):
            return schedule.conflict_note(".", text, now=NOW)

    def test_the_film_is_named_with_its_time(self):
        note = self.note("i am going to study tmr morning", [movie()])
        self.assertIn("Movie with Madhu", note)
        self.assertIn("09:00", note)

    def test_the_window_he_referred_to_is_named_back(self):
        """So a wrong reading is visible rather than mysterious."""
        note = self.note("i am going to study tmr morning", [movie()])
        self.assertIn("tmr morning", note)

    def test_nothing_booked_means_nothing_said(self):
        self.assertEqual(self.note("studying tmr morning", []), "")

    def test_no_time_mentioned_means_nothing_said(self):
        """And, importantly, no calendar read at all."""
        with patch.object(schedule, "_calendar_events") as read:
            self.assertEqual(schedule.conflict_note(".", "i should study more",
                                                    now=NOW), "")
        read.assert_not_called()

    def test_it_is_phrased_as_a_fact_not_an_instruction_to_warn(self):
        """The judgment is the model's. A rule here would be a thousand rules."""
        note = self.note("studying tmr morning", [movie()])
        self.assertIn("not an instruction about what to do with it", note)

    def test_it_says_when_not_to_mention_something(self):
        """A film during revision is worth raising. A water reminder is not."""
        note = self.note("studying tmr morning", [movie()])
        self.assertIn("do not mention it", note)

    def test_it_contains_no_rule_about_particular_activities(self):
        import inspect
        source = inspect.getsource(schedule)
        for word in ("movie", "film", "study", "revision", "cinema"):
            with self.subTest(word=word):
                self.assertNotIn(
                    word, source.lower().split('"""')[-1],
                    f"{word!r} is hard-coded outside the docstrings")


class TheCalendarReadIsCheap(unittest.TestCase):
    def setUp(self):
        schedule.invalidate_cache()
        self.addCleanup(schedule.invalidate_cache)

    def test_a_second_read_is_served_from_the_cache(self):
        """A conversation must not become one API call per sentence."""
        calls = []

        def fake(*args, **kwargs):
            calls.append(1)
            return []

        with patch("providers.google_api.get_credentials", fake):
            schedule._calendar_events(".")
            schedule._calendar_events(".")
        self.assertEqual(len(calls), 1)

    def test_a_failure_is_not_fatal(self):
        """A turn where Google is slow or signed out must answer normally."""
        with patch("providers.google_api.get_credentials",
                   side_effect=RuntimeError("signed out")):
            self.assertEqual(schedule._calendar_events("."), [])

    def test_the_cache_can_be_dropped(self):
        schedule._CACHE["events"] = [movie()]
        schedule.invalidate_cache()
        self.assertIsNone(schedule._CACHE["events"])

    def test_changing_the_calendar_drops_it(self):
        """Otherwise it warns about an event VANTA has just deleted."""
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "actions.py"), encoding="utf-8") as handle:
            source = handle.read()
        self.assertEqual(source.count("_schedule.invalidate_cache()"), 3,
                         "a calendar mutation does not drop the cache")


class WiredIntoTheTurn(unittest.TestCase):
    ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    def setUp(self):
        with open(os.path.join(self.ROOT, "agent.py"),
                  encoding="utf-8") as handle:
            self.agent = handle.read()

    def test_the_check_runs_on_a_turn(self):
        self.assertIn("schedule.conflict_note", self.agent)

    def test_only_for_something_he_said(self):
        """A background turn is not a plan being stated."""
        start = self.agent.index("schedule.conflict_note")
        self.assertIn('origin == "user"', self.agent[start - 400:start])

    def test_a_failure_does_not_break_the_turn(self):
        start = self.agent.index("schedule.conflict_note")
        self.assertIn("except Exception", self.agent[start:start + 300])


if __name__ == "__main__":
    unittest.main()
