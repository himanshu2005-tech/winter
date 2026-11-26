# -*- coding: utf-8 -*-
"""Structured product-price evidence through SerpApi Google Shopping.

Web pages are useful when an exact product URL is known, but they are a poor
price API: stores render prices in JavaScript, expose several unrelated
amounts, and regularly return bot challenges.  This provider gives VANTA a
typed, cached price source for product discovery and durable watches.  It does
not decide whether a result is the product the user meant; it returns the
candidate evidence and keeps that judgement with the model.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import statistics
import threading
import time
from datetime import datetime, timezone
from typing import Any

import requests


ENDPOINT = "https://serpapi.com/search.json"
_TIMEOUT = (5, 20)
_CACHE_TTL = int(os.getenv("VANTA_PRICE_CACHE_SECONDS", "900"))
_lock = threading.RLock()
_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_QUERY_NOISE = frozenset({
    "buy", "price", "prices", "cost", "deal", "deals", "offer", "offers",
    "online", "india", "indian", "new", "latest", "best", "cheap", "cheapest",
})


def _key() -> str:
    value = os.getenv("SERPAPI_API_KEY", "").strip()
    if value:
        return value.removeprefix("serpapi-")
    try:
        from providers.chatgpt_auth import get_secret_chunked
        return (get_secret_chunked("WINTER_SYSTEM", "serpapi_api_key")
                or "").strip().removeprefix("serpapi-")
    except Exception:
        return ""


def available() -> bool:
    return bool(_key())


def _number(value: Any) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    raw = str(value or "").replace(",", "")
    found = re.search(r"-?\d+(?:\.\d+)?", raw)
    if not found:
        return None
    try:
        return float(found.group(0))
    except ValueError:
        return None


def _identity(**values: Any) -> str:
    raw = json.dumps(values, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _match_score(query: str, title: str) -> float:
    """Conservative lexical variant match for unattended price evidence.

    This is evidence validation, not product interpretation.  Model/size
    identifiers (M4, 16GB, 512GB, S24...) must survive into the result title;
    otherwise a cheap accessory or older generation must not become the
    lowest price for the requested item.
    """
    def prepared(value):
        text = re.sub(r"(\d+)\s+(gb|tb|mb|inch|inches|cm)\b", r"\1\2",
                      str(value or "").casefold())
        words = re.findall(r"[a-z0-9]+", text)
        compact = "".join(words)
        return words, compact

    query_words, _ = prepared(query)
    title_words, title_compact = prepared(title)
    raw_wanted = {word for word in query_words if word not in _QUERY_NOISE}
    # Short model generations such as M4/S9 are highly discriminative even
    # though ordinary two-letter words are not.
    specific = {word for word in raw_wanted
                if len(word) >= 2 and any(ch.isdigit() for ch in word)}
    ordinary = {word for word in raw_wanted
                if len(word) >= 3 and word not in specific}
    if specific and any(word not in title_compact for word in specific):
        return 0.0
    title_set = set(title_words)
    coverage = (len(ordinary & title_set) / len(ordinary)) if ordinary else 1.0
    if ordinary and coverage < 0.5:
        return 0.0
    return min(1.0, 0.65 * coverage + (0.35 if specific else 0.0))


def _compact(item: dict[str, Any]) -> dict[str, Any] | None:
    extracted = item.get("extracted_price")
    price = _number(extracted if extracted is not None else item.get("price"))
    if price is None or price < 0:
        return None
    return {
        "title": " ".join(str(item.get("title") or "").split())[:240],
        "price": price,
        "currency": str(item.get("currency") or "INR").upper(),
        "merchant": " ".join(str(item.get("source") or "").split())[:120],
        "link": str(item.get("product_link") or item.get("link") or "").strip(),
        "product_id": str(item.get("product_id") or "").strip(),
        "rating": item.get("rating"),
        "reviews": item.get("reviews"),
        "delivery": " ".join(str(item.get("delivery") or "").split())[:160],
        "position": item.get("position"),
    }


def _product_offers(product_id: str, query: str, country: str,
                    language: str, api_key: str, limit: int) -> dict[str, Any]:
    """Exact offer view for a product selected during Shopping discovery."""
    response = requests.get(ENDPOINT, params={
        "engine": "google_product", "product_id": product_id,
        "offer_view": "true", "api_key": api_key,
        "gl": country, "hl": language,
    }, timeout=_TIMEOUT)
    if response.status_code == 429:
        raise RuntimeError("SerpApi pricing is rate limited; retry later")
    response.raise_for_status()
    payload = response.json()
    if payload.get("error"):
        raise RuntimeError(str(payload["error"])[:300])
    product = payload.get("product_results") or {}
    sellers = product.get("stores") or []
    if not sellers:
        seller_group = product.get("sellers_results") or payload.get("sellers_results") or {}
        sellers = (seller_group.get("online_sellers") or
                   seller_group.get("local_sellers") or [])
    product_title = " ".join(str(product.get("title") or query).split())
    offers = []
    for item in sellers:
        adapted = {
            "title": item.get("title") or product_title,
            "price": item.get("price"),
            "extracted_price": item.get("extracted_price"),
            "currency": item.get("currency") or "INR",
            "source": item.get("name") or item.get("source"),
            "product_link": item.get("link"),
            "product_id": product_id,
            "rating": item.get("rating"), "reviews": item.get("reviews"),
            "delivery": item.get("delivery") or item.get("details_and_offers"),
            "position": item.get("position"),
        }
        row = _compact(adapted)
        if row:
            row["match_score"] = 1.0
            offers.append(row)
    # Some locales expose one current price on the product itself rather than
    # a stores array. Preserve it as evidence instead of reporting no result.
    if not offers and (product.get("extracted_price") is not None or product.get("price")):
        row = _compact({
            "title": product_title, "price": product.get("price"),
            "extracted_price": product.get("extracted_price"),
            "currency": product.get("currency") or "INR",
            "source": product.get("source") or "Google Product",
            "product_link": product.get("link"), "product_id": product_id,
        })
        if row:
            row["match_score"] = 1.0
            offers.append(row)
    offers.sort(key=lambda row: (row["price"], row.get("position") or 9999))
    prices = [row["price"] for row in offers]
    return {
        "ok": True, "query": query, "product_id": product_id,
        "offers": offers[:limit],
        "lowest_price": min(prices) if prices else None,
        "highest_price": max(prices) if prices else None,
        "median_price": statistics.median(prices) if prices else None,
        "rejected_loose_matches": 0,
        "currency": offers[0]["currency"] if offers else "INR",
        "source": "SerpApi Google Product",
        "searched_at": datetime.now(timezone.utc).isoformat(),
        "cached": False,
    }


def search(query: str, *, location: str = "India", country: str = "in",
           language: str = "en", limit: int = 10, product_id: str = "",
           force: bool = False) -> dict[str, Any]:
    """Return normalized, priced Google Shopping offers.

    ``product_id`` narrows a durable watch to the product selected from an
    earlier result.  When it is absent, every candidate remains visible so the
    model can reason about variant/merchant ambiguity instead of silently
    treating the cheapest row as truth.
    """
    query = " ".join(str(query or "").split())
    if not query:
        raise ValueError("a product query is required")
    api_key = _key()
    if not api_key:
        raise RuntimeError("SerpApi product pricing is not configured")
    limit = max(1, min(int(limit or 10), 20))
    country = re.sub(r"[^a-z]", "", str(country or "in").lower())[:2] or "in"
    language = re.sub(r"[^a-z]", "", str(language or "en").lower())[:2] or "en"
    cache_key = _identity(query=query.casefold(), location=location,
                          country=country, language=language,
                          product_id=product_id, limit=limit)
    with _lock:
        cached = _cache.get(cache_key)
    if cached and not force and time.time() - cached[0] <= _CACHE_TTL:
        return {**cached[1], "cached": True}

    if product_id:
        result = _product_offers(product_id, query, country, language,
                                 api_key, limit)
        with _lock:
            _cache[cache_key] = (time.time(), dict(result))
        return result

    params = {
        "engine": "google_shopping", "q": query, "api_key": api_key,
        "gl": country, "hl": language, "location": location or "India",
        # Ask for enough candidates to survive conservative variant filtering.
        "num": max(10, limit),
    }
    response = requests.get(ENDPOINT, params=params, timeout=_TIMEOUT)
    if response.status_code == 429:
        raise RuntimeError("SerpApi pricing is rate limited; retry later")
    response.raise_for_status()
    payload = response.json()
    if payload.get("error"):
        raise RuntimeError(str(payload["error"])[:300])

    offers = []
    rejected = 0
    for item in payload.get("shopping_results") or []:
        row = _compact(item)
        if not row:
            continue
        if product_id and row.get("product_id") != str(product_id):
            continue
        score = 1.0 if product_id else _match_score(query, row.get("title", ""))
        if score <= 0:
            rejected += 1
            continue
        row["match_score"] = score
        offers.append(row)
    offers.sort(key=lambda row: (row["price"], row.get("position") or 9999))
    prices = [row["price"] for row in offers]
    result = {
        "ok": True,
        "query": query,
        "product_id": str(product_id or ""),
        "offers": offers[:limit],
        "lowest_price": min(prices) if prices else None,
        "highest_price": max(prices) if prices else None,
        "median_price": statistics.median(prices) if prices else None,
        "rejected_loose_matches": rejected,
        "currency": (offers[0]["currency"] if offers else "INR"),
        "source": "SerpApi Google Shopping",
        "searched_at": datetime.now(timezone.utc).isoformat(),
        "cached": False,
    }
    with _lock:
        _cache[cache_key] = (time.time(), dict(result))
        if len(_cache) > 128:
            for old, _ in sorted(_cache.items(), key=lambda row: row[1][0])[:32]:
                _cache.pop(old, None)
    return result


def describe(result: dict[str, Any]) -> str:
    offers = result.get("offers") or []
    if not offers:
        return (f"No priced shopping offers were returned for "
                f"{result.get('query')!r}.")
    lines = [
        f"Live product-price evidence for {result.get('query')} "
        f"from {result.get('source')}:"
    ]
    for row in offers[:10]:
        merchant = f" from {row['merchant']}" if row.get("merchant") else ""
        product = f" [product_id={row['product_id']}]" if row.get("product_id") else ""
        lines.append(f"- INR {row['price']:,.0f}{merchant}: {row.get('title')}{product}")
    if result.get("median_price") is not None:
        lines.append(f"Observed median: INR {float(result['median_price']):,.0f}.")
    if result.get("rejected_loose_matches"):
        lines.append(
            f"Excluded {int(result['rejected_loose_matches'])} looser variant/accessory match(es).")
    lines.append("These are current marketplace observations, not guaranteed checkout prices; verify the exact variant and seller before purchasing.")
    return "\n".join(lines)
