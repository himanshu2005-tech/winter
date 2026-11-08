# -*- coding: utf-8 -*-
"""
research_api.py -- one way into web search, shared by every caller.

SEARCH_WEB and DEEP_RESEARCH were two copies of the same forty lines of Tavily
plumbing, differing only in how they formatted the output, so a fix to one
never reached the other. The autonomous researcher needs the same call a third
time, which made consolidating it the only sane option.

Tavily is the primary because it returns extracted article text rather than a
page of links, which is what makes unattended multi-hour research possible at
all -- an agent that has to fetch and strip HTML itself spends its budget on
boilerplate. When Tavily is unreachable the module says so plainly instead of
silently returning nothing, because a researcher that cannot tell "no results"
from "no network" will happily conclude a topic is exhausted.
"""

import os
import re
import time
import json
import hashlib
import threading
import copy
import math
from collections import Counter
from datetime import datetime
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

TAVILY_ENDPOINT = "https://api.tavily.com/search"

# Tavily bills by depth. "basic" is enough to triage whether a question is
# worth pursuing; "advanced" is what actually returns article bodies.
DEPTHS = ("basic", "advanced")

_last_error = {"when": 0.0, "message": ""}
_cache_lock = threading.RLock()
_cache = {}
_CACHE_TTL = int(os.getenv("VANTA_SEARCH_CACHE_SECONDS", "900"))


class SearchUnavailable(RuntimeError):
    """Raised when the search provider cannot be reached at all.

    Distinct from an empty result on purpose: the researcher treats zero
    results as "this question is answered or unanswerable" and stops pursuing
    it, which would be badly wrong if the real cause was a dropped network.
    """


def is_available():
    return bool(os.getenv("TAVILY_API_KEY", "").strip()
                or os.getenv("SERPAPI_API_KEY", "").strip())


def _canonical_url(value):
    """Remove tracking fragments so two providers do not become two sources."""
    try:
        parts = urlsplit(str(value or "").strip())
        if parts.scheme.lower() not in {'http', 'https'} or not parts.hostname:
            return ''
        if parts.username or parts.password:
            return ''
        query = urlencode([(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
                           if not k.lower().startswith('utm_')
                           and k.lower() not in {'gclid', 'fbclid'}])
        return urlunsplit((parts.scheme.lower(), parts.netloc.lower(),
                           parts.path.rstrip("/"), query, ""))
    except Exception:
        return ''


def _cache_key(**values):
    raw = json.dumps(values, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _cached(key):
    with _cache_lock:
        item = _cache.get(key)
        if item and time.time() - item[0] <= _CACHE_TTL:
            result = copy.deepcopy(item[1])
            result["cache_hit"] = True
            return result
        if item:
            _cache.pop(key, None)


def _remember(key, result):
    with _cache_lock:
        _cache[key] = (time.time(), copy.deepcopy(result))
        # A personal assistant does not need an unbounded search cache.
        if len(_cache) > 128:
            for old_key, _ in sorted(_cache.items(), key=lambda row: row[1][0])[:32]:
                _cache.pop(old_key, None)


def _google_search(query, max_results=5, timeout=12):
    """Google organic search through the already-configured SerpAPI account."""
    import requests
    key = os.getenv("SERPAPI_API_KEY", "").strip()
    if not key:
        raise SearchUnavailable("SERPAPI_API_KEY is not configured.")
    response = requests.get(
        "https://serpapi.com/search.json",
        params={"engine": "google", "q": query, "api_key": key,
                "num": max(1, min(int(max_results), 10)), "hl": "en", "gl": "in"},
        timeout=timeout,
    )
    if response.status_code == 429:
        raise SearchUnavailable("Google search is rate limited; retry later.")
    response.raise_for_status()
    payload = response.json()
    if payload.get('error'):
        raise SearchUnavailable('Google search provider reported an error.')
    answer = ""
    box = payload.get("answer_box") or payload.get("knowledge_graph") or {}
    if isinstance(box, dict):
        answer = str(box.get("answer") or box.get("snippet") or box.get("description") or "")
    rows = []
    for position, item in enumerate(payload.get("organic_results") or [], 1):
        rows.append({
            "title": str(item.get("title") or "").strip(),
            "url": str(item.get("link") or "").strip(),
            "content": str(item.get("snippet") or "").strip(),
            "score": max(0.05, 1.0 - (position - 1) * 0.08),
            "provider": "google",
            "published": str(item.get('date') or ''),
            "content_type": "search_snippet",
        })
    return {"answer": answer, "results": rows, "query": query,
            "providers": ["google"]}


_SEARCH_STOPWORDS = set('a an the and or of for to in on with is are what how can do my me please find search get show tell'.split())


def _query_terms(text):
    return set(re.findall(r'[a-z0-9]{2,}', str(text or '').casefold())) - _SEARCH_STOPWORDS


def _domain_allowed(url, include_domains=None, exclude_domains=None):
    host = (urlsplit(url).hostname or '').casefold()
    def matches(domains):
        for domain in domains or ():
            domain = str(domain).strip().casefold()
            domain = (urlsplit(domain).hostname if '://' in domain else domain.split('/')[0]) or ''
            domain = domain.lstrip('.')
            if domain and (host == domain or host.endswith('.' + domain)):
                return True
        return False
    return (not include_domains or matches(include_domains)) and not matches(exclude_domains)


def _merge_results(query, responses, max_results, include_domains=None, exclude_domains=None):
    by_url, answers, providers = {}, [], []
    for response in responses:
        providers.extend(response.get("providers") or [])
        if response.get("answer"):
            answers.append(str(response["answer"]).strip())
        for position, item in enumerate(response.get("results") or [], 1):
            url = _canonical_url(item.get("url"))
            # Hosts are case-insensitive; URL paths and query values are not.
            if not url or not _domain_allowed(url, include_domains, exclude_domains):
                continue
            row = dict(item)
            row["url"] = url
            source = str(row.get('provider') or next(iter(response.get('providers') or []), 'unknown'))
            row['providers'] = [source]
            row['provider_ranks'] = {source: position}
            row['content_type'] = row.get('content_type') or 'extracted_text'
            row['retrieved_at'] = datetime.now().astimezone().isoformat(timespec='seconds')
            existing = by_url.get(url)
            if existing:
                existing['providers'] = sorted(set(existing['providers'] + [source]))
                existing['provider_ranks'][source] = min(position, existing['provider_ranks'].get(source, position))
                if len(str(row.get('content') or '')) > len(str(existing.get('content') or '')):
                    existing['content'] = row['content']
                    existing['content_type'] = row['content_type']
                if not existing.get('published'):
                    existing['published'] = row.get('published', '')
            else:
                by_url[url] = row
    rows = list(by_url.values())
    terms = _query_terms(query)
    documents = [_query_terms(str(r.get('title') or '') + ' ' + str(r.get('content') or '')[:16000]) for r in rows]
    frequencies = Counter(t for document in documents for t in document)
    for row, document in zip(rows, documents):
        matched = terms & document
        relevance = sum(math.log(1 + (len(rows)+1)/(1+frequencies[t])) for t in matched)
        coverage = len(matched) / max(1, len(terms))
        # Provider scores use incompatible scales. Fuse ordinal ranks instead.
        fusion = sum(1 / (60 + rank) for rank in row['provider_ranks'].values())
        row['relevance_terms'] = sorted(matched)
        row['query_coverage'] = round(coverage, 3)
        row['rank_score'] = relevance + coverage + fusion * 10
    rows.sort(key=lambda row: row['rank_score'], reverse=True)
    # Penalize repeated host coverage gently without discarding strong matches.
    selected, counts = [], Counter()
    while rows and len(selected) < max_results:
        best = max(rows, key=lambda row: row['rank_score'] / (1 + .15 * counts[urlsplit(row['url']).hostname]))
        rows.remove(best)
        counts[urlsplit(best['url']).hostname] += 1
        best['source_id'] = f'S{len(selected)+1}'
        selected.append(best)
    return {"answer": answers[0] if answers else "", "results": selected,
            "query": query, "providers": sorted(set(providers)), "cache_hit": False}


def advanced_search(query, depth="advanced", max_results=5, provider="auto",
                    include_raw=True, include_domains=None,
                    exclude_domains=None, timeout=25):
    """Cached, deduplicated web search with Google and Tavily failover.

    ``auto`` keeps latency low: Tavily is preferred for extracted article text;
    Google is used when Tavily is unavailable. ``all`` queries both in parallel
    for research-grade breadth, while ``google`` forces Google results.
    """
    query = " ".join(str(query or "").split())
    provider = str(provider or "auto").lower()
    deadline = time.monotonic() + max(.1, float(timeout))
    if provider not in {'auto', 'all', 'google', 'tavily'}:
        raise ValueError('provider must be auto, all, google, or tavily')
    max_results = max(1, min(int(max_results), 20))
    key = _cache_key(query=query, depth=depth, max=max_results,
                     provider=provider, include=include_domains,
                     exclude=exclude_domains, raw=bool(include_raw),
                     date=datetime.now().date().isoformat())
    hit = _cached(key)
    if hit is not None:
        return hit

    responses, errors = [], []
    google_query = query
    if include_domains:
        sites = [str(domain).strip() for domain in include_domains]
        google_query += ' (' + ' OR '.join('site:' + domain for domain in sites) + ')'
    if exclude_domains:
        google_query += ' ' + ' '.join('-site:' + str(domain).strip() for domain in exclude_domains)
    if provider in {"google"}:
        responses.append(_google_search(google_query, max_results, min(timeout, 15)))
    elif provider == 'all' and not os.getenv('TAVILY_API_KEY', '').strip() and os.getenv('SERPAPI_API_KEY', '').strip():
        responses.append(_google_search(google_query, max_results, min(timeout, 15)))
    elif provider == "all" and os.getenv("SERPAPI_API_KEY", "").strip() \
            and os.getenv("TAVILY_API_KEY", "").strip():
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            jobs = [pool.submit(search, query, depth, max_results, include_raw,
                                include_domains, exclude_domains, timeout),
                    pool.submit(_google_search, google_query, max_results, min(timeout, 15))]
            for job in jobs:
                try:
                    responses.append(job.result())
                except Exception as exc:
                    errors.append(_safe_error(exc))
    else:
        try:
            # Reserve some of the total deadline for failover in auto mode.
            primary_timeout = timeout * .65 if provider == 'auto' and os.getenv('SERPAPI_API_KEY', '').strip() else timeout
            responses.append(search(query, depth, max_results, include_raw,
                                    include_domains, exclude_domains, primary_timeout))
        except Exception as exc:
            errors.append(_safe_error(exc))
            remaining = deadline - time.monotonic()
            if provider == 'auto' and remaining > 0 and os.getenv("SERPAPI_API_KEY", "").strip():
                try:
                    responses.append(_google_search(google_query, max_results, min(remaining, 15)))
                except Exception as fallback_exc:
                    errors.append(_safe_error(fallback_exc))
    if not responses:
        raise SearchUnavailable("; ".join(errors) or "No search provider is configured.")
    if (provider == 'auto' and not any(r.get('results') for r in responses)
            and os.getenv('SERPAPI_API_KEY', '').strip()
            and not any('google' in r.get('providers', []) for r in responses)):
        try:
            remaining = deadline - time.monotonic()
            if remaining > 0:
                responses.append(_google_search(google_query, max_results, min(remaining, 15)))
        except Exception as exc:
            errors.append(_safe_error(exc))
    result = _merge_results(query, responses, max_results, include_domains, exclude_domains)
    result["evidence"] = assess_evidence(result)
    if errors:
        result["provider_warnings"] = errors
    if result['results']:
        _remember(key, result)
    return result


def _safe_error(exc):
    message = str(exc)
    for name in ('TAVILY_API_KEY', 'SERPAPI_API_KEY'):
        secret = os.getenv(name, '').strip()
        if secret:
            message = message.replace(secret, '[redacted]')
    return message[:500]


def search(query, depth="advanced", max_results=5, include_raw=True,
           include_domains=None, exclude_domains=None, timeout=30):
    """Run one search and return normalised results.

    Returns {"answer": str, "results": [{"title", "url", "content", "score"}]}.
    Raises SearchUnavailable if the provider could not be reached.
    """
    import requests

    query = " ".join(str(query or "").split())
    if not query:
        return {"answer": "", "results": [], "query": ""}

    key = os.getenv("TAVILY_API_KEY", "").strip()
    if not key:
        raise SearchUnavailable(
            "TAVILY_API_KEY is not set, so there is no way to search the web. "
            "Add it to .env.")

    payload = {
        "api_key": key,
        "query": query[:380],  # Tavily rejects very long queries outright
        "search_depth": depth if depth in DEPTHS else "advanced",
        "include_answer": True,
        "include_raw_content": bool(include_raw),
        "max_results": max(1, min(int(max_results), 20)),
    }
    if include_domains:
        payload["include_domains"] = list(include_domains)[:20]
    if exclude_domains:
        payload["exclude_domains"] = list(exclude_domains)[:20]

    last_exception = None
    data = None
    deadline = time.monotonic() + max(0.1, float(timeout))
    for attempt in range(3):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            response = requests.post(TAVILY_ENDPOINT, json=payload, timeout=remaining)
            if response.status_code == 429:
                # Rate limited: back off rather than hammering, since the
                # researcher may be running this loop for hours.
                last_exception = SearchUnavailable('Search provider is rate limited.')
                wait = min(2 ** attempt, max(0, deadline - time.monotonic()))
                if wait:
                    time.sleep(wait)
                continue
            response.raise_for_status()
            data = response.json()
            break
        except requests.HTTPError as exc:
            last_exception = exc
            if response.status_code < 500:
                raise SearchUnavailable(f'Search provider rejected request (HTTP {response.status_code}).') from None
        except Exception as exc:
            last_exception = exc
        if attempt < 2:
            time.sleep(min(2 ** attempt, max(0, deadline - time.monotonic())))
    if data is None:
        message = _safe_error(last_exception or SearchUnavailable('Search deadline exceeded.'))
        _last_error.update(when=time.time(), message=message)
        raise SearchUnavailable(f'Search unavailable: {message}')

    results = []
    for item in (data.get("results") or []):
        body = item.get("raw_content") or item.get("content") or ""
        results.append({
            "title": str(item.get("title") or "").strip(),
            "url": str(item.get("url") or "").strip(),
            "content": str(body).strip(),
            "score": float(item.get("score") or 0.0),
            "published": str(item.get('published_date') or item.get('published') or ''),
            "provider": "tavily",
            "content_type": "extracted_text" if item.get('raw_content') else "search_snippet",
        })
    output = {
        "answer": str(data.get("answer") or "").strip(),
        "results": results,
        "query": query,
        "providers": ["tavily"],
    }
    output["evidence"] = assess_evidence(output)
    return output


_MONTHS = {name.lower(): number for number, name in enumerate(
    ("January", "February", "March", "April", "May", "June", "July",
     "August", "September", "October", "November", "December"), 1)}
_MONTHS.update({name[:3]: number for name, number in _MONTHS.items()})


def _dates_in(text):
    """Best-effort date extraction for detecting mismatched snippets."""
    found = set()
    for year, month, day in re.findall(r"\b(20\d{2})[-/](\d{1,2})[-/](\d{1,2})\b", text):
        try:
            found.add(datetime(int(year), int(month), int(day)).date().isoformat())
        except ValueError:
            pass
    month_names = "|".join(sorted(_MONTHS, key=len, reverse=True))
    pattern = rf"\b({month_names})\s+(\d{{1,2}})(?:st|nd|rd|th)?(?:,?\s+(20\d{{2}}))?\b"
    years = re.findall(r"\b(20\d{2})\b", text)
    default_year = int(years[0]) if years else None
    for month, day, year in re.findall(pattern, text, flags=re.I):
        try:
            found.add(datetime(int(year or default_year or datetime.now().year),
                               _MONTHS[month.lower()], int(day)).date().isoformat())
        except (ValueError, TypeError):
            pass
    return found


def assess_evidence(data):
    """Qualify search snippets before an LLM treats them as evidence."""
    query = str(data.get("query") or "")
    requested_dates = _dates_in(query)
    rows, matching = [], 0
    for item in data.get("results") or []:
        text = " ".join((str(item.get("title") or ""),
                         str(item.get("content") or "")))
        dates = _dates_in(text)
        date_match = bool(requested_dates and requested_dates <= dates)
        matching += int(date_match)
        rows.append({"url": str(item.get("url") or ""),
                     "provider_score": float(item.get("score") or 0),
                     "dates": sorted(dates), "date_match": date_match})
    warnings = []
    if requested_dates and rows and not matching:
        warnings.append("No returned source explicitly matches the requested date.")
    if requested_dates and any(not row["date_match"] for row in rows):
        warnings.append("Some results refer to different dates and cannot verify a live price.")
    warnings.append('Search excerpts are source leads, not independently verified claims. Date mentions do not verify deadlines, prices, availability, or publication freshness.')
    return {"requested_dates": sorted(requested_dates), "source_count": len(rows),
            "matching_date_sources": matching,
            "verified_for_requested_date": False,
            "date_mentions_found": bool(requested_dates and matching),
            "verification_status": "source_excerpts_only" if rows else "no_sources",
            "sources": rows, "warnings": warnings}


def format_for_model(data, max_chars=8000, per_result=3000, with_urls=True):
    """Turn a result set into the block a model reads.

    Keeps URLs attached to their content: a research note whose source cannot
    be named later is not evidence, it is a rumour the graph now believes.
    """
    parts = []
    evidence = data.get("evidence") or assess_evidence(data)
    parts.append("[Evidence quality]\n" +
                 f"Independently verified: False; "
                 f"sources mentioning all requested dates: {evidence['matching_date_sources']}/"
                 f"{evidence['source_count']}.\n" + " ".join(evidence["warnings"]))
    answer = data.get("answer")
    if answer:
        parts.append(f"[Summary]\nProvider-generated, unverified: {str(answer)[:min(1000, max_chars//6)]}\n")
    results = data.get("results") or []
    if results:
        parts.append("[Sources]")
        # Reserve space for every citation before filling with long excerpts.
        headers = []
        for index, item in enumerate(results, start=1):
            header = f"{index}. {str(item.get('title') or '')[:240]}"
            if with_urls and item.get('url'):
                header += f"\n   URL: {item['url']}"
            header += f"\n   Source ID: {item.get('source_id') or 'S'+str(index)}; evidence: {item.get('content_type', 'search_excerpt')}; published: {item.get('published') or 'unknown'}"
            headers.append(header)
        available = max(0, max_chars - sum(map(len, parts)) - sum(map(len, headers)) - 100)
        budget = min(per_result, available // max(1, len(results)))
        for item, header in zip(results, headers):
            body = _relevant_passages(item.get('content') or '', data.get('query', ''), budget)
            parts.append(f"{header}\n   {body}\n")
    text = "\n".join(parts).strip()
    if not text:
        return "No results found for this query."
    if len(text) > max_chars:
        text = text[:max_chars] + "\n\n...[truncated for length]"
    return text


def _relevant_passages(content, query, budget):
    """Select query-bearing passages instead of only the first page header."""
    if budget <= 0:
        return ''
    content = str(content)
    if len(content) <= budget:
        return content
    chunks = [content[i:i+500] for i in range(0, min(len(content), 60000), 500)]
    terms = _query_terms(query)
    ranked = sorted(range(len(chunks)), key=lambda i: len(terms & _query_terms(chunks[i])), reverse=True)
    indexes = sorted(ranked[:max(1, budget//500)])
    return '\n[...]\n'.join(chunks[i] for i in indexes)[:budget]


# Pages served as UTF-8 but authored as cp1252 arrive with their punctuation
# mangled. Left alone it reaches the graph inside entity names, where
# "Solomonoff\ufffdKolmogorov" and "Solomonoff-Kolmogorov" become two
# different nodes that never merge.
_MOJIBAKE = {
    "\u00e2\u20ac\u201c": "-", "\u00e2\u20ac\u201d": "-",
    "\u00e2\u20ac\u0153": '"', "\u00e2\u20ac\u009d": '"',
    "\u00e2\u20ac\u2122": "'", "\u00e2\u20ac\u02dc": "'",
    "\u00e2\u20ac\u00a6": "...", "\u00c2\u00a0": " ",
    "\ufffd": "-",
}


def clean_text(value):
    """Repair common encoding damage in scraped page text."""
    text = str(value or "")
    for broken, fixed in _MOJIBAKE.items():
        if broken in text:
            text = text.replace(broken, fixed)
    return text


def corpus(data, per_result=6000):
    """Everything worth storing from a result set, as one block of prose.

    Used by the consolidator, which wants raw material rather than a briefing.
    """
    chunks = []
    if data.get("answer"):
        chunks.append(f"[Summary] {clean_text(data['answer'])}")
    for item in (data.get("results") or []):
        body = clean_text(item["content"][:per_result])
        if not body.strip():
            continue
        source = clean_text(item["title"] or item["url"])
        chunks.append(f"[From {source}]\nURL: {item.get('url', '')}\n{body}")
    return "\n\n".join(chunks)
