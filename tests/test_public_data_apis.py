import os
import unittest
from unittest.mock import Mock, patch

from providers import air_quality_api, india_post_api


class PublicApiTests(unittest.TestCase):
    def setUp(self):
        air_quality_api._cache.clear()
        india_post_api._cache.clear()

    @patch("providers.http_client.requests.get")
    def test_waqi_city_and_scale(self, get):
        response = Mock(status_code=200, headers={})
        response.raise_for_status.return_value = None
        response.json.return_value = {"status": "ok", "data": {
            "aqi": 82, "city": {"name": "Chennai US Consulate", "geo": [13.0, 80.2]},
            "time": {"s": "2026-10-18 10:00:00"},
            "iaqi": {"pm25": {"v": 82}, "pm10": {"v": 44}}}}
        get.return_value = response
        with patch.dict(os.environ, {"WAQI_API_TOKEN": "secret"}):
            result = air_quality_api.get_air_quality(city="Chennai")
        self.assertIn("WAQI AQI: 82", result)
        self.assertIn("not India's National AQI", result)
        self.assertNotIn("secret", result)

    def test_waqi_requires_token(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertIn("WAQI_API_TOKEN", air_quality_api.get_air_quality(city="Chennai"))

    def test_pin_validation(self):
        self.assertIn("six digits", india_post_api.lookup(pin="123"))

    @patch("providers.http_client.requests.get")
    def test_post_office_multiple_matches(self, get):
        response = Mock(status_code=200, headers={})
        response.raise_for_status.return_value = None
        response.json.return_value = [{"Status": "Success", "PostOffice": [
            {"Name": "Madhapur", "Pincode": "500081", "District": "Hyderabad",
             "State": "Telangana", "DeliveryStatus": "Delivery", "BranchType": "S.O"},
            {"Name": "Cyberabad", "Pincode": "500081", "District": "Hyderabad",
             "State": "Telangana", "DeliveryStatus": "Non-Delivery", "BranchType": "B.O"}]}]
        get.return_value = response
        result = india_post_api.lookup(pin="500081")
        self.assertIn("2 post-office match(es)", result)
        self.assertIn("Madhapur", result)


if __name__ == "__main__":
    unittest.main()
