"""Weather for VANTA.

Travel mode has always talked about weather without having any way to look it
up. This uses Open-Meteo, which needs no API key and no account, and reuses the
Nominatim geocoding already in maps_api rather than introducing a second
location service that could disagree with the first.

No key means nothing to rotate, nothing to leak, and no quota to exhaust at the
moment Himanshu actually needs it.
"""

import requests

_ENDPOINT = "https://api.open-meteo.com/v1/forecast"

# WMO weather codes. The API returns a number; a number is not an answer.
_CONDITIONS = {
    0: "clear", 1: "mostly clear", 2: "partly cloudy", 3: "overcast",
    45: "foggy", 48: "freezing fog",
    51: "light drizzle", 53: "drizzle", 55: "heavy drizzle",
    56: "freezing drizzle", 57: "heavy freezing drizzle",
    61: "light rain", 63: "rain", 65: "heavy rain",
    66: "freezing rain", 67: "heavy freezing rain",
    71: "light snow", 73: "snow", 75: "heavy snow", 77: "snow grains",
    80: "light showers", 81: "showers", 82: "violent showers",
    85: "snow showers", 86: "heavy snow showers",
    95: "thunderstorms", 96: "thunderstorms with hail",
    99: "severe thunderstorms with hail",
}


def _describe(code):
    return _CONDITIONS.get(int(code), f"weather code {code}")


#: How far ahead Open-Meteo forecasts, and how far the answer can be trusted.
MAX_HORIZON_DAYS = 15
RELIABLE_DAYS = 7

_WEEKDAYS = {
    "monday": 0, "mon": 0, "tuesday": 1, "tues": 1, "tue": 1,
    "wednesday": 2, "weds": 2, "wed": 2, "thursday": 3, "thurs": 3,
    "thur": 3, "thu": 3, "friday": 4, "fri": 4, "saturday": 5, "sat": 5,
    "sunday": 6, "sun": 6,
}
_RELATIVE = {"today": 0, "tonight": 0, "tomorrow": 1, "tmrw": 1, "tmr": 1,
             "tomo": 1, "day after tomorrow": 2, "yesterday": -1}

#: Parts of a day, in local hours, for the hour-by-hour summary.
DAY_PARTS = (("morning", 6, 12), ("afternoon", 12, 17),
             ("evening", 17, 21), ("night", 21, 30))


def resolve_day(spec, today=None):
    """The date a person means, worked out here rather than by the model.

    The first version of this action took `days="2"`, an offset, which meant
    that "Sunday" had to be turned into a number by whatever was calling it.
    That is date arithmetic by inference, and it is how VANTA came to say it
    "can only check today's weather" about a perfectly available forecast: the
    catalogue's only example was days="0".

    Returns a date, or None if the text names no day.
    """
    import datetime
    import re
    today = today or datetime.date.today()
    text = " ".join(str(spec or "").lower().replace(",", " ").split())
    if not text:
        return None

    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        try:
            return datetime.date.fromisoformat(text)
        except ValueError:
            return None

    for phrase, offset in sorted(_RELATIVE.items(), key=lambda kv: -len(kv[0])):
        if re.search(rf"\b{re.escape(phrase)}\b", text):
            return today + datetime.timedelta(days=offset)

    match = re.search(r"\bin\s+(\d{1,2})\s+days?\b", text)
    if match:
        return today + datetime.timedelta(days=int(match.group(1)))

    words = re.findall(r"[a-z]+", text)
    for word in words:
        if word in _WEEKDAYS:
            ahead = (_WEEKDAYS[word] - today.weekday()) % 7
            # "Sunday", said on a Sunday, is today. For weather that is the
            # useful reading -- the opposite of scheduling, where "Monday" said
            # on a Monday means next week.
            if "next" in words and ahead < 7:
                # "this Sunday" is the nearest one; "next Sunday" is the one
                # after it.
                ahead += 7 if ahead else 0
            return today + datetime.timedelta(days=ahead)

    try:
        from dateutil import parser
        parsed = parser.parse(text, default=datetime.datetime(
            today.year, today.month, today.day), fuzzy=True).date()
        # "18 oct" carries no year; if that has already gone by, it means next.
        if parsed < today and not re.search(r"\b\d{4}\b", text):
            parsed = parsed.replace(year=parsed.year + 1)
        return parsed
    except (ValueError, OverflowError, ImportError):
        return None


def _day_label(day, today):
    """"Sunday 18 Oct", with "today"/"tomorrow" where they apply.

    Every forecast line carries the weekday. A bare "2026-10-18" made whoever
    read it map a date back to a day of the week -- the same arithmetic, done
    in the other direction.
    """
    name = f"{day.strftime('%A')} {day.day} {day.strftime('%b')}"
    offset = (day - today).days
    if offset == 0:
        return f"Today ({name})"
    if offset == 1:
        return f"Tomorrow ({name})"
    return name


def _dominant(codes):
    """The most common weather code in a stretch of hours."""
    counted = {}
    for code in codes:
        if code is not None:
            counted[code] = counted.get(code, 0) + 1
    if not counted:
        return None
    # Ties go to the worse weather: a day that is half rain is a rainy day.
    return max(counted, key=lambda code: (counted[code], code))


def day_parts(hourly, day):
    """Morning / afternoon / evening / night for one date, from hourly data."""
    times = hourly.get("time") or []
    out = []
    for name, start, end in DAY_PARTS:
        temps, rain, codes = [], [], []
        for index, stamp in enumerate(times):
            if not str(stamp).startswith(day.isoformat()):
                continue
            hour = int(str(stamp)[11:13])
            if not start <= hour < end and not (end > 24 and hour < end - 24):
                continue
            temps.append((hourly.get("temperature_2m") or [None])[index])
            rain.append((hourly.get("precipitation_probability") or [None])[index])
            codes.append((hourly.get("weather_code") or [None])[index])
        temps = [t for t in temps if t is not None]
        rain = [r for r in rain if r is not None]
        if not temps:
            continue
        line = (f"  {name:<9} {_describe(_dominant(codes) or 0)}, "
                f"{min(temps):.0f}-{max(temps):.0f}°C")
        if rain:
            line += f", up to {max(rain)}% chance of rain"
        out.append(line)
    return out


def get_weather(location, days=0, date=""):
    """Current conditions; a forecast for N days; or one named day in detail.

    `date` takes whatever a person would say -- "sunday", "tomorrow", "in 3
    days", "18 oct", an ISO date -- and is resolved here.
    """
    import datetime
    today = datetime.date.today()
    place = str(location or "").strip()
    if not place:
        return ("[Error] GET_WEATHER needs a location. Do not guess where Himanshu "
                "is — ask him, or use a place he has told you about.")

    target = None
    if str(date or "").strip():
        target = resolve_day(date, today)
        if target is None:
            return (f"[Error] I could not tell which day '{date}' means. Give a "
                    f"weekday, 'tomorrow', 'in 3 days', or a date.")
        offset = (target - today).days
        if offset < 0:
            return (f"[Error] {target.strftime('%A %d %b')} is in the past, so "
                    f"there is no forecast for it.")
        if offset > MAX_HORIZON_DAYS:
            return (f"[Error] {target.strftime('%A %d %b')} is {offset} days "
                    f"away. Forecasts reach {MAX_HORIZON_DAYS} days ahead, so "
                    f"that is beyond what any weather service can say yet.")

    try:
        from providers.maps_api import get_coordinates
        lat, lon = get_coordinates(place)
    except Exception as exc:
        return f"[Error] Could not look up '{place}': {exc}"
    if not lat:
        return f"[Error] I could not find a place called '{place}'."

    params = {
        "latitude": lat, "longitude": lon,
        "current": "temperature_2m,apparent_temperature,relative_humidity_2m,"
                   "precipitation,weather_code,wind_speed_10m",
        "timezone": "auto",
    }
    horizon = max(0, min(int(days or 0), MAX_HORIZON_DAYS))
    if target is not None:
        horizon = max(horizon, (target - today).days)
    if horizon:
        params["daily"] = ("weather_code,temperature_2m_max,temperature_2m_min,"
                           "precipitation_probability_max,sunrise,sunset")
        params["forecast_days"] = horizon + 1
    if target is not None:
        params["hourly"] = ("temperature_2m,precipitation_probability,"
                            "weather_code")

    try:
        response = requests.get(_ENDPOINT, params=params, timeout=12)
        response.raise_for_status()
        data = response.json()
    except requests.Timeout:
        return "[Error] The weather service did not respond in time."
    except Exception as exc:
        return f"[Error] Could not fetch weather: {exc}"

    # The service's own idea of "today" at that place, which can differ from
    # this machine's near midnight.
    daily = data.get("daily") or {}
    current = data.get("current", {})
    lines = []
    if target is None:
        lines.append(
            f"Weather in {place}: {_describe(current.get('weather_code', 0))}, "
            f"{current.get('temperature_2m')}°C "
            f"(feels like {current.get('apparent_temperature')}°C), "
            f"humidity {current.get('relative_humidity_2m')}%, "
            f"wind {current.get('wind_speed_10m')} km/h.")
        if current.get("precipitation"):
            lines.append(f"Precipitation right now: {current['precipitation']} mm.")

    if horizon and daily.get("time"):
        wanted = [target.isoformat()] if target is not None else \
            list(daily["time"][:horizon + 1])
        for stamp in wanted:
            if stamp not in daily["time"]:
                continue
            index = daily["time"].index(stamp)
            day = datetime.date.fromisoformat(stamp)
            rain = (daily.get("precipitation_probability_max") or [None])[index]
            summary = (
                f"{_day_label(day, today)} in {place}: "
                f"{_describe(daily['weather_code'][index])}, "
                f"{daily['temperature_2m_min'][index]}-"
                f"{daily['temperature_2m_max'][index]}°C"
                + (f", {rain}% chance of rain" if rain is not None else ""))
            lines.append(summary)
            if target is not None:
                sunrise = (daily.get("sunrise") or [""])[index]
                sunset = (daily.get("sunset") or [""])[index]
                if sunrise and sunset:
                    lines.append(f"  Sun {str(sunrise)[11:16]} to "
                                 f"{str(sunset)[11:16]}.")
                lines.extend(day_parts(data.get("hourly") or {}, day))
                if (target - today).days > RELIABLE_DAYS:
                    lines.append(
                        f"  This is {(target - today).days} days out, so treat "
                        f"it as a rough indication rather than a promise.")
    if target is not None and len(lines) == 0:
        return (f"[Error] The weather service returned no forecast for "
                f"{target.strftime('%A %d %b')} in {place}.")
    return "\n".join(lines)
