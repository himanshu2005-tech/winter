# -*- coding: utf-8 -*-
"""Durable, evidence-bearing conditions VANTA checks without occupying the screen.

A watch is an observation plus a condition plus a notification policy.  It is
not an LLM reminder and it does not keep a browser window open.  The scheduler
reads active watches from Neo4j, observes their source, records immutable
evidence, and alerts only on a false -> true condition crossing.

Adapters are deliberately typed.  Price watches parse structured Product JSON
before considering visible currency text; page-text, file and process watches
have similarly narrow evidence.  Adding another life-long capability means
adding an adapter here, not teaching the model to improvise a polling script.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import ipaddress
import json
import os
import re
import socket
import threading
import time
from urllib.parse import urlparse

DEFAULT_INTERVAL_SECONDS = 30 * 60
MIN_INTERVAL_SECONDS = 5 * 60
MAX_INTERVAL_SECONDS = 7 * 24 * 60 * 60
PRODUCT_PRICE_MIN_INTERVAL_SECONDS = max(
    60 * 60,
    int(float(os.getenv("VANTA_PRODUCT_PRICE_MIN_HOURS", "12")) * 60 * 60),
)
_STOP = threading.Event()


def _store():
    from memory.neo4j_store import store
    return store


def _now(now=None):
    return now or dt.datetime.now(dt.timezone.utc)


def _iso(value):
    if isinstance(value, str):
        return value
    return value.astimezone(dt.timezone.utc).isoformat()


def parse_interval(value):
    raw = " ".join(str(value or "").lower().split())
    if not raw:
        return DEFAULT_INTERVAL_SECONDS
    match = re.fullmatch(r"(\d+(?:\.\d+)?)\s*(m|min|mins|minutes|h|hr|hours|d|days)", raw)
    if not match:
        raise ValueError("interval must look like 30m, 2h, or 1d")
    amount = float(match.group(1))
    unit = match.group(2)[0]
    seconds = amount * {"m": 60, "h": 3600, "d": 86400}[unit]
    return int(max(MIN_INTERVAL_SECONDS, min(MAX_INTERVAL_SECONDS, seconds)))


def parse_number(value):
    """Indian/ordinary money speech and display text to a decimal number."""
    raw = str(value or "").strip().lower()
    if not raw:
        raise ValueError("a numeric target is required")
    multiplier = 1.0
    if re.search(r"\b(?:lakh|lac|lakhs|lacs)\b", raw):
        multiplier = 100000.0
    elif re.search(r"\b(?:crore|crores|cr)\b", raw):
        multiplier = 10000000.0
    elif re.search(r"\b(?:thousand|k)\b", raw):
        multiplier = 1000.0
    cleaned = re.sub(r"[^0-9.]", "", raw.replace(",", ""))
    if not cleaned or cleaned.count(".") > 1:
        raise ValueError(f"could not read numeric value '{value}'")
    return float(cleaned) * multiplier


def _valid_web_url(source):
    parsed = urlparse(str(source or "").strip())
    return parsed.scheme in {"http", "https"} and bool(parsed.hostname)


def _assert_public_url(source):
    """Reject loopback/private destinations before and after redirects."""
    if not _valid_web_url(source):
        raise ValueError("an exact http/https URL is required")
    host = urlparse(source).hostname
    try:
        addresses = {item[4][0] for item in socket.getaddrinfo(host, None)}
    except OSError as exc:
        raise ValueError(f"the source hostname could not be resolved: {exc}") from exc
    for address in addresses:
        if not ipaddress.ip_address(address).is_global:
            raise ValueError("private, loopback, link-local, and reserved URLs cannot be watched")


def _normalise_condition(kind, condition):
    kind = str(kind or "").strip().lower()
    condition = str(condition or "").strip().lower().replace(" ", "_")
    allowed = {
        "price": {"below", "at_or_below", "above", "at_or_above"},
        "product_price": {"below", "at_or_below", "above", "at_or_above"},
        "flight_price": {"below", "at_or_below", "above", "at_or_above"},
        "page_text": {"contains", "not_contains"},
        "file": {"exists", "missing", "changed"},
        "process": {"running", "stopped"},
    }
    if kind not in allowed:
        raise ValueError(
            "kind must be price, product_price, flight_price, page_text, file, or process")
    aliases = {"less_than": "below", "under": "below", "lte": "at_or_below",
               "greater_than": "above", "over": "above", "gte": "at_or_above"}
    condition = aliases.get(condition, condition)
    if condition not in allowed[kind]:
        raise ValueError(f"condition for {kind} must be one of {', '.join(sorted(allowed[kind]))}")
    return kind, condition


def _watch_id(kind, source, condition, target):
    material = "|".join((kind, str(source).strip(), condition,
                         str(target).strip().casefold()))
    return "watch-" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:18]


def create(kind, source, condition, target="", label="", interval="30m",
           currency="INR", now=None, store_obj=None):
    store_obj = store_obj or _store()
    kind, condition = _normalise_condition(kind, condition)
    source = str(source or "").strip()
    if kind in {"price", "page_text"} and not _valid_web_url(source):
        return "[Error] A price or page watch needs the exact http/https product URL."
    if kind in {"price", "page_text"}:
        try:
            _assert_public_url(source)
        except ValueError as exc:
            return f"[Error] {exc}"
    if kind == "file":
        source = os.path.abspath(source)
    if not source:
        return "[Error] A watch source is required."
    if kind in {"price", "product_price", "flight_price"}:
        target_value = parse_number(target)
        target_text = str(target_value)
    elif condition in {"contains", "not_contains"}:
        if not str(target or "").strip():
            return "[Error] A text watch needs the text to look for."
        target_text = str(target).strip()
    else:
        target_text = str(target or "").strip()
    interval_seconds = parse_interval(interval)
    if kind == "flight_price":
        # The free SerpApi account permits 250 searches/month. Six-hour checks
        # keep one durable watch near 120 while still catching useful changes.
        interval_seconds = max(interval_seconds, 6 * 60 * 60)
    elif kind == "product_price":
        # SerpApi is a quota-bearing evidence source.  A durable watch should
        # sample it, not hammer it; urgent one-off questions use PRICE_SEARCH.
        interval_seconds = max(interval_seconds, PRODUCT_PRICE_MIN_INTERVAL_SECONDS)
    now = _now(now)
    watch_id = _watch_id(kind, source, condition, target_text)
    clean_label = str(label or "").strip() or f"{kind} watch"
    rows = store_obj.query(
        """
        MERGE (w:Watch {id:$id})
        ON CREATE SET w.created_at=$now, w.trigger_count=0
        SET w.kind=$kind, w.source=$source, w.condition=$condition,
            w.target=$target, w.label=$label, w.currency=$currency,
            w.interval_seconds=$interval, w.status='active',
            w.next_check=$now, w.updated_at=$now,
            w.last_error=null, w.consecutive_failures=0
        RETURN w.id AS id, w.created_at AS created_at
        """, id=watch_id, kind=kind, source=source, condition=condition,
        target=target_text, label=clean_label, currency=str(currency or "INR").upper(),
        interval=interval_seconds, now=_iso(now))
    if not rows:
        return "[Error] Neo4j did not verify watch creation."
    return (f"[Success] Watch {watch_id} is active: {clean_label}. "
            f"It checks every {interval_seconds // 60} minutes and survives restarts.")


def create_flight(origin, destination, departure_date, target, *, return_date="",
                  adults=1, travel_class=1, nonstop=False, label="",
                  interval="6h", currency="INR", now=None, store_obj=None):
    """Create a typed fare watch without pretending a search URL is a product page."""
    from providers import flights_api
    origin = flights_api._airport(origin)
    destination = flights_api._airport(destination)
    departure_date = flights_api._date(departure_date, "departure_date")
    if return_date:
        return_date = flights_api._date(return_date, "return_date")
        if return_date < departure_date:
            raise ValueError("return_date cannot be before departure_date")
    spec = {"origin": origin, "destination": destination,
            "departure_date": departure_date, "return_date": return_date,
            "adults": max(1, min(int(adults), 9)),
            "travel_class": max(1, min(int(travel_class), 4)),
            "nonstop": bool(nonstop)}
    source = json.dumps(spec, sort_keys=True, separators=(",", ":"))
    return create("flight_price", source, "at_or_below", target,
                  label or f"{origin} to {destination} flight",
                  interval, currency, now=now, store_obj=store_obj)


def create_product_price(query, target, *, product_id="", location="India",
                         label="", interval="12h", currency="INR", now=None,
                         store_obj=None):
    """Create a quota-safe structured Google Shopping price watch."""
    query = " ".join(str(query or "").split())
    if not query:
        return "[Error] A product price watch needs a product query."
    product_id = str(product_id or "").strip()
    if not product_id:
        return ("[Error] A structured product watch needs an exact product_id. "
                "Run PRICE_SEARCH first and select the matching variant, or use "
                "WATCH_CREATE with an exact store URL.")
    spec = {"query": query, "product_id": product_id,
            "location": " ".join(str(location or "India").split())}
    source = json.dumps(spec, sort_keys=True, separators=(",", ":"))
    return create("product_price", source, "at_or_below", target,
                  label or query, interval, currency, now=now,
                  store_obj=store_obj)


def _rows(store_obj=None, status=None):
    store_obj = store_obj or _store()
    return store_obj.query(
        """
        MATCH (w:Watch)
        WHERE $status IS NULL OR w.status=$status
        RETURN properties(w) AS watch
        ORDER BY w.created_at DESC
        """, status=status)


def listing(store_obj=None):
    rows = _rows(store_obj)
    if not rows:
        return "No watches exist."
    lines = [f"Watches ({len(rows)}):"]
    for row in rows:
        w = row["watch"]
        state = w.get("status", "unknown")
        latest = w.get("last_value")
        detail = f"; last {latest}" if latest not in (None, "") else ""
        lines.append(f"- [{w['id']}] {w.get('label')} ? {state}{detail}")
    return "\n".join(lines)


def status(identifier, store_obj=None):
    watch = _find(identifier, store_obj)
    if not watch:
        return f"[Error] No watch matching '{identifier}'."
    lines = [f"Watch {watch['id']}: {watch.get('label')}",
             f"- status: {watch.get('status')}",
             f"- rule: {watch.get('kind')} {watch.get('condition')} {watch.get('target','')}",
             f"- source: {watch.get('source')}",
             f"- next check: {watch.get('next_check') or 'pending'}"]
    if watch.get("last_checked"):
        lines.append(f"- last observation: {watch.get('last_value')} at {watch.get('last_checked')}")
    if watch.get("last_error"):
        lines.append(f"- last error: {watch.get('last_error')}")
    return "\n".join(lines)


def _find(identifier, store_obj=None):
    store_obj = store_obj or _store()
    rows = store_obj.query(
        """MATCH (w:Watch)
        WHERE w.id=$wanted OR toLower(w.label)=toLower($wanted)
        RETURN properties(w) AS watch ORDER BY w.created_at DESC LIMIT 1""",
        wanted=str(identifier or "").strip())
    return rows[0]["watch"] if rows else None


def dependency_state(identifiers, store_obj=None):
    """Summarise watch dependencies without treating lookup failure as inactive.

    A recurring routine can refer to durable watch IDs.  Cancelling those
    watches must also make that routine inert; otherwise it wakes the model to
    check known-dead work and announces the expected errors.  ``unknown`` is
    kept separate so a temporary Neo4j outage never disables a valid routine.
    """
    store_obj = store_obj or _store()
    states = {}
    for identifier in dict.fromkeys(str(item).strip() for item in identifiers if item):
        watch = _find(identifier, store_obj)
        states[identifier] = None if watch is None else watch.get("status", "unknown")
    if not states:
        return {"all_inactive": False, "states": states}
    known = [state for state in states.values() if state is not None]
    all_resolved = len(known) == len(states)
    return {
        "all_inactive": all_resolved and all(
            state in {"cancelled", "paused"} for state in known),
        "states": states,
    }


def set_status(identifier, status, store_obj=None):
    if status not in {"active", "paused", "cancelled"}:
        return "[Error] Invalid watch status."
    store_obj = store_obj or _store()
    watch = _find(identifier, store_obj)
    if not watch:
        return f"[Error] No watch matching '{identifier}'."
    store_obj.query(
        """MATCH (w:Watch {id:$id}) SET w.status=$status,
        w.updated_at=$now, w.next_check=CASE WHEN $status='active' THEN $now ELSE w.next_check END
        RETURN w.id AS id""", id=watch["id"], status=status, now=_iso(_now()))
    return f"[Success] Watch {watch['id']} {status}."


def history(identifier, limit=20, store_obj=None):
    store_obj = store_obj or _store()
    watch = _find(identifier, store_obj)
    if not watch:
        return f"[Error] No watch matching '{identifier}'."
    rows = store_obj.query(
        """MATCH (:Watch {id:$id})-[:OBSERVED]->(o:WatchObservation)
        RETURN properties(o) AS observation ORDER BY o.observed_at DESC LIMIT $limit""",
        id=watch["id"], limit=max(1, min(int(limit), 100)))
    if not rows:
        return f"No observations yet for {watch['id']}."
    lines = [f"History for {watch.get('label')}:"]
    for row in rows:
        o = row["observation"]
        lines.append(f"- {o.get('observed_at')}: {o.get('value')} ({o.get('method')})")
    return "\n".join(lines)


def _jsonld_products(html):
    blocks = re.findall(r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
                        html, re.I | re.S)
    products = []
    for block in blocks:
        try:
            data = json.loads(block.strip())
        except Exception:
            continue
        stack = data if isinstance(data, list) else [data]
        while stack:
            item = stack.pop()
            if isinstance(item, dict):
                if item.get("@type") == "Product" or "offers" in item:
                    products.append(item)
                graph = item.get("@graph")
                if isinstance(graph, list):
                    stack.extend(graph)
    return products


def extract_price(html):
    """Return (price, currency, title, method) from strongest evidence first."""
    text = str(html or "")
    for product in _jsonld_products(text):
        offers = product.get("offers") or {}
        if isinstance(offers, list):
            offers = next((o for o in offers if isinstance(o, dict) and o.get("price")), {})
        if isinstance(offers, dict):
            raw = offers.get("price") or offers.get("lowPrice")
            if raw not in (None, ""):
                return (parse_number(raw), str(offers.get("priceCurrency") or "").upper(),
                        str(product.get("name") or "").strip(), "json-ld Product.offers")
    amount = re.search(r'(?:itemprop=["\']price["\'][^>]*content|property=["\']product:price:amount["\'][^>]*content)=["\']([^"\']+)', text, re.I)
    if amount:
        currency = re.search(r'(?:itemprop=["\']priceCurrency["\'][^>]*content|property=["\']product:price:currency["\'][^>]*content)=["\']([^"\']+)', text, re.I)
        title = re.search(r'<title[^>]*>(.*?)</title>', text, re.I | re.S)
        return (parse_number(amount.group(1)), currency.group(1).upper() if currency else "",
                re.sub(r'<[^>]+>', '', title.group(1)).strip() if title else "", "structured meta")
    # Last resort is deliberately narrow. If several different visible prices
    # exist (MRP, offer, exchange value, accessories), refusing is safer than
    # notifying on the wrong number.
    visible_prices = re.findall(
        r'(?:INR|Rs\.?)[\s\u00a0]*([0-9][0-9,]*(?:\.[0-9]{1,2})?)', text, re.I)
    distinct = {parse_number(value) for value in visible_prices}
    if len(distinct) == 1:
        title = re.search(r'<title[^>]*>(.*?)</title>', text, re.I | re.S)
        return (distinct.pop(), "INR",
                re.sub(r'<[^>]+>', '', title.group(1)).strip() if title else "", "visible currency text")
    if len(distinct) > 1:
        raise ValueError("the page contains multiple visible prices and no authoritative product price")
    raise ValueError("no defensible product price was found")


def _fetch_page(url):
    import requests
    _assert_public_url(url)
    response = requests.get(url, timeout=(5, 20), stream=True, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36",
        "Accept-Language": "en-IN,en;q=0.9",
    })
    response.raise_for_status()
    _assert_public_url(response.url)
    content_length = int(response.headers.get("Content-Length") or 0)
    if content_length > 5 * 1024 * 1024:
        raise ValueError("the source page is too large to monitor safely")
    chunks, total = [], 0
    for chunk in response.iter_content(chunk_size=65536):
        total += len(chunk)
        if total > 5 * 1024 * 1024:
            raise ValueError("the source page is too large to monitor safely")
        chunks.append(chunk)
    body = b"".join(chunks).decode(response.encoding or "utf-8", errors="replace")
    lowered = body.lower()
    if any(marker in lowered for marker in ("enter the characters you see below", "captcha", "robot check")):
        raise ValueError("the site returned a bot challenge instead of the product")
    return body, response.url


def observe(watch):
    kind, source = watch["kind"], watch["source"]
    if kind == "product_price":
        from providers import pricing_api
        spec = json.loads(source)
        result = pricing_api.search(
            spec["query"], product_id=spec.get("product_id", ""),
            location=spec.get("location", "India"), force=True)
        offers = result.get("offers") or []
        value = result.get("lowest_price")
        if value is None or not offers:
            raise ValueError("the pricing API returned no priced matching offers")
        best = offers[0]
        return {
            "value": float(value),
            "currency": best.get("currency") or result.get("currency") or "INR",
            "title": best.get("title") or spec["query"],
            "merchant": best.get("merchant") or "",
            "product_id": best.get("product_id") or spec.get("product_id", ""),
            "method": "SerpApi Google Shopping",
            "source_url": best.get("link") or "serpapi:google_shopping",
        }
    if kind == "flight_price":
        from providers import flights_api
        spec = json.loads(source)
        result = flights_api.search(**spec, limit=5, force=True)
        value = result.get("lowest_price")
        if value is None:
            raise ValueError("no priced flight offers were returned")
        return {"value": float(value), "currency": "INR",
                "title": f"{spec['origin']} to {spec['destination']} flight",
                "method": "SerpApi Google Flights", "source_url": "serpapi:google_flights"}
    if kind in {"price", "page_text"}:
        body, final_url = _fetch_page(source)
        if kind == "price":
            value, currency, title, method = extract_price(body)
            return {"value": value, "currency": currency, "title": title,
                    "method": method, "source_url": final_url}
        plain = re.sub(r'(?is)<script.*?</script>|<style.*?</style>|<[^>]+>', ' ', body)
        plain = " ".join(plain.split())
        return {"value": plain[:1000], "method": "page text", "source_url": final_url,
                "matched": str(watch.get("target", "")).casefold() in plain.casefold()}
    if kind == "file":
        exists = os.path.exists(source)
        stat = os.stat(source) if exists else None
        signature = f"{stat.st_size}:{stat.st_mtime_ns}" if stat else "missing"
        return {"value": signature, "exists": exists, "method": "filesystem stat"}
    if kind == "process":
        import psutil
        wanted = os.path.basename(source).casefold()
        running = any((p.info.get("name") or "").casefold() == wanted
                      for p in psutil.process_iter(["name"]))
        return {"value": running, "running": running, "method": "process table"}
    raise ValueError(f"unsupported watch kind {kind}")


def condition_met(watch, observation):
    condition = watch["condition"]
    if watch["kind"] in {"price", "product_price", "flight_price"}:
        wanted_currency = str(watch.get("currency") or "").upper()
        observed_currency = str(observation.get("currency") or "").upper()
        if wanted_currency and observed_currency and wanted_currency != observed_currency:
            raise ValueError(
                f"currency mismatch: expected {wanted_currency}, observed {observed_currency}")
        value, target = float(observation["value"]), float(watch["target"])
        return {"below": value < target, "at_or_below": value <= target,
                "above": value > target, "at_or_above": value >= target}[condition]
    if watch["kind"] == "page_text":
        found = bool(observation.get("matched"))
        return found if condition == "contains" else not found
    if watch["kind"] == "file":
        if condition == "exists": return bool(observation.get("exists"))
        if condition == "missing": return not observation.get("exists")
        previous = watch.get("last_value")
        return previous not in (None, "") and previous != observation.get("value")
    if watch["kind"] == "process":
        running = bool(observation.get("running"))
        return running if condition == "running" else not running
    return False


def _alert_text(watch, observation):
    if watch["kind"] in {"price", "product_price", "flight_price"}:
        currency = observation.get("currency") or watch.get("currency") or "INR"
        symbol = "Rs " if currency == "INR" else currency + " "
        title = observation.get("title") or watch.get("label")
        return (f"Price alert: {title} is now {symbol}{float(observation['value']):,.0f}, "
                f"meeting your {watch['condition'].replace('_', ' ')} {symbol}{float(watch['target']):,.0f} watch.")
    return f"Watch alert: {watch.get('label')} now meets its {watch.get('condition')} condition."


def check_one(identifier, store_obj=None, observer=None, now=None):
    store_obj = store_obj or _store()
    watch = _find(identifier, store_obj)
    if not watch:
        return {"ok": False, "message": f"No watch matching '{identifier}'."}
    if watch.get("status") != "active":
        return {"ok": False, "message": f"Watch {watch['id']} is {watch.get('status')}."}
    now = _now(now)
    try:
        observation = (observer or observe)(watch)
        met = condition_met(watch, observation)
        value = observation.get("value")
        obs_id = "obs-" + hashlib.sha256(
            f"{watch['id']}|{_iso(now)}|{value}".encode("utf-8")).hexdigest()[:22]
        store_obj.query(
            """MATCH (w:Watch {id:$id})
            CREATE (o:WatchObservation {id:$obs_id, observed_at:$now,
                value:$value, currency:$currency, method:$method,
                source_url:$source_url, condition_met:$met})
            CREATE (w)-[:OBSERVED]->(o)
            SET w.last_checked=$now, w.last_value=$value,
                w.last_method=$method, w.last_error=null,
                w.consecutive_failures=0, w.next_check=$next_check,
                w.last_triggered=$met,
                w.trigger_count=coalesce(w.trigger_count,0) + CASE WHEN $crossed THEN 1 ELSE 0 END,
                w.last_alert_at=CASE WHEN $crossed THEN $now ELSE w.last_alert_at END
            RETURN w.id AS id""",
            id=watch["id"], obs_id=obs_id, now=_iso(now), value=str(value),
            currency=str(observation.get("currency") or ""),
            method=str(observation.get("method") or ""),
            source_url=str(observation.get("source_url") or watch["source"]),
            met=met, crossed=bool(met and not watch.get("last_triggered")),
            next_check=_iso(now + dt.timedelta(seconds=int(watch.get("interval_seconds") or DEFAULT_INTERVAL_SECONDS))))
        crossed = bool(met and not watch.get("last_triggered"))
        return {"ok": True, "condition_met": met, "crossed": crossed,
                "watch_id": watch.get("id"), "kind": watch.get("kind"),
                "label": watch.get("label"),
                "observation": observation,
                "alert": _alert_text(watch, observation) if crossed else ""}
    except Exception as exc:
        failures = int(watch.get("consecutive_failures") or 0) + 1
        backoff = min(int(watch.get("interval_seconds") or DEFAULT_INTERVAL_SECONDS) * (2 ** min(failures, 4)),
                      MAX_INTERVAL_SECONDS)
        store_obj.query(
            """MATCH (w:Watch {id:$id}) SET w.last_checked=$now,
            w.last_error=$error, w.consecutive_failures=$failures,
            w.next_check=$next_check RETURN w.id AS id""",
            id=watch["id"], now=_iso(now), error=str(exc)[:500], failures=failures,
            next_check=_iso(now + dt.timedelta(seconds=backoff)))
        return {"ok": False, "message": str(exc), "retry_in": backoff}


def check_due(store_obj=None, observer=None, now=None):
    store_obj = store_obj or _store()
    now = _now(now)
    rows = store_obj.query(
        """MATCH (w:Watch {status:'active'})
        WHERE w.next_check IS NULL OR w.next_check <= $now
        RETURN properties(w) AS watch ORDER BY w.next_check ASC LIMIT 25""",
        now=_iso(now))
    return [check_one(row["watch"]["id"], store_obj, observer, now) for row in rows]


def context_for_agent(store_obj=None, limit=20):
    """Current watch evidence for ordinary and proactive model reasoning.

    Reading state is cheap and does not call a provider.  Observations remain
    the responsibility of the durable worker, so merely assembling a prompt
    cannot spend API quota or move a watch's schedule.
    """
    rows = _rows(store_obj, status="active")[:max(1, min(int(limit), 50))]
    if not rows:
        return ""
    lines = ["--- ACTIVE DURABLE WATCHES ---"]
    for row in rows:
        watch = row.get("watch") or {}
        line = (f"- {watch.get('label') or watch.get('id')} "
                f"[{watch.get('kind')} {watch.get('condition')} "
                f"{watch.get('target') or ''} {watch.get('currency') or ''}]")
        if watch.get("last_value") not in (None, ""):
            line += (f"; last={watch.get('last_value')} at "
                     f"{watch.get('last_checked') or 'unknown'}")
        line += f"; next={watch.get('next_check') or 'due'}"
        if watch.get("last_error"):
            line += f"; error={str(watch.get('last_error'))[:160]}"
        lines.append(line)
    return "\n".join(lines)


def worker(on_alert, store_obj=None, poll_seconds=60, stop_event=None,
           on_observation=None):
    stop_event = stop_event or _STOP
    print("[Watches] Durable condition monitor started.")
    while not stop_event.is_set():
        try:
            import runtime_power
            if not runtime_power.background_allowed():
                stop_event.wait(max(5, poll_seconds))
                continue
            for result in check_due(store_obj=store_obj):
                if result.get("ok") and on_observation:
                    try:
                        on_observation(result)
                    except Exception as exc:
                        print(f"[Watches] Observation listener failed: {exc}")
                if result.get("crossed") and result.get("alert"):
                    on_alert(result["alert"])
        except Exception as exc:
            print(f"[Watches] Scheduler unavailable: {exc}")
        stop_event.wait(max(5, poll_seconds))
