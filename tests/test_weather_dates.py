import datetime
import unittest
from unittest import mock

from providers import weather_api

TODAY = datetime.date(2026, 10, 16)  # a Friday


class ResolveDay(unittest.TestCase):
    def test_weekday_names(self):
        self.assertEqual(weather_api.resolve_day("sunday", TODAY), datetime.date(2026, 10, 18))
        self.assertEqual(weather_api.resolve_day("this sunday", TODAY), datetime.date(2026, 10, 18))
        self.assertEqual(weather_api.resolve_day("friday", TODAY), TODAY)

    def test_next_weekday_is_a_week_later(self):
        self.assertEqual(weather_api.resolve_day("next sunday", TODAY), datetime.date(2026, 10, 25))

    def test_relative_words(self):
        self.assertEqual(weather_api.resolve_day("tomorrow", TODAY), datetime.date(2026, 10, 17))
        self.assertEqual(weather_api.resolve_day("in 3 days", TODAY), datetime.date(2026, 10, 19))
        self.assertEqual(weather_api.resolve_day("2026-10-20", TODAY), datetime.date(2026, 10, 20))

    def test_unknown_text(self):
        self.assertIsNone(weather_api.resolve_day("", TODAY))
        self.assertIsNone(weather_api.resolve_day("whenever", TODAY))

    def test_labels_carry_the_weekday(self):
        self.assertEqual(weather_api._day_label(datetime.date(2026, 10, 18), TODAY), "Sunday 18 Oct")
        self.assertTrue(weather_api._day_label(TODAY, TODAY).startswith("Today"))


class GetWeather(unittest.TestCase):
    def test_missing_location(self):
        self.assertIn("[Error]", weather_api.get_weather("", date="sunday"))

    def test_past_and_far_dates_are_refused_without_a_request(self):
        with mock.patch.object(weather_api.requests, "get") as get:
            self.assertIn("past", weather_api.get_weather("Chennai", date="yesterday"))
            self.assertIn("beyond", weather_api.get_weather("Chennai", date="2027-01-01"))
            get.assert_not_called()

    def test_named_day_returns_that_day_with_parts(self):
        today = datetime.date.today()
        target = today + datetime.timedelta(days=2)
        hours = [f"{target.isoformat()}T{h:02d}:00" for h in range(24)]
        payload = {"current": {}, "daily": {
            "time": [(today + datetime.timedelta(days=i)).isoformat() for i in range(3)],
            "weather_code": [0, 0, 61], "temperature_2m_max": [30, 30, 29],
            "temperature_2m_min": [24, 24, 25],
            "precipitation_probability_max": [10, 10, 70],
            "sunrise": ["", "", f"{target.isoformat()}T06:00"],
            "sunset": ["", "", f"{target.isoformat()}T18:00"]},
            "hourly": {"time": hours, "temperature_2m": [26] * 24,
                       "precipitation_probability": [60] * 24, "weather_code": [61] * 24}}
        resp = mock.Mock()
        resp.json.return_value = payload
        with mock.patch("providers.maps_api.get_coordinates", return_value=(13.0, 80.2)), \
                mock.patch.object(weather_api.requests, "get", return_value=resp):
            out = weather_api.get_weather("Chennai", date=target.isoformat())
        self.assertIn(target.strftime("%A"), out)
        self.assertIn("morning", out)
        self.assertIn("Sun 06:00 to 18:00", out)


if __name__ == "__main__":
    unittest.main()
