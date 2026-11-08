# -*- coding: utf-8 -*-
"""
route_render.py -- turning route GeoJSON into something drawable.

providers/maps_api.route() already returns a full GeoJSON LineString with
traffic-aware durations and per-segment congestion. Nothing could show it: the
geometry was fetched, summarised into "36.5 km, 50 minutes" and then discarded,
so the most useful part of a route -- its shape -- never reached the screen.

This file does the arithmetic and none of the drawing. The projection, the
padding, the aspect ratio and the congestion colouring are all decisions that
can be wrong in ways a person would notice immediately and a test can catch
without a display: a route squashed into the wrong aspect, a line drawn off the
edge of its box, a single-point route dividing by zero. Keeping them here means
they are checked. The two renderers that consume this -- the Control Center
panel and the on-screen overlay -- then only place lines where they are told.

WHY MERCATOR AND NOT A FLAT SCALE
---------------------------------
Latitude degrees are a constant distance apart; longitude degrees narrow toward
the poles. Treating both as linear stretches a route east-west -- at Hyderabad's
latitude by about 8%, which is enough to make a familiar road look wrong. The
Web Mercator y transform is three lines and removes the question.
"""

import math
from urllib.parse import quote

#: Mapbox's own congestion vocabulary, and a colour per level. Low is not
#: coloured green: a route that is fine should read as neutral, so the eye goes
#: to the parts that are not.
CONGESTION_COLORS = {
    "low": "#3b82f6",
    "moderate": "#f59e0b",
    "heavy": "#ef4444",
    "severe": "#b91c1c",
    "unknown": "#6b7280",
}
DEFAULT_COLOR = "#3b82f6"

#: One colour per route when several are shown. The first is the recommended
#: route and keeps its congestion colouring; the rest take these.
ROUTE_COLORS = ("#3b82f6", "#a855f7", "#14b8a6", "#ec4899")


def coordinates_of(route):
    """The [lon, lat] pairs from a route dict, or an empty list.

    Accepts the whole route, its geometry, or a bare coordinate list, because
    callers reasonably hold any of the three and guessing wrong is silent.
    """
    if not route:
        return []
    if isinstance(route, list):
        pairs = route
    elif isinstance(route, dict):
        geometry = route.get("geometry") if "geometry" in route else route
        if isinstance(geometry, dict):
            pairs = geometry.get("coordinates") or []
        elif isinstance(geometry, list):
            pairs = geometry
        else:
            pairs = []
    else:
        return []
    clean = []
    for pair in pairs:
        if (isinstance(pair, (list, tuple)) and len(pair) >= 2):
            try:
                lon, lat = float(pair[0]), float(pair[1])
            except (TypeError, ValueError):
                continue
            # Mapbox and OSRM both emit [lon, lat]. A swapped pair would put an
            # Indian route in the Indian Ocean, so the obviously impossible is
            # dropped rather than drawn.
            if -180 <= lon <= 180 and -90 <= lat <= 90:
                clean.append((lon, lat))
    return clean


def _mercator_y(lat):
    """Web Mercator y for a latitude, clamped away from the poles."""
    bounded = max(-85.05112878, min(85.05112878, lat))
    radians = math.radians(bounded)
    return math.log(math.tan(math.pi / 4 + radians / 2))


def bounds(points):
    """(min_lon, min_lat, max_lon, max_lat) for a list of (lon, lat)."""
    if not points:
        return None
    lons = [point[0] for point in points]
    lats = [point[1] for point in points]
    return (min(lons), min(lats), max(lons), max(lats))


def project(points, width, height, padding=18):
    """Pixel coordinates for a route inside a width x height box.

    Aspect ratio is preserved and the result is centred, so a long east-west
    route does not get stretched vertically to fill the box -- which would make
    a straight highway look like a detour.
    """
    cleaned = [(float(lon), float(lat)) for lon, lat in points]
    if not cleaned:
        return []
    inner_w = max(1.0, float(width) - 2 * padding)
    inner_h = max(1.0, float(height) - 2 * padding)

    # Both axes in radians. Longitude was taken in DEGREES while the Mercator
    # y transform returns natural-log (radian-scale) units, so x came out about
    # 57 times too large relative to y: a route 3x taller than it is wide
    # projected to 564px across and 29px down. Preserving the aspect ratio is
    # the entire point of this function, and mixing the units defeated it while
    # looking like it worked.
    xs = [math.radians(lon) for lon, _lat in cleaned]
    ys = [_mercator_y(lat) for _lon, lat in cleaned]
    span_x = max(xs) - min(xs)
    span_y = max(ys) - min(ys)

    if span_x <= 0 and span_y <= 0:
        # A single point, or every point identical. Centre it rather than
        # dividing by zero.
        return [(padding + inner_w / 2, padding + inner_h / 2)] * len(cleaned)

    # One scale for both axes is what keeps the shape honest.
    scale = min(inner_w / span_x if span_x > 0 else float("inf"),
                inner_h / span_y if span_y > 0 else float("inf"))
    used_w = span_x * scale
    used_h = span_y * scale
    offset_x = padding + (inner_w - used_w) / 2
    offset_y = padding + (inner_h - used_h) / 2

    placed = []
    for lon, lat in cleaned:
        x = offset_x + (math.radians(lon) - min(xs)) * scale
        # Screen y grows downward; Mercator y grows north.
        y = offset_y + (max(ys) - _mercator_y(lat)) * scale
        placed.append((x, y))
    return placed


def segments(points, congestion=None):
    """Consecutive point pairs with a colour each.

    Mapbox gives one congestion value per segment, so there is one fewer value
    than there are points. Zipping them naively silently drops the last
    segment or reads past the end depending on which way round it is done, so
    the mapping is explicit and short lists fall back to the default colour
    rather than to whatever happened to be last.
    """
    levels = [str(level).lower() for level in (congestion or [])]
    pairs = []
    for index in range(len(points) - 1):
        level = levels[index] if index < len(levels) else None
        colour = CONGESTION_COLORS.get(level, DEFAULT_COLOR)
        pairs.append((points[index], points[index + 1], colour))
    return pairs


def static_map_url(route, width=600, height=360, token=None):
    """A Mapbox Static Images URL with the route drawn on it, or None.

    A real map behind the line is worth a request: a polyline alone says the
    shape of the journey but not where it is. Returns None without a token so
    the caller falls back to drawing the line on its own.
    """
    points = coordinates_of(route)
    if not points or not token:
        return None
    # The path is simplified before encoding: a full-overview Mapbox route can
    # carry a few thousand points, and a URL that long is rejected.
    simplified = simplify(points, 90)
    encoded = quote(_encode_polyline(simplified), safe="")
    overlays = []
    for index, item in enumerate(all_routes(route)):
        item_points = coordinates_of(item)
        if len(item_points) < 2:
            continue
        # The URL budget is split across routes; Mapbox rejects long ones.
        piece = quote(_encode_polyline(simplify(item_points, 60)), safe="")
        colour = ROUTE_COLORS[index % len(ROUTE_COLORS)].lstrip("#")
        overlays.append(f"path-{5 if index == 0 else 4}+{colour}-0.9({piece})")
    # Alternatives go first so the recommended route is drawn on top.
    overlay = ",".join(reversed(overlays)) if overlays \
        else f"path-4+3b82f6-0.9({encoded})"
    size = f"{int(width)}x{int(height)}"
    return ("https://api.mapbox.com/styles/v1/mapbox/dark-v11/static/"
            f"{overlay}/auto/{size}@2x?access_token={token}&padding=30")


def simplify(points, limit):
    """At most `limit` points, evenly sampled, keeping both ends.

    Nearest-neighbour sampling rather than Douglas-Peucker: the purpose is a
    URL short enough to be accepted and a line cheap enough to draw, not a
    cartographically faithful reduction.
    """
    total = len(points)
    if limit <= 2 or total <= limit:
        return list(points)
    step = (total - 1) / float(limit - 1)
    picked = [points[min(total - 1, int(round(index * step)))]
              for index in range(limit)]
    # Sampling can repeat a point on short routes; duplicates draw nothing.
    deduped = [picked[0]]
    for point in picked[1:]:
        if point != deduped[-1]:
            deduped.append(point)
    return deduped


def _encode_polyline(points, precision=5):
    """Google/Mapbox encoded polyline for (lon, lat) pairs."""
    factor = 10 ** precision
    output = []
    previous_lat = previous_lon = 0
    for lon, lat in points:
        lat_value = int(round(lat * factor))
        lon_value = int(round(lon * factor))
        output.append(_encode_value(lat_value - previous_lat))
        output.append(_encode_value(lon_value - previous_lon))
        previous_lat, previous_lon = lat_value, lon_value
    return "".join(output)


def _encode_value(value):
    value = ~(value << 1) if value < 0 else (value << 1)
    chunks = []
    while value >= 0x20:
        chunks.append(chr((0x20 | (value & 0x1f)) + 63))
        value >>= 5
    chunks.append(chr(value + 63))
    return "".join(chunks)


def describe(route):
    """The one-line caption that goes under the drawing."""
    if not isinstance(route, dict):
        return ""
    parts = []
    distance = route.get("distance_km")
    if distance:
        parts.append(f"{distance} km")
    minutes = route.get("duration_minutes")
    if minutes:
        parts.append(f"{minutes} min")
    delay = route.get("traffic_delay_minutes") or 0
    if delay > 0:
        parts.append(f"{delay} min of traffic")
    elif route.get("typical_duration_minutes") and minutes:
        if minutes < route["typical_duration_minutes"]:
            parts.append("quicker than usual")
    levels = [level for level in (route.get("congestion_levels") or [])
              if level and level != "unknown"]
    if levels:
        parts.append("congestion " + "/".join(levels))
    return " · ".join(parts)


def route_label(index):
    """A, B, C... for the index-th route."""
    return chr(ord("A") + int(index))


def route_color(index):
    return ROUTE_COLORS[int(index) % len(ROUTE_COLORS)]


def all_routes(route):
    """The recommended route followed by every alternative with geometry."""
    if not isinstance(route, dict):
        return []
    routes = [route]
    for alternative in route.get("alternatives") or []:
        if isinstance(alternative, dict) and len(coordinates_of(alternative)) >= 2:
            routes.append(alternative)
    return routes


def project_all(routes, width, height, padding=18):
    """Project several routes into one box with a SHARED bounding box.

    Projecting each on its own would stretch every route to fill the box, so
    two roads of different length would look the same size and sit in the
    wrong places relative to each other.
    """
    point_lists = [coordinates_of(item) for item in routes]
    flat = [point for points in point_lists for point in points]
    placed = project(flat, width, height, padding)
    out, cursor = [], 0
    for points in point_lists:
        out.append(placed[cursor:cursor + len(points)])
        cursor += len(points)
    return out


def label_anchor(placed):
    """Where to put a route's tag: the midpoint of its drawn line."""
    return placed[len(placed) // 2]


def describe_all(route):
    """Caption for every route shown; the plain caption when there is one."""
    routes = all_routes(route)
    if len(routes) <= 1:
        return describe(route)
    lines = []
    for index, item in enumerate(routes):
        kind = "recommended" if index == 0 else "alternative"
        lines.append(f"{route_label(index)} ({kind}): {describe(item)}")
    return "\n".join(lines)
