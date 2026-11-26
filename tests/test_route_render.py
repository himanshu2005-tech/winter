import math

import route_render as render


ROUTE = {"ok": True, "distance_km": 36.5, "duration_minutes": 50,
         "typical_duration_minutes": 56, "traffic_delay_minutes": 0,
         "congestion_levels": ["low", "moderate"],
         "geometry": {"type": "LineString", "coordinates": [
             [78.4294, 17.2403], [78.4350, 17.2800], [78.4400, 17.3300],
             [78.4480, 17.3900], [78.4867, 17.4399], [78.5010, 17.4450]]}}


def test_projection_preserves_mercator_aspect_and_bounds():
    points = render.coordinates_of(ROUTE)
    placed = render.project(points, 600, 360)
    xs, ys = [x for x, _ in placed], [y for _, y in placed]
    expected = ((render._mercator_y(17.4450) - render._mercator_y(17.2403)) /
                math.radians(78.5010 - 78.4294))
    actual = (max(ys) - min(ys)) / (max(xs) - min(xs))
    assert abs(actual - expected) < 0.05
    assert all(0 <= x <= 600 and 0 <= y <= 360 for x, y in placed)


def test_degenerate_and_dirty_geometry_never_crashes():
    samples = [{}, {"geometry": {"coordinates": [[78.4, 17.2]]}},
               {"geometry": {"coordinates": [[78.4, 17.2]] * 4}},
               {"geometry": {"coordinates": [["x", "y"], None, [78.4, 17.2]]}}]
    for sample in samples:
        placed = render.project(render.coordinates_of(sample), 300, 200)
        assert all(0 <= x <= 300 and 0 <= y <= 200 for x, y in placed)


def test_static_url_is_bounded_and_does_not_leak_without_token():
    assert render.static_map_url(ROUTE, token=None) is None
    long_route = {"geometry": {"coordinates": [
        [78 + i * .001, 17 + i * .001] for i in range(3000)]}}
    url = render.static_map_url(long_route, token="pk.TEST")
    assert "api.mapbox.com" in url and "path-" in url
    assert "pk.TEST" in url and len(url) < 2000


def test_caption_and_congestion_segments_are_human_readable():
    caption = render.describe(ROUTE)
    assert "36.5 km" in caption and "50 min" in caption
    assert "quicker than usual" in caption and "Â" not in caption
    placed = render.project(render.coordinates_of(ROUTE), 600, 360)
    segments = render.segments(placed, ["low", "moderate", "heavy"])
    assert len(segments) == len(placed) - 1
    assert {segment[2] for segment in segments[:3]} == {
        "#3b82f6", "#f59e0b", "#ef4444"}
