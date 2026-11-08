"""Small YouTube Data API v3 client for VANTA.

The Data API discovers videos and supplies stable metadata.  It does not
control playback; the managed browser owns that part and verifies its state.
Keeping those responsibilities separate prevents a search result from being
mistaken for a video that actually started playing.
"""
from __future__ import annotations

import html
import os
import threading
import time
from urllib.parse import parse_qs, urlparse

import requests


API_ROOT = "https://www.googleapis.com/youtube/v3"
REQUEST_TIMEOUT = 15
CACHE_SECONDS = 10 * 60
_cache = {}
_lock = threading.RLock()


class YouTubeError(RuntimeError):
    pass


def configured():
    return bool(_api_key())


def _api_key():
    # A dedicated restricted key is preferred. GOOGLE_YOUTUBE_API_KEY is kept
    # as an explicit alias; unrelated Maps/Gemini keys are never borrowed.
    return (os.getenv("YOUTUBE_API_KEY", "").strip()
            or os.getenv("GOOGLE_YOUTUBE_API_KEY", "").strip())


def _request(resource, params):
    key = _api_key()
    if not key:
        raise YouTubeError(
            "YouTube Data API is not configured. Enable YouTube Data API v3 "
            "for a restricted Google Cloud API key and set YOUTUBE_API_KEY "
            "in D:\\winter\\.env.")
    query = dict(params)
    query["key"] = key
    try:
        response = requests.get(f"{API_ROOT}/{resource}", params=query,
                                timeout=REQUEST_TIMEOUT)
    except requests.RequestException as exc:
        raise YouTubeError(f"YouTube API could not be reached: {exc}") from exc
    try:
        payload = response.json()
    except ValueError as exc:
        raise YouTubeError(
            f"YouTube API returned HTTP {response.status_code} without JSON") from exc
    if response.status_code >= 400:
        detail = payload.get("error", {}) if isinstance(payload, dict) else {}
        message = detail.get("message") or f"HTTP {response.status_code}"
        raise YouTubeError(f"YouTube API rejected the request: {message}")
    return payload


def _clean(value):
    return " ".join(html.unescape(str(value or "")).split())


def search(query, limit=5, order="relevance"):
    wanted = " ".join(str(query or "").split())
    if not wanted:
        raise YouTubeError("YOUTUBE_SEARCH needs a query")
    try:
        count = max(1, min(int(limit or 5), 10))
    except (TypeError, ValueError):
        count = 5
    allowed_orders = {"date", "rating", "relevance", "title", "viewCount"}
    selected_order = str(order or "relevance")
    if selected_order not in allowed_orders:
        selected_order = "relevance"
    cache_key = (wanted.casefold(), count, selected_order)
    with _lock:
        cached = _cache.get(cache_key)
        if cached and time.monotonic() - cached[0] < CACHE_SECONDS:
            return [dict(item) for item in cached[1]]

    payload = _request("search", {
        "part": "snippet",
        "type": "video",
        "q": wanted,
        "maxResults": count,
        "order": selected_order,
        "safeSearch": "moderate",
        "regionCode": "IN",
        "relevanceLanguage": "en",
    })
    results = []
    for item in payload.get("items", []):
        video_id = str((item.get("id") or {}).get("videoId") or "").strip()
        if not video_id:
            continue
        snippet = item.get("snippet") or {}
        thumbnails = snippet.get("thumbnails") or {}
        thumb = (thumbnails.get("high") or thumbnails.get("medium")
                 or thumbnails.get("default") or {})
        results.append({
            "video_id": video_id,
            "title": _clean(snippet.get("title")),
            "channel": _clean(snippet.get("channelTitle")),
            "published_at": str(snippet.get("publishedAt") or ""),
            "description": _clean(snippet.get("description"))[:500],
            "thumbnail": str(thumb.get("url") or ""),
            "url": f"https://www.youtube.com/watch?v={video_id}",
        })
    with _lock:
        _cache[cache_key] = (time.monotonic(), [dict(item) for item in results])
    return results


def extract_video_id(value):
    raw = str(value or "").strip()
    if not raw:
        return ""
    if "://" not in raw and all(ch.isalnum() or ch in "-_" for ch in raw):
        return raw
    parsed = urlparse(raw)
    host = (parsed.hostname or "").lower()
    if host == "youtu.be":
        return parsed.path.strip("/").split("/", 1)[0]
    if host in {"youtube.com", "www.youtube.com", "m.youtube.com"}:
        if parsed.path == "/watch":
            return (parse_qs(parsed.query).get("v") or [""])[0]
        if parsed.path.startswith(("/shorts/", "/live/", "/embed/")):
            parts = parsed.path.strip("/").split("/")
            return parts[1] if len(parts) > 1 else ""
    return ""


def search_receipt(query, limit=5, order="relevance"):
    try:
        results = search(query, limit=limit, order=order)
    except YouTubeError as exc:
        return f"[Error] {exc}"
    if not results:
        return f"[Success] YouTube API returned 0 videos for {query!r}."
    lines = [f"[Success] YouTube API returned {len(results)} videos for {query!r}."]
    for index, item in enumerate(results, 1):
        lines.append(
            f"{index}. {item['title']} — {item['channel']}\n"
            f"   video_id={item['video_id']} URL: {item['url']}")
    return "\n".join(lines)


def resolve_video(query="", video_id="", url=""):
    chosen = extract_video_id(video_id or url)
    if chosen:
        return {
            "video_id": chosen,
            "title": "",
            "channel": "",
            "url": f"https://www.youtube.com/watch?v={chosen}",
        }
    results = search(query, limit=1)
    if not results:
        raise YouTubeError(f"YouTube API found no video for {query!r}")
    return results[0]

