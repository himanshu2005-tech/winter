# -*- coding: utf-8 -*-
"""
Tests for hailing a ride by voice.

Uber's ride-request API needs a registered app plus rider OAuth -- a stored
credential that can book rides on his account. Uber's own guidance sends new
integrations to deep links instead, and that is the better shape here anyway:

VANTA fills the form in, Himanshu taps Request.

A ride costs money and puts a car on a road. The Uber screen he confirms on
shows the price and the pickup pin, which is a better check than any gate VANTA
could offer, because it is shown by the app that will charge him. Nothing in
this module books anything.

Geocoding is Nominatim, the same service weather and travel time already use, so
the three cannot disagree about where a place is. The live tests need network
and skip without it.
"""

import os
import sys
import unittest
from urllib.parse import parse_qs, unquote, urlparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from providers.rides_api import _place, build_uber_link, prepare_ride

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

HOME = _place(13.0878, 80.2785, "Chennai")
AIRPORT = _place(12.9941, 80.1709, "Chennai Airport", "MAA, Meenambakkam")


class LinkTests(unittest.TestCase):

    def setUp(self):
        self.saved = os.environ.pop("UBER_CLIENT_ID", None)

    def tearDown(self):
        os.environ.pop("UBER_CLIENT_ID", None)
        if self.saved is not None:
            os.environ["UBER_CLIENT_ID"] = self.saved

    def test_it_points_at_uber(self):
        url = build_uber_link(HOME, AIRPORT)
        self.assertEqual(urlparse(url).netloc, "m.uber.com")

    def test_both_ends_of_the_trip_are_in_the_link(self):
        params = parse_qs(urlparse(build_uber_link(HOME, AIRPORT)).query)
        joined = unquote(str(params))
        self.assertIn("13.0878", joined)
        self.assertIn("80.2785", joined)
        self.assertIn("12.9941", joined)
        self.assertIn("80.1709", joined)

    def test_without_a_client_id_it_still_builds_a_usable_link(self):
        # Requiring a developer registration before he can be driven anywhere
        # would be a silly place to stop.
        url = build_uber_link(HOME, AIRPORT)
        self.assertIn("/ul/", url)
        self.assertIn("setPickup", url)

    def test_with_a_client_id_it_uses_the_current_endpoint(self):
        os.environ["UBER_CLIENT_ID"] = "test-client-id"
        url = build_uber_link(HOME, AIRPORT)
        self.assertIn("/looking", url)
        self.assertIn("test-client-id", unquote(url))

    def test_place_names_survive_into_the_link(self):
        joined = unquote(build_uber_link(HOME, AIRPORT))
        self.assertIn("Chennai Airport", joined)

    def test_coordinates_are_numbers_not_strings(self):
        place = _place("13.0878", "80.2785", "x")
        self.assertIsInstance(place["latitude"], float)
        self.assertIsInstance(place["longitude"], float)


class RefusalTests(unittest.TestCase):
    """It must decline rather than guess where he is going."""

    def test_no_destination_is_refused(self):
        url, message = prepare_ride("")
        self.assertIsNone(url)
        self.assertIn("needs a destination", message)
        self.assertIn("rather than guessing", message)

    def test_nothing_is_ever_described_as_booked(self):
        url, message = prepare_ride("")
        self.assertNotIn("booked", message.lower().replace("not been booked", ""))


class PolicyTests(unittest.TestCase):

    def test_it_is_registered_or_it_is_refused(self):
        import autonomy
        self.assertIn("BOOK_RIDE", autonomy.KNOWN_ACTIONS)

    def test_the_schema_knows_its_arguments(self):
        import action_schema
        spec = action_schema.ACTION_SPECS["BOOK_RIDE"]
        self.assertEqual(spec["requires"], ["destination"])
        self.assertIn("pickup", spec["optional"])

    def test_the_model_is_told_it_exists(self):
        import agent
        self.assertIn("BOOK_RIDE", agent.ACTION_CATALOGS["SYSTEM"])

    def test_the_catalogue_says_it_does_not_book(self):
        import agent
        entry = agent.ACTION_CATALOGS["SYSTEM"]
        self.assertIn("does NOT book", entry)

    def test_a_user_may_ask_for_one(self):
        import autonomy
        allowed, _ = autonomy.action_allowed("BOOK_RIDE", "user")
        self.assertTrue(allowed)

    def test_winter_may_not_open_uber_unasked(self):
        # It may say "rain from 16:30, leave earlier" on its own; launching the
        # app is his call. OPEN_URL is blocked proactively for the same reason.
        import autonomy
        allowed, why = autonomy.action_allowed("BOOK_RIDE", "proactive")
        self.assertFalse(allowed)
        self.assertIn("proactive", why.lower())


@unittest.skipUnless(os.getenv("WINTER_NETWORK_TESTS"),
                     "set WINTER_NETWORK_TESTS=1 to geocode for real")
class LiveTests(unittest.TestCase):

    def test_a_real_destination_resolves(self):
        url, message = prepare_ride("Chennai International Airport")
        if url is None and "could not" in message:
            self.skipTest("geocoder unreachable")
        self.assertIsNotNone(url)
        self.assertIn("m.uber.com", url)
        self.assertIn("Nothing has been booked", message)

    def test_a_nonsense_destination_is_refused(self):
        url, message = prepare_ride("zzqqxx not a real place at all")
        self.assertIsNone(url)
        self.assertIn("could not find", message)


if __name__ == "__main__":
    unittest.main()
