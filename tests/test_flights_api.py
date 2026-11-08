import datetime as dt
import unittest
from unittest.mock import Mock, patch

from providers import flights_api
import watches


def _future(days=30):
    return (dt.date.today() + dt.timedelta(days=days)).isoformat()


def _payload():
    return {
        "best_flights": [{
            "price": 6123, "total_duration": 95,
            "flights": [{
                "airline": "Example Air", "flight_number": "EA 101",
                "departure_airport": {"id": "HYD", "time": "2026-11-10 08:00"},
                "arrival_airport": {"id": "MAA", "time": "2026-11-10 09:35"},
            }],
        }],
        "price_insights": {"price_level": "typical"},
    }


class FlightApiTests(unittest.TestCase):
    def test_search_normalises_compact_offers_and_caches(self):
        flights_api._cache.clear()
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = _payload()
        with patch.object(flights_api, "_key", return_value="test"), \
             patch.object(flights_api.requests, "get", return_value=response) as get:
            first = flights_api.search("hyd", "maa", _future())
            second = flights_api.search("HYD", "MAA", _future())
        self.assertEqual(first["lowest_price"], 6123)
        self.assertEqual(first["offers"][0]["flight_numbers"], ["EA 101"])
        self.assertTrue(second["cached"])
        self.assertEqual(get.call_count, 1)

    def test_search_rejects_ambiguous_city_and_past_date_without_spending_quota(self):
        with patch.object(flights_api.requests, "get") as get:
            with self.assertRaisesRegex(ValueError, "three-letter"):
                flights_api.search("Hyderabad", "MAA", _future())
            with self.assertRaisesRegex(ValueError, "past"):
                flights_api.search("HYD", "MAA", "2020-01-01")
        get.assert_not_called()


class Store:
    def __init__(self):
        self.kwargs = None

    def query(self, _cypher, **kwargs):
        self.kwargs = kwargs
        return [{"id": kwargs.get("id"), "created_at": kwargs.get("now")}]


class FlightWatchTests(unittest.TestCase):
    def test_flight_watch_is_typed_durable_and_quota_safe(self):
        store = Store()
        result = watches.create_flight("HYD", "DEL", _future(), "7000",
                                       interval="5m", store_obj=store)
        self.assertTrue(result.startswith("[Success]"))
        self.assertEqual(store.kwargs["kind"], "flight_price")
        self.assertEqual(store.kwargs["interval"], 6 * 60 * 60)
        self.assertIn('"origin":"HYD"', store.kwargs["source"])

    def test_flight_watch_observation_uses_structured_lowest_fare(self):
        watch = {"kind": "flight_price", "source": (
            '{"adults":1,"departure_date":"%s","destination":"DEL",'
            '"nonstop":false,"origin":"HYD","return_date":"","travel_class":1}' % _future())}
        with patch.object(flights_api, "search", return_value={"lowest_price": 5999}):
            observed = watches.observe(watch)
        self.assertEqual(observed["value"], 5999)
        self.assertEqual(observed["method"], "SerpApi Google Flights")
