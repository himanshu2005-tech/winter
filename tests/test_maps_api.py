from unittest.mock import Mock, patch

from providers import maps_api


def _place(lat, lon, name):
    return {"lat": lat, "lon": lon, "display_name": name, "source": "openstreetmap"}


def test_keyless_route_uses_osrm_and_marks_no_live_traffic():
    response = Mock()
    response.raise_for_status.return_value = None
    response.json.return_value = {"routes": [{"duration": 3600, "distance": 42000}]}
    with patch.object(maps_api, "geocode", side_effect=[_place(1, 2, "A"), _place(3, 4, "B")]), \
         patch.object(maps_api, "_mapbox_route", return_value=None), \
         patch.object(maps_api.requests, "get", return_value=response):
        result = maps_api.route("A", "B")
    assert result["ok"] and result["duration_minutes"] == 60
    assert result["traffic_aware"] is False


def test_live_traffic_route_is_preferred_when_configured():
    live = {"duration_minutes": 54, "distance_km": 42,
            "traffic_delay_minutes": 8, "traffic_aware": True,
            "source": "mapbox"}
    with patch.object(maps_api, "geocode", side_effect=[_place(1, 2, "A"), _place(3, 4, "B")]), \
         patch.object(maps_api, "_mapbox_route", return_value=live):
        result = maps_api.route("A", "B")
    assert result["source"] == "mapbox"
    assert result["traffic_delay_minutes"] == 8


def test_transit_is_not_misrepresented_as_driving():
    with patch.object(maps_api, "geocode", side_effect=[_place(1, 2, "A"), _place(3, 4, "B")]), \
         patch.object(maps_api, "_mapbox_route", return_value=None):
        result = maps_api.route("A", "B", mode="transit")
    assert not result["ok"]
    assert "transit provider" in result["error"]


def test_same_interface_survives_mapbox_failure():
    response = Mock()
    response.raise_for_status.return_value = None
    response.json.return_value = {"routes": [{"duration": 600, "distance": 3000}]}
    with patch.object(maps_api, "geocode", side_effect=[_place(1, 2, "A"), _place(3, 4, "B")]), \
         patch.object(maps_api, "_mapbox_route", return_value=None), \
         patch.object(maps_api.requests, "get", return_value=response):
        text = maps_api.get_travel_time("A", "B")
    assert "10 minutes" in text and "without live traffic" in text
