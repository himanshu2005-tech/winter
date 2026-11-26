"""Mapbox-first location and routing for VANTA.

Mapbox supplies search, drawable GeoJSON routes, alternatives, traffic and
incidents. OpenStreetMap/OSRM is deliberately only the keyless outage fallback.
No Google Maps credential or endpoint is used here.
"""

from __future__ import annotations

import os
import re
from typing import Any

import requests


_TIMEOUT = 10
_USER_AGENT = "VANTA-Personal-Assistant/1.0"


def _mapbox_key() -> str:
    key = os.getenv("MAPBOX_ACCESS_TOKEN", "").strip()
    if key:
        return key
    try:
        from security.keyring_manager import get_secret_chunked
        return (get_secret_chunked("WINTER_SYSTEM", "mapbox_access_token")
                or "").strip()
    except Exception:
        return ""


def _osm_geocode(query: str) -> dict[str, Any] | None:
    try:
        response = requests.get(
            "https://nominatim.openstreetmap.org/search",
            params={"q": query, "format": "jsonv2", "limit": 1,
                    "addressdetails": 1, "countrycodes": "in"},
            headers={"User-Agent": _USER_AGENT}, timeout=_TIMEOUT)
        response.raise_for_status()
        rows = response.json()
        if not rows:
            return None
        row = rows[0]
        return {"lat": float(row["lat"]), "lon": float(row["lon"]),
                "display_name": row.get("display_name") or query,
                "source": "openstreetmap"}
    except (requests.RequestException, ValueError, KeyError, TypeError):
        return None


def geocode(location_query: str) -> dict[str, Any] | None:
    """Resolve an Indian place and reject superficially similar bad matches."""
    query = str(location_query or "").strip()
    if not query:
        return None
    key = _mapbox_key()
    mapbox_result = None
    if key:
        try:
            response = requests.get(
                "https://api.mapbox.com/search/geocode/v6/forward",
                params={"q": query, "country": "in", "limit": 1,
                        "language": "en", "access_token": key},
                timeout=_TIMEOUT)
            response.raise_for_status()
            features = response.json().get("features") or []
            if features:
                feature = features[0]
                coordinates = feature.get("geometry", {}).get("coordinates") or []
                props = feature.get("properties") or {}
                if len(coordinates) >= 2:
                    mapbox_result = {
                        "lat": float(coordinates[1]), "lon": float(coordinates[0]),
                        "display_name": (props.get("full_address") or
                                         feature.get("place_name") or
                                         props.get("name") or query),
                        "mapbox_id": props.get("mapbox_id") or feature.get("id"),
                        "source": "mapbox"}
        except (requests.RequestException, ValueError, KeyError, TypeError) as exc:
            print(f"[Maps] Mapbox geocoding unavailable: {type(exc).__name__}; using fallback")
    osm_result = _osm_geocode(query)
    if not mapbox_result:
        return osm_result
    if not osm_result:
        return mapbox_result

    # A provider may turn "International Airport" into a similarly named
    # neighbourhood. Prefer the candidate that actually contains more of the
    # user's meaningful words; provider order must not override correctness.
    ignored = {"the", "in", "at", "of", "and", "india"}
    wanted = {w for w in re.findall(r"[a-z0-9]+", query.lower())
              if len(w) > 2 and w not in ignored}
    def match_score(candidate):
        words = set(re.findall(r"[a-z0-9]+", candidate["display_name"].lower()))
        return len(wanted & words) / max(1, len(wanted))
    return osm_result if match_score(osm_result) > match_score(mapbox_result) else mapbox_result


def get_coordinates(location_query):
    result = geocode(location_query)
    return ((str(result["lat"]), str(result["lon"])) if result
            else (None, None))


def _route_summary(row: dict[str, Any]) -> dict[str, Any]:
    duration = float(row.get("duration") or 0)
    typical = float(row.get("duration_typical") or duration)
    incidents = []
    congestion = []
    for leg in row.get("legs") or []:
        incidents.extend(leg.get("incidents") or [])
        annotation = leg.get("annotation") or {}
        congestion.extend(annotation.get("congestion") or [])
    levels = {str(level).lower() for level in congestion if level}
    return {
        "duration_minutes": max(1, round(duration / 60)),
        "typical_duration_minutes": max(1, round(typical / 60)),
        "traffic_delay_minutes": max(0, round((duration - typical) / 60)),
        "distance_km": round(float(row.get("distance") or 0) / 1000, 1),
        "geometry": row.get("geometry"),
        "congestion_levels": sorted(levels),
        "congestion_levels_per_segment": congestion,
        "incidents": incidents,
    }


def _mapbox_route(start: dict[str, Any], end: dict[str, Any], mode: str,
                  traffic: bool) -> dict[str, Any] | None:
    key = _mapbox_key()
    if not key:
        return None
    profiles = {"driving": "driving-traffic" if traffic else "driving",
                "car": "driving-traffic" if traffic else "driving",
                "walking": "walking", "cycling": "cycling",
                "bicycling": "cycling"}
    profile = profiles.get(mode)
    if not profile:
        return None
    coordinates = f"{start['lon']},{start['lat']};{end['lon']},{end['lat']}"
    try:
        response = requests.get(
            f"https://api.mapbox.com/directions/v5/mapbox/{profile}/{coordinates}",
            params={"access_token": key, "alternatives": "true",
                    "geometries": "geojson", "overview": "full",
                    "steps": "true", "annotations": "congestion,duration,distance",
                    "notifications": "all"},
            timeout=_TIMEOUT)
        response.raise_for_status()
        rows = response.json().get("routes") or []
        if not rows:
            return None
        primary = _route_summary(rows[0])
        primary.update({"traffic_aware": profile == "driving-traffic",
                        "source": "mapbox", "alternatives": [
                            _route_summary(row) for row in rows[1:3]]})
        return primary
    except (requests.RequestException, ValueError, KeyError, TypeError) as exc:
        print(f"[Maps] Mapbox route unavailable: {type(exc).__name__}; using fallback")
        return None


def _osrm_route(start: dict[str, Any], end: dict[str, Any]) -> dict[str, Any]:
    try:
        response = requests.get(
            f"https://router.project-osrm.org/route/v1/driving/"
            f"{start['lon']},{start['lat']};{end['lon']},{end['lat']}",
            params={"overview": "full", "geometries": "geojson",
                    "alternatives": "true"},
            headers={"User-Agent": _USER_AGENT}, timeout=_TIMEOUT)
        response.raise_for_status()
        rows = response.json().get("routes") or []
        if not rows:
            return {"ok": False, "error": "No driving route was found."}
        row = rows[0]
        return {"ok": True, "duration_minutes": max(1, round(row["duration"] / 60)),
                "typical_duration_minutes": None, "distance_km": round(row["distance"] / 1000, 1),
                "traffic_delay_minutes": None, "traffic_aware": False,
                "geometry": row.get("geometry"), "incidents": [],
                "congestion_levels": [], "congestion_levels_per_segment": [],
                "alternatives": [
                    {"duration_minutes": max(1, round(alt["duration"] / 60)),
                     "distance_km": round(alt["distance"] / 1000, 1),
                     "geometry": alt.get("geometry"), "incidents": [],
                     "congestion_levels": [],
                     "congestion_levels_per_segment": []}
                    for alt in rows[1:3]],
                "source": "openstreetmap_osrm"}
    except (requests.RequestException, ValueError, KeyError, TypeError) as exc:
        return {"ok": False, "error": f"Routing service unavailable ({type(exc).__name__})."}


def route(origin: str, destination: str, mode: str = "driving",
          traffic: bool = True) -> dict[str, Any]:
    """Return route facts plus GeoJSON suitable for the VANTA map panel."""
    mode = str(mode or "driving").strip().lower().replace("-", "_")
    start, end = geocode(origin), geocode(destination)
    if not start:
        return {"ok": False, "error": f"Could not locate origin: {origin}"}
    if not end:
        return {"ok": False, "error": f"Could not locate destination: {destination}"}
    live = _mapbox_route(start, end, mode, traffic)
    if live:
        return {"ok": True, "origin": start, "destination": end,
                "mode": mode, **live}
    if mode not in {"driving", "car"}:
        return {"ok": False, "error": (
            f"Mapbox is unavailable and a transit provider is required to route {mode} reliably.")}
    fallback = _osrm_route(start, end)
    return {"origin": start, "destination": end, "mode": "driving", **fallback}


def get_travel_time(origin, destination, mode="driving", traffic=True):
    result = route(origin, destination, mode, traffic=traffic)
    if not result.get("ok"):
        return f"Error: {result.get('error', 'Route unavailable')}"
    if result["traffic_aware"]:
        delay = result.get("traffic_delay_minutes") or 0
        note = f" Live traffic adds about {delay} minutes." if delay else " Live traffic is included."
        incidents = len(result.get("incidents") or [])
        if incidents:
            note += f" {incidents} traffic incident{'s' if incidents != 1 else ''} reported."
    else:
        note = " This is an estimate without live traffic."
    return (f"Travel by {result['mode']} from {origin} to {destination} takes "
            f"approximately {result['duration_minutes']} minutes "
            f"({result['distance_km']} km).{note}")
