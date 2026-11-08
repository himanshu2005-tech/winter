"""Structured Google Flights access through SerpApi.

Searches are cached because flight results are costly and volatile. Callers
receive compact, provenance-bearing offers rather than raw search payloads.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import threading
import time
from typing import Any

import requests

ENDPOINT = "https://serpapi.com/search.json"
_TIMEOUT = 25
_CACHE_TTL = 10 * 60
_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_lock = threading.RLock()


def _key() -> str:
    value = os.getenv("SERPAPI_API_KEY", "").strip()
    if value:
        return value.removeprefix("serpapi-")
    try:
        from security.keyring_manager import get_secret_chunked
        return (get_secret_chunked("WINTER_SYSTEM", "serpapi_api_key")
                or "").strip().removeprefix("serpapi-")
    except Exception:
        return ""


def _airport(value: str) -> str:
    value = str(value or "").strip().upper()
    if not re.fullmatch(r"[A-Z]{3}(?:,[A-Z]{3})*", value):
        raise ValueError("origin and destination must be three-letter airport codes such as HYD or DEL")
    return value


def _date(value: str, name: str) -> str:
    try:
        parsed = dt.date.fromisoformat(str(value or ""))
    except ValueError as exc:
        raise ValueError(f"{name} must use YYYY-MM-DD") from exc
    if parsed < dt.date.today():
        raise ValueError(f"{name} cannot be in the past")
    return parsed.isoformat()


def _compact_flight(item: dict[str, Any]) -> dict[str, Any]:
    legs = item.get("flights") or []
    first, last = (legs[0] if legs else {}), (legs[-1] if legs else {})
    airlines = list(dict.fromkeys(str(leg.get("airline") or "").strip()
                                  for leg in legs if leg.get("airline")))
    numbers = [str(leg.get("flight_number") or "").strip()
               for leg in legs if leg.get("flight_number")]
    departure = first.get("departure_airport") or {}
    arrival = last.get("arrival_airport") or {}
    return {
        "price": item.get("price"), "currency": "INR",
        "airlines": airlines, "flight_numbers": numbers,
        "departure_airport": departure.get("id") or departure.get("name"),
        "departure_time": departure.get("time"),
        "arrival_airport": arrival.get("id") or arrival.get("name"),
        "arrival_time": arrival.get("time"),
        "duration_minutes": item.get("total_duration"),
        "stops": max(0, len(legs) - 1),
        "layovers": item.get("layovers") or [],
        "booking_token": item.get("booking_token"),
        "extensions": item.get("extensions") or [],
    }


def search(origin: str, destination: str, departure_date: str, *,
           return_date: str = "", adults: int = 1, travel_class: int = 1,
           nonstop: bool = False, max_price: int | None = None,
           limit: int = 8, force: bool = False) -> dict[str, Any]:
    api_key = _key()
    if not api_key:
        raise RuntimeError("SerpApi is not configured")
    origin, destination = _airport(origin), _airport(destination)
    departure_date = _date(departure_date, "departure_date")
    if return_date:
        return_date = _date(return_date, "return_date")
        if return_date < departure_date:
            raise ValueError("return_date cannot be before departure_date")
    adults = max(1, min(int(adults), 9))
    travel_class = max(1, min(int(travel_class), 4))
    limit = max(1, min(int(limit), 20))
    query = {"engine": "google_flights", "departure_id": origin,
             "arrival_id": destination, "outbound_date": departure_date,
             "type": 1 if return_date else 2, "currency": "INR", "hl": "en",
             "gl": "in", "adults": adults, "travel_class": travel_class,
             "sort_by": 2, "api_key": api_key}
    if return_date:
        query["return_date"] = return_date
    if nonstop:
        query["stops"] = 1
    if max_price is not None:
        query["max_price"] = max(1, int(max_price))
    identity = hashlib.sha256(json.dumps({k: v for k, v in query.items() if k != "api_key"},
                                         sort_keys=True).encode()).hexdigest()
    with _lock:
        cached = _cache.get(identity)
    if cached and not force and time.time() - cached[0] < _CACHE_TTL:
        return {**cached[1], "cached": True}
    response = requests.get(ENDPOINT, params=query, timeout=_TIMEOUT)
    response.raise_for_status()
    payload = response.json()
    if payload.get("error"):
        raise RuntimeError(str(payload["error"])[:300])
    raw = (payload.get("best_flights") or []) + (payload.get("other_flights") or [])
    offers = [_compact_flight(item) for item in raw]
    offers = [item for item in offers if isinstance(item.get("price"), (int, float))]
    offers.sort(key=lambda item: item["price"])
    result = {
        "ok": True, "origin": origin, "destination": destination,
        "departure_date": departure_date, "return_date": return_date or None,
        "offers": offers[:limit], "lowest_price": offers[0]["price"] if offers else None,
        "price_insights": payload.get("price_insights") or {},
        "source": "SerpApi Google Flights", "searched_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "cached": False,
    }
    with _lock:
        _cache[identity] = (time.time(), result)
    return result


def describe(result: dict[str, Any]) -> str:
    offers = result.get("offers") or []
    if not offers:
        return (f"No flight offers were found from {result.get('origin')} to "
                f"{result.get('destination')} on {result.get('departure_date')}.")
    lines = [f"Flights {result['origin']} to {result['destination']} on {result['departure_date']} "
             f"(live search; {'cached' if result.get('cached') else 'fresh'}):"]
    for offer in offers[:8]:
        airline = ", ".join(offer.get("airlines") or []) or "Airline unavailable"
        lines.append(f"- INR {offer['price']:,.0f}; {airline}; "
                     f"{offer.get('departure_time')} to {offer.get('arrival_time')}; "
                     f"{offer.get('duration_minutes')} min; {offer.get('stops')} stop(s)")
    insight = result.get("price_insights") or {}
    if insight.get("price_level"):
        lines.append(f"Price insight: {insight['price_level']}.")
    lines.append("Prices are observations, not guarantees; recheck before booking.")
    return "\n".join(lines)
