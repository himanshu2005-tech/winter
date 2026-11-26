import unittest
from unittest import mock

import hud
import route_render


def line(offset, n=6):
    return {"type": "LineString",
            "coordinates": [[80.20 + i * 0.01, 13.00 + offset + i * 0.005]
                            for i in range(n)]}


ROUTE = {"ok": True, "duration_minutes": 30, "distance_km": 12.0,
         "geometry": line(0.0),
         "alternatives": [
             {"duration_minutes": 36, "distance_km": 13.5, "geometry": line(0.02)},
             {"duration_minutes": 40, "distance_km": 15.0, "geometry": {}}]}


class Placement(unittest.TestCase):
    def test_window_stays_on_screen_at_every_dpi_scale(self):
        for scale in (1.0, 1.25, 1.5, 2.0):
            w, h, x, y = hud.placement(1920, 1080, 500, scale)
            right = x + w * scale
            self.assertLessEqual(right + hud.MARGIN, 1920 + 1, scale)
            self.assertLessEqual(y + h * scale, 1080, scale)
            self.assertGreaterEqual(x, 0)

    def test_tall_content_is_fitted_to_the_screen(self):
        w, h, x, y = hud.placement(1366, 768, 5000, 1.25)
        self.assertLessEqual(y + h * 1.25, 768)


class MultiRoute(unittest.TestCase):
    def test_only_routes_with_geometry_are_drawn(self):
        self.assertEqual(len(route_render.all_routes(ROUTE)), 2)

    def test_routes_share_one_bounding_box(self):
        placed = route_render.project_all(route_render.all_routes(ROUTE), 300, 200)
        self.assertEqual(len(placed), 2)
        ys_a = [p[1] for p in placed[0]]
        ys_b = [p[1] for p in placed[1]]
        # B lies 0.02 degrees north of A, so it must be drawn higher up.
        self.assertLess(min(ys_b), min(ys_a))
        for pts in placed:
            for x, y in pts:
                self.assertTrue(0 <= x <= 300 and 0 <= y <= 200)

    def test_caption_names_every_route(self):
        text = route_render.describe_all(ROUTE)
        self.assertIn("A (recommended)", text)
        self.assertIn("B (alternative)", text)
        self.assertIn("36 min", text)

    def test_single_route_caption_is_unchanged(self):
        solo = {k: v for k, v in ROUTE.items() if k != "alternatives"}
        self.assertEqual(route_render.describe_all(solo), route_render.describe(solo))

    def test_static_map_url_carries_each_route(self):
        url = route_render.static_map_url(ROUTE, token="t")
        self.assertEqual(url.count("path-"), 2)

    def test_osrm_keeps_alternatives(self):
        from providers import maps_api
        rows = [{"duration": 1800, "distance": 12000, "geometry": line(0)},
                {"duration": 2100, "distance": 13000, "geometry": line(0.02)}]
        resp = mock.Mock()
        resp.json.return_value = {"routes": rows}
        with mock.patch.object(maps_api.requests, "get", return_value=resp):
            out = maps_api._osrm_route({"lon": 1, "lat": 1}, {"lon": 2, "lat": 2})
        self.assertEqual(len(out["alternatives"]), 1)
        self.assertEqual(out["alternatives"][0]["duration_minutes"], 35)


if __name__ == "__main__":
    unittest.main()
