"""World Air Quality Index (WAQI/AQICN) provider."""
import os
import threading
import time
from urllib.parse import quote
from providers.http_client import get_json

_BASE = "https://api.waqi.info"
_cache = {}
_lock = threading.Lock()


def _cached(key, loader, ttl=600):
    now = time.monotonic()
    with _lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < ttl:
            return hit[1]
    value = loader()
    with _lock:
        _cache[key] = (now, value)
    return value


def get_air_quality(city="", lat="", lon=""):
    token = os.getenv("WAQI_API_TOKEN", "").strip()
    if not token:
        return "[Error] WAQI_API_TOKEN is not configured."
    city = str(city or "").strip()
    try:
        if lat != "" or lon != "":
            latitude, longitude = float(lat), float(lon)
            if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
                raise ValueError
            feed = f"geo:{latitude};{longitude}"
        elif city:
            feed = quote(city, safe="")
        else:
            return "[Error] Air-quality lookup needs a city or latitude and longitude."
    except (TypeError, ValueError):
        return "[Error] Latitude or longitude is invalid."

    try:
        payload = _cached(feed.casefold(), lambda: get_json(
            f"{_BASE}/feed/{feed}/", params={"token": token}, timeout=10))
    except RuntimeError as exc:
        return f"[Error] Air-quality service is unavailable: {exc}"
    if payload.get("status") != "ok" or not isinstance(payload.get("data"), dict):
        return f"[Error] WAQI could not find that location: {payload.get('data', 'unknown error')}"
    data = payload["data"]
    station = data.get("city") or {}
    iaqi = data.get("iaqi") or {}
    pollutants = []
    for name in ("pm25", "pm10", "o3", "no2", "so2", "co"):
        value = iaqi.get(name)
        if isinstance(value, dict) and value.get("v") is not None:
            pollutants.append(f"{name.upper()}: {value['v']}")
    geo = station.get("geo") or []
    lines = [
        f"WAQI AQI: {data.get('aqi', 'unavailable')}",
        f"Station: {station.get('name', 'unavailable')}",
        f"Measured: {(data.get('time') or {}).get('s', 'unavailable')}",
    ]
    if geo:
        lines.append(f"Station coordinates: {', '.join(map(str, geo))}")
    if pollutants:
        lines.append("Pollutants: " + ", ".join(pollutants))
    forecast = (data.get("forecast") or {}).get("daily") or {}
    available = [key.upper() for key, values in forecast.items() if values]
    if available:
        lines.append("Forecast available for: " + ", ".join(available))
    lines.append("Scale: WAQI/AQICN (not India's National AQI). This is a monitoring-station reading, not exact personal exposure.")
    return "\n".join(lines)
