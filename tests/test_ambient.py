# -*- coding: utf-8 -*-
"""
Tests for the ambient context: where he is, the sky, and the headlines.

What this exists for: a flight from Chennai at 17:00 with rain forecast from
16:30. Both halves were knowable and neither was in front of the model, so it
could not connect them. Nothing here decides what they mean together -- no rule
says "warn about rain before a flight" -- because the collisions worth catching
are the ones nobody wrote a rule for. The job is to put the hourly forecast
beside the calendar and let the model reason.

The formatting and caching tests run offline. The live ones need network and
skip without it, which matters: this was written on a flaky hotspot and a test
that fails when the wifi drops teaches nothing.
"""

import os
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ambient

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

PLACE = {"city": "Chennai", "region": "Tamil Nadu", "country": "IN",
         "lat": 13.0878, "lon": 80.2785, "timezone": "Asia/Kolkata"}

# The real shape of the case: dry now, wet across the hour he must travel in.
SKY = {
    "now": {"temp": 35.0, "condition": "overcast", "wind": 10.0},
    "hourly": [
        {"time": "15:00", "temp": 34.0, "rain_chance": 52, "rain_mm": 0.1,
         "condition": "light drizzle"},
        {"time": "16:00", "temp": 32.0, "rain_chance": 57, "rain_mm": 0.0,
         "condition": "clear"},
        {"time": "17:00", "temp": 31.0, "rain_chance": 55, "rain_mm": 0.0,
         "condition": "clear"},
    ],
}

NEWS = [{"title": "Something happened in Chennai", "source": "The Hindu"},
        {"title": "Something else happened", "source": "NDTV"}]


class CacheTests(unittest.TestCase):

    def setUp(self):
        self.saved_location = mock.patch.object(
            ambient, "_saved_location", return_value=None)
        self.saved_location.start()
        ambient._cache.clear()
        ambient._cache.update({"location": (0.0, None), "weather": (0.0, None),
                               "news": (0.0, None)})

    def tearDown(self):
        self.saved_location.stop()

    def test_a_fresh_value_is_served_from_cache(self):
        ambient._store("location", PLACE)
        self.assertEqual(ambient._fresh("location", 3600), PLACE)

    def test_a_stale_value_is_not(self):
        ambient._cache["location"] = (time.time() - 10_000, PLACE)
        self.assertIsNone(ambient._fresh("location", 3600))

    def test_an_empty_cache_returns_nothing(self):
        self.assertIsNone(ambient._fresh("weather", 3600))

    def test_weather_expires_sooner_than_a_city(self):
        # A forecast goes stale; Chennai does not move.
        self.assertLess(ambient.WEATHER_TTL, ambient.LOCATION_TTL)


class ConditionTests(unittest.TestCase):

    def test_a_code_becomes_words(self):
        self.assertEqual(ambient._describe(0), "clear")
        self.assertEqual(ambient._describe(65), "heavy rain")
        self.assertEqual(ambient._describe(95), "thunderstorms")

    def test_an_unknown_code_is_admitted_not_invented(self):
        self.assertIn("code", ambient._describe(12345))

    def test_rubbish_does_not_raise(self):
        self.assertEqual(ambient._describe(None), "unknown")
        self.assertEqual(ambient._describe("x"), "unknown")


class ContextBlockTests(unittest.TestCase):
    """What the model actually reads."""

    def setUp(self):
        self.saved_location = mock.patch.object(
            ambient, "_saved_location", return_value=None)
        self.saved_location.start()
        ambient._cache.clear()
        ambient._store("location", PLACE)
        ambient._store("weather", SKY)
        ambient._store("news", NEWS)

    def tearDown(self):
        self.saved_location.stop()
        ambient._cache.clear()
        ambient._cache.update({"location": (0.0, None), "weather": (0.0, None),
                               "news": (0.0, None)})

    def test_the_place_is_named_with_coordinates(self):
        block = ambient.context_block()
        self.assertIn("Chennai", block)
        self.assertIn("Tamil Nadu", block)
        self.assertIn("13.088", block)

    def test_the_hourly_forecast_is_present_hour_by_hour(self):
        # A daily summary cannot answer whether to leave before four o'clock,
        # which is the only question rain ever raises.
        block = ambient.context_block()
        for hour in ("15:00", "16:00", "17:00"):
            self.assertIn(hour, block)

    def test_rain_probability_is_shown_per_hour(self):
        block = ambient.context_block()
        self.assertIn("rain 52%", block)
        self.assertIn("rain 57%", block)

    def test_current_conditions_are_shown(self):
        block = ambient.context_block()
        self.assertIn("35C", block)
        self.assertIn("overcast", block)

    def test_headlines_are_scoped_to_startup_or_relevance(self):
        # Otherwise the model treats them as the thing he asked about.
        block = ambient.context_block()
        self.assertIn("requested for startup briefings", block)
        self.assertIn("otherwise mention only when relevant", block)
        self.assertIn("The Hindu", block)

    def test_a_missing_piece_drops_its_section_rather_than_failing(self):
        # Caching an empty list is deliberately treated as a miss -- no news
        # is more likely a failed fetch than a quiet news day -- so the
        # snapshot is stubbed instead of poisoning the cache.
        original = ambient.snapshot
        ambient.snapshot = lambda force=False: {
            "location": PLACE, "weather": SKY, "news": []}
        try:
            block = ambient.context_block()
        finally:
            ambient.snapshot = original
        self.assertNotIn("HEADLINES", block)
        self.assertIn("WEATHER", block)

    def test_no_data_at_all_is_an_empty_block_not_an_error(self):
        ambient._cache.clear()
        ambient._cache.update({"location": (0.0, None),
                               "weather": (0.0, None), "news": (0.0, None)})
        try:
            block = ambient.context_block()
        except Exception as exc:
            self.fail(f"context_block raised: {exc}")
        self.assertIsInstance(block, str)


class WiringTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        with open(os.path.join(ROOT, "agent.py"), encoding="utf-8") as handle:
            cls.agent = handle.read()

    def test_the_block_sits_next_to_the_calendar(self):
        import agent

        with mock.patch(
                "providers.google_api.list_calendar_events_with_ids",
                return_value="- Lab today at 4 PM"), mock.patch.object(
                    agent, "_ambient_context",
                    return_value="--- WEATHER ---\nRain from 3 PM"):
            context = agent.gather_context(
                resources=("calendar", "environment"))
        calendar = context.index("CALENDAR: TODAY ONWARDS")
        weather = context.index("WEATHER")
        self.assertLess(calendar, weather,
                        "the forecast must follow the commitments it bears on")

    def test_the_helper_cannot_break_a_turn(self):
        start = self.agent.find("def _ambient_context()")
        body = self.agent[start:start + 400]
        self.assertIn("except Exception", body)

    def test_the_refresher_starts_at_boot(self):
        self.assertIn("ambient.start_refresher(", self.agent)

    def test_an_update_is_queued_not_spoken(self):
        # A weather change must not interrupt him; the proactive path decides.
        self.assertIn("CONDITIONS CHANGED", self.agent)
        self.assertIn("<DONE /> with no SAY", self.agent)

    def test_the_old_uncached_ipinfo_call_is_gone(self):
        self.assertNotIn("requests.get('https://ipinfo.io/json', timeout=2)",
                         self.agent)


class KnowledgeTests(unittest.TestCase):

    def test_headlines_go_to_knowledge_not_the_fact_graph(self):
        # A graph edge asserts something is true until invalidated. "Iran
        # proposes a deal" is true for an afternoon, which is the exact shape
        # of the bug where a note saying the exam was "tomorrow" was still
        # being repeated the next day.
        with open(os.path.join(ROOT, "ambient.py"), encoding="utf-8") as handle:
            source = handle.read()
        self.assertIn("save_knowledge", source)
        self.assertNotIn("add_edge", source)

    def test_filed_headlines_carry_their_date(self):
        with open(os.path.join(ROOT, "ambient.py"), encoding="utf-8") as handle:
            source = handle.read()
        self.assertIn("Reported on", source)


@unittest.skipUnless(os.getenv("WINTER_NETWORK_TESTS"),
                     "set WINTER_NETWORK_TESTS=1 to call the live services")
class LiveTests(unittest.TestCase):
    """The three free services, for real."""

    def test_location_resolves(self):
        place = ambient.location(force=True)
        if place is None:
            self.skipTest("no network")
        for key in ("city", "lat", "lon"):
            self.assertIn(key, place)

    def test_hourly_weather_returns_rows(self):
        sky = ambient.weather(13.0878, 80.2785, force=True)
        if sky is None:
            self.skipTest("no network")
        self.assertTrue(sky["hourly"])
        first = sky["hourly"][0]
        self.assertIn("rain_chance", first)
        self.assertRegex(first["time"], r"^\d{2}:\d{2}$")

    def test_headlines_return_titles(self):
        items = ambient.headlines(limit=3, force=True)
        if not items:
            self.skipTest("no network")
        self.assertTrue(all(item["title"] for item in items))


if __name__ == "__main__":
    unittest.main()
