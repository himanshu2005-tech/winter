# -*- coding: utf-8 -*-
"""
ambient.py -- where Himanshu is, what the sky is doing, and what happened today.

The startup context used to carry the clock, the battery and the calendar, which
is enough to answer "what is next" and not enough to answer "should I leave
earlier". The gap shows up in a case like this one: a flight from Chennai at
17:00 and rain forecast from 16:30. Both halves were knowable and neither was in
front of the model, so it could not connect them.

So this module puts the halves in the same place. It does not decide what they
mean together -- no rule here says "warn about rain before a flight", because
the interesting cases are the ones nobody wrote a rule for. It supplies the
hourly forecast next to the calendar and lets the model reason.

Everything here is free and keyless, which is deliberate: a key is a thing to
leak, rotate and exhaust, and the moment it fails is the moment he needed it.

    location   ipinfo.io        no key, city/region/lat/lon/timezone
    weather    Open-Meteo       no key, no quota, hourly precipitation
    news       Google News RSS  no key, regional edition

Refreshed on a timer as well as at startup, because the weather at boot is not
the weather five hours later, and a briefing built once is stale by lunchtime.
"""

import json
import os
import subprocess
import threading
import time
import xml.etree.ElementTree as ET

# How long each kind of answer stays useful. Weather moves; a city does not.
LOCATION_TTL = 6 * 3600
WEATHER_TTL = 20 * 60
NEWS_TTL = 45 * 60

# The background refresh interval. Short enough that a forecast is current,
# long enough that three free services are not being leaned on.
REFRESH_SECONDS = 20 * 60

_HTTP_TIMEOUT = 6
_lock = threading.RLock()
_cache = {
    "location": (0.0, None),
    "weather": (0.0, None),
    "news": (0.0, None),
}

# WMO codes, same vocabulary as providers/weather_api so the two cannot
# describe the same sky differently.
_CONDITIONS = {
    0: "clear", 1: "mostly clear", 2: "partly cloudy", 3: "overcast",
    45: "foggy", 48: "freezing fog",
    51: "light drizzle", 53: "drizzle", 55: "heavy drizzle",
    61: "light rain", 63: "rain", 65: "heavy rain",
    66: "freezing rain", 67: "heavy freezing rain",
    71: "light snow", 73: "snow", 75: "heavy snow", 77: "snow grains",
    80: "light showers", 81: "showers", 82: "violent showers",
    85: "snow showers", 86: "heavy snow showers",
    95: "thunderstorms", 96: "thunderstorms with hail",
    99: "severe thunderstorms with hail",
}


def _saved_location():
    """Return the explicit location for the current Wi-Fi, if configured.

    An IP address locates an ISP exit point, not a person.  A location the
    user explicitly associated with the current network therefore has higher
    authority and must be consulted before either the cache or IP lookup.
    """
    try:
        output = subprocess.check_output(
            "netsh wlan show interfaces", shell=True, text=True,
            encoding="utf-8", errors="replace", timeout=3)
        ssid = ""
        for line in output.splitlines():
            if " SSID " in line and ":" in line:
                ssid = line.split(":", 1)[1].strip()
                break
        if not ssid:
            return None
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "vault", "system", "saved_locations.json")
        with open(path, "r", encoding="utf-8") as handle:
            item = (json.load(handle) or {}).get(ssid) or {}
        lat, lon = float(item["lat"]), float(item["lon"])
        return {
            "city": item.get("city") or item.get("address") or "Unknown",
            "region": item.get("region") or "",
            "country": item.get("country") or "IN",
            "lat": lat,
            "lon": lon,
            "timezone": item.get("timezone") or "Asia/Kolkata",
            "source": "explicit_wifi_location",
            "address": item.get("address") or "",
        }
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError,
            subprocess.SubprocessError):
        return None


def _describe(code):
    try:
        return _CONDITIONS.get(int(code), f"code {code}")
    except (TypeError, ValueError):
        return "unknown"


def _fresh(key, ttl):
    """The cached value if it is still young enough, else None."""
    with _lock:
        stamp, value = _cache.get(key, (0.0, None))
    if value is not None and time.time() - stamp < ttl:
        return value
    return None


def _store(key, value):
    with _lock:
        _cache[key] = (time.time(), value)
    return value


# --------------------------------------------------------------------- #
# Where he is
# --------------------------------------------------------------------- #

def location(force=False):
    """City, region, coordinates and timezone, or None.

    IP geolocation, so it is accurate to the city and not to the street. That
    is the right resolution for this: the question being answered is "what is
    the weather where he is", not "which building".
    """
    saved = _saved_location()
    if saved:
        return _store("location", saved)
    if not force:
        cached = _fresh("location", LOCATION_TTL)
        if cached:
            return cached
    try:
        import requests
        data = requests.get("https://ipinfo.io/json",
                            timeout=_HTTP_TIMEOUT).json()
        raw = str(data.get("loc") or "")
        lat, _, lon = raw.partition(",")
        if not lat or not lon:
            return None
        return _store("location", {
            "city": data.get("city") or "Unknown",
            "region": data.get("region") or "",
            "country": data.get("country") or "",
            "lat": float(lat),
            "lon": float(lon),
            "timezone": data.get("timezone") or "",
        })
    except Exception:
        # A stale city is far better than no city; only a cold cache returns
        # None, and the caller degrades rather than failing.
        with _lock:
            return _cache.get("location", (0.0, None))[1]


# --------------------------------------------------------------------- #
# What the sky is doing
# --------------------------------------------------------------------- #

def weather(lat, lon, hours=12, force=False):
    """Current conditions plus an hour-by-hour outlook.

    The hourly rows are the point. A daily summary saying "rain" cannot tell
    him whether to leave before or after four o'clock, and that is the only
    question that ever matters about rain.
    """
    if not force:
        cached = _fresh("weather", WEATHER_TTL)
        if cached:
            return cached
    try:
        import requests
        data = requests.get(
            "https://api.open-meteo.com/v1/forecast",
            timeout=_HTTP_TIMEOUT,
            params={
                "latitude": lat, "longitude": lon,
                "current": "temperature_2m,weather_code,wind_speed_10m",
                "hourly": ("temperature_2m,precipitation_probability,"
                           "precipitation,weather_code"),
                "forecast_days": 2, "timezone": "auto",
            }).json()

        current = data.get("current") or {}
        hourly = data.get("hourly") or {}
        times = hourly.get("time") or []

        # Open-Meteo returns from midnight, so find where now is and read
        # forward. Rows already past are noise in a briefing.
        now_iso = str(current.get("time") or "")
        start = 0
        for index, stamp in enumerate(times):
            if stamp >= now_iso:
                start = index
                break

        rows = []
        for index in range(start, min(start + hours, len(times))):
            rows.append({
                "time": times[index][11:16],
                "temp": (hourly.get("temperature_2m") or [None])[index],
                "rain_chance": (hourly.get("precipitation_probability")
                                or [None])[index],
                "rain_mm": (hourly.get("precipitation") or [None])[index],
                "condition": _describe((hourly.get("weather_code")
                                        or [0])[index]),
            })

        return _store("weather", {
            "now": {
                "temp": current.get("temperature_2m"),
                "condition": _describe(current.get("weather_code", 0)),
                "wind": current.get("wind_speed_10m"),
            },
            "hourly": rows,
        })
    except Exception:
        with _lock:
            return _cache.get("weather", (0.0, None))[1]


# --------------------------------------------------------------------- #
# What happened today
# --------------------------------------------------------------------- #

def headlines(limit=6, region="IN", language="en", force=False):
    """Top headlines from the Google News RSS edition for his region."""
    if not force:
        cached = _fresh("news", NEWS_TTL)
        if cached:
            return cached
    try:
        import requests
        url = (f"https://news.google.com/rss?hl={language}-{region}"
               f"&gl={region}&ceid={region}:{language}")
        response = requests.get(
            url, timeout=_HTTP_TIMEOUT,
            headers={"User-Agent": "Mozilla/5.0 (VANTA)"})
        root = ET.fromstring(response.content)
        items = []
        for item in root.findall(".//item")[:limit]:
            title = (item.findtext("title") or "").strip()
            if not title:
                continue
            # The RSS title carries " - Publisher"; the publisher is not news.
            headline, _, source = title.rpartition(" - ")
            items.append({"title": (headline or title)[:160],
                          "source": source[:40] if headline else ""})
        return _store("news", items)
    except Exception:
        with _lock:
            return _cache.get("news", (0.0, None))[1] or []


# --------------------------------------------------------------------- #
# All of it, as the model sees it
# --------------------------------------------------------------------- #

def snapshot(force=False):
    place = location(force=force)
    sky = weather(place["lat"], place["lon"], force=force) if place else None
    return {"location": place, "weather": sky, "news": headlines(force=force)}


def context_block(force=False):
    """The ambient section of the turn context. Never raises."""
    try:
        snap = snapshot(force=force)
    except Exception:
        return ""

    lines = []
    place = snap.get("location")
    if place:
        where = ", ".join(part for part in (place["city"], place["region"],
                                           place["country"]) if part)
        lines.append(f"--- WHERE HE IS ---\n{where} "
                     f"({place['lat']:.3f}, {place['lon']:.3f})"
                     + (f", timezone {place['timezone']}"
                        if place["timezone"] else ""))

    sky = snap.get("weather")
    if sky:
        now = sky.get("now") or {}
        head = "--- WEATHER ---"
        if now.get("temp") is not None:
            head += (f"\nNow: {now['temp']:.0f}C, {now.get('condition')}"
                     + (f", wind {now['wind']:.0f} km/h"
                        if now.get("wind") is not None else ""))
        rows = sky.get("hourly") or []
        if rows:
            # Every hour, not a daily summary: the question is always when,
            # not whether. Read alongside the calendar above -- if something
            # he has to travel to collides with rain, that is worth saying
            # before he leaves rather than after.
            head += "\nHour by hour (local time):"
            for row in rows:
                chance = row.get("rain_chance")
                mm = row.get("rain_mm")
                bits = [f"  {row['time']}"]
                if row.get("temp") is not None:
                    bits.append(f"{row['temp']:.0f}C")
                bits.append(str(row.get("condition")))
                if chance is not None:
                    bits.append(f"rain {chance}%")
                if mm:
                    bits.append(f"{mm:.1f}mm")
                head += "\n" + "  ".join(bits)
        lines.append(head)

    news = snap.get("news") or []
    if news:
        block = "--- HEADLINES (requested for startup briefings; otherwise mention only when relevant) ---"
        for item in news:
            block += f"\n- {item['title']}"
            if item.get("source"):
                block += f" ({item['source']})"
        lines.append(block)

    return "\n\n".join(lines)


# --------------------------------------------------------------------- #
# Keeping it current
# --------------------------------------------------------------------- #

_refresher = None


def start_refresher(on_change=None, interval=REFRESH_SECONDS):
    """Refresh in the background so a briefing is never hours old.

    Startup-only was the original shape, and it meant the weather VANTA knew
    about was the weather when the machine booted. A forecast that mattered at
    four in the afternoon was fetched at nine in the morning.
    """
    global _refresher
    if _refresher is not None and _refresher.is_alive():
        return _refresher

    def loop():
        while True:
            time.sleep(max(60, interval))
            try:
                import runtime_power
                if not runtime_power.background_allowed():
                    continue
                before = context_block()
                snapshot(force=True)
                after = context_block()
                if on_change and after and after != before:
                    on_change(after)
            except Exception as exc:
                print(f"[Ambient] Refresh failed: {exc}")

    _refresher = threading.Thread(target=loop, daemon=True,
                                  name="vanta-ambient")
    _refresher.start()
    return _refresher


# --------------------------------------------------------------------- #
# Keeping what is worth keeping
# --------------------------------------------------------------------- #

def remember_headlines(base_dir=None, topic="news"):
    """File today's headlines in the knowledge store, dated.

    Deliberately the knowledge store and not the fact graph. A graph edge is
    asserted as true until invalidated, and "it is raining" or "Iran proposes a
    deal" are true for an afternoon -- that is exactly the shape of the bug
    where a note saying the exam was "tomorrow" was still being repeated a day
    later. Knowledge entries carry their date and read as a record of what was
    reported, which is what these are.
    """
    items = headlines()
    if not items:
        return 0
    try:
        import datetime

        import memory
        stamp = datetime.date.today().isoformat()
        body = "\n".join(f"- {item['title']}"
                         + (f" ({item['source']})" if item.get("source") else "")
                         for item in items)
        return memory.save_knowledge(
            f"Headlines for {stamp}",
            f"Reported on {stamp}:\n{body}",
            topic=topic, source="news_rss")
    except Exception as exc:
        print(f"[Ambient] Could not file headlines: {exc}")
        return 0
