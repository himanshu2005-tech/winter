# -*- coding: utf-8 -*-
"""Federated, provenance-preserving access to free knowledge sources.

Every adapter returns the same small record.  A failed source is reported in
``errors`` while healthy sources still answer; transient web data is never
written to memory here.  Promotion into VANTA's graph remains an explicit
research/consolidation decision with evidence and timestamps.
"""
from __future__ import annotations

import concurrent.futures
import datetime as dt
import html
import re
import xml.etree.ElementTree as ET
from difflib import SequenceMatcher
from email.utils import parsedate_to_datetime
from urllib.parse import quote_plus

import requests

USER_AGENT = "VANTA/1.0 personal knowledge assistant"
TIMEOUT = 9
ALL_SOURCES = (
    "openalex", "semantic_scholar", "arxiv", "crossref", "wikidata",
    "hacker_news", "dev", "stack_overflow", "reddit", "google_news",
)
ACADEMIC = ("openalex", "semantic_scholar", "arxiv", "crossref")
TECH = ("hacker_news", "dev", "stack_overflow", "github")
NEWS = ("hacker_news", "reddit", "google_news", "dev")
_STOPWORDS = {"a", "an", "and", "are", "as", "at", "be", "for", "from",
              "how", "in", "is", "it", "of", "on", "or", "the", "to",
              "using", "with", "this", "that", "its", "about", "was", "were",
              "have", "has", "project", "research", "thing", "new", "word",
              "creating", "deciding", "higher", "package", "buy"}
_PRIVATE_NEWS_TERMS = {"health", "medical", "anxiety", "relationship", "partner",
                       "family", "sleep", "loneliness", "password", "account",
                       "financial", "bank", "phone", "address"}
_GENERIC_NEWS_TERMS = {
    "breaking", "current", "headlines", "headline", "latest", "media",
    "news", "now", "stories", "story", "today", "tonight", "trending",
    "update", "updates", "world",
}


class KnowledgeError(RuntimeError):
    pass


def _paper_identity_score(candidate, title, author="", year=""):
    """Score bibliographic identity, not generic topical similarity."""
    def normalized(value):
        value = _clean(value, 300).casefold()
        # Indexes commonly append the publication year to the canonical title.
        value = re.sub(r"\s*[\[(](?:18|19|20)\d{2}[\])]\s*$", "", value)
        return re.sub(r"[^a-z0-9]+", " ", value).strip()

    wanted = normalized(title)
    found = normalized(candidate.get("title"))
    if wanted == found:
        return 1.0
    wanted_tokens, found_tokens = _tokens(wanted), _tokens(found)
    union = wanted_tokens | found_tokens
    token_score = len(wanted_tokens & found_tokens) / max(1, len(union))
    sequence_score = SequenceMatcher(None, wanted, found).ratio()
    # Extra/missing title words matter more than metadata: otherwise a paper
    # *about* the requested work by the same author/year can outrank the work.
    score = 0.85 * token_score + 0.15 * sequence_score
    evidence = (candidate.get("summary") or "").casefold()
    if author and _clean(author, 120).casefold() in evidence:
        score += 0.05
    if year and str(year).strip() in (str(candidate.get("published")) + " " + evidence):
        score += 0.03
    return min(score, 1.0)


def resolve_paper(title, author="", year="", limit=12):
    """Resolve one paper to a provenance-backed canonical destination.

    A weak or near-tied result is refused. Opening nothing is safer than
    silently replacing the requested paper with a topically related work.
    """
    title = " ".join(str(title or "").split())
    if not title:
        raise ValueError("A paper title is required")
    query = " ".join(value for value in (title, str(author or ""), str(year or "")) if value)
    data = search(query, sources=ACADEMIC, limit=max(3, min(int(limit), 20)))
    ranked = []
    for item in data.get("results", []):
        if item.get("kind") != "paper" or not item.get("url"):
            continue
        candidate = dict(item)
        candidate["identity_score"] = _paper_identity_score(
            candidate, title, author=author, year=year)
        ranked.append(candidate)
    ranked.sort(key=lambda item: item["identity_score"], reverse=True)
    if not ranked or ranked[0]["identity_score"] < 0.68:
        raise KnowledgeError("No academic result matched the requested paper identity closely enough")
    if (len(ranked) > 1 and ranked[1]["identity_score"] >= 0.68
            and ranked[0]["identity_score"] - ranked[1]["identity_score"] < 0.04
            and ranked[0]["url"] != ranked[1]["url"]):
        raise KnowledgeError("Multiple academic records matched too closely; title, author, or year is needed")
    return {"match": ranked[0], "alternatives": ranked[1:4],
            "errors": data.get("errors", {})}


def _get(url, *, params=None, headers=None):
    merged = {"User-Agent": USER_AGENT, "Accept": "application/json, application/atom+xml, application/rss+xml, application/xml, text/xml"}
    merged.update(headers or {})
    response = requests.get(url, params=params, headers=merged, timeout=TIMEOUT)
    response.raise_for_status()
    return response


def _clean(value, limit=900):
    text = html.unescape(re.sub(r"<[^>]+>", " ", str(value or "")))
    return re.sub(r"\s+", " ", text).strip()[:limit]


def _tokens(value):
    return {word for word in re.findall(r"[a-z0-9]+", str(value).lower())
            if (len(word) > 2 or word in {"ai", "ml", "cs"})
            and word not in _STOPWORDS}


def _row(source, title, url, summary="", published="", score=0.5, kind="knowledge"):
    return {"source": source, "title": _clean(title, 240), "url": str(url or "").strip(),
            "summary": _clean(summary), "published": str(published or "")[:40],
            "score": max(0.0, min(float(score or 0), 1.0)), "kind": kind,
            "retrieved_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")}


def _published_datetime(value):
    """Best-effort UTC timestamp for RSS, Atom and ISO publication values."""
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = parsedate_to_datetime(text)
    except (TypeError, ValueError, OverflowError):
        try:
            parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
        except (TypeError, ValueError, OverflowError):
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def _news_topic_tokens(query):
    """Terms that make a news request topical rather than a general briefing."""
    return _tokens(query) - _GENERIC_NEWS_TERMS


def _is_broad_news_request(query):
    return not _news_topic_tokens(query)


def _score_news_freshness(item, now=None):
    """Attach recency evidence and make current reports outrank stale ones."""
    now = now or dt.datetime.now(dt.timezone.utc)
    published = _published_datetime(item.get("published"))
    if published is None:
        item["age_hours"] = None
        return item
    age_hours = max(0.0, (now - published).total_seconds() / 3600.0)
    item["age_hours"] = round(age_hours, 1)
    if age_hours <= 30:
        item["score"] = min(1.0, float(item.get("score", 0)) + 0.22)
    elif age_hours <= 72:
        item["score"] = min(1.0, float(item.get("score", 0)) + 0.12)
    elif age_hours > 24 * 14:
        item["score"] = max(0.0, float(item.get("score", 0)) - 0.30)
    return item


def _openalex_key():
    try:
        from security.keyring_manager import get_secret_chunked
        return get_secret_chunked("WINTER_SYSTEM", "openalex_api_key") or ""
    except Exception:
        return ""


def openalex(query, limit=5):
    params = {"search": query, "per-page": limit,
              "select": "id,doi,title,publication_year,authorships,primary_location,cited_by_count"}
    key = _openalex_key()
    if key:
        params["api_key"] = key
    try:
        data = _get("https://api.openalex.org/works", params=params).json()
    except requests.HTTPError as exc:
        # OpenAlex still permits a small anonymous budget. A mistyped/rotated
        # key should reduce capacity, not remove the source from every answer.
        if key and getattr(exc.response, "status_code", None) == 401:
            params.pop("api_key", None)
            data = _get("https://api.openalex.org/works", params=params).json()
        else:
            raise
    rows = []
    for item in data.get("results", []):
        authors = ", ".join(a.get("author", {}).get("display_name", "")
                            for a in item.get("authorships", [])[:4])
        landing = (item.get("primary_location") or {}).get("landing_page_url")
        rows.append(_row("OpenAlex", item.get("title"), landing or item.get("doi") or item.get("id"),
                         f"{authors}; {item.get('publication_year')}; cited by {item.get('cited_by_count', 0)}",
                         item.get("publication_year"), 0.9, "paper"))
    return rows


def semantic_scholar(query, limit=5):
    data = _get("https://api.semanticscholar.org/graph/v1/paper/search",
                params={"query": query, "limit": limit,
                        "fields": "title,url,abstract,authors,year,citationCount,publicationDate"}).json()
    return [_row("Semantic Scholar", item.get("title"), item.get("url"),
                 item.get("abstract") or f"{item.get('citationCount', 0)} citations",
                 item.get("publicationDate") or item.get("year"), 0.88, "paper")
            for item in data.get("data", [])]


def arxiv(query, limit=5):
    response = _get("https://export.arxiv.org/api/query",
                    params={"search_query": f"all:{query}", "start": 0,
                            "max_results": limit, "sortBy": "relevance"})
    root = ET.fromstring(response.content)
    ns = {"a": "http://www.w3.org/2005/Atom"}
    rows = []
    for entry in root.findall("a:entry", ns):
        rows.append(_row("arXiv", entry.findtext("a:title", "", ns),
                         entry.findtext("a:id", "", ns),
                         entry.findtext("a:summary", "", ns),
                         entry.findtext("a:published", "", ns), 0.86, "paper"))
    return rows


def crossref(query, limit=5):
    data = _get("https://api.crossref.org/works",
                params={"query": query, "rows": limit,
                        "select": "DOI,title,author,abstract,published,URL"}).json()
    rows = []
    for item in data.get("message", {}).get("items", []):
        title = (item.get("title") or [""])[0]
        parts = (item.get("published") or {}).get("date-parts") or [[]]
        published = "-".join(str(x) for x in parts[0])
        rows.append(_row("Crossref", title, item.get("URL"), item.get("abstract"),
                         published, 0.82, "paper"))
    return rows


def wikidata(query, limit=5):
    data = _get("https://www.wikidata.org/w/api.php", params={
        "action": "wbsearchentities", "search": query, "language": "en",
        "format": "json", "limit": limit, "origin": "*"}).json()
    return [_row("Wikidata", item.get("label"), item.get("concepturi"),
                 item.get("description"), score=0.8, kind="entity")
            for item in data.get("search", [])]


def hacker_news(query, limit=5):
    ids = _get("https://hacker-news.firebaseio.com/v0/topstories.json").json()[:30]
    words = _tokens(query)
    rows = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as pool:
        futures = [pool.submit(_get, f"https://hacker-news.firebaseio.com/v0/item/{item}.json") for item in ids]
        for future in concurrent.futures.as_completed(futures):
            try:
                item = future.result().json() or {}
            except Exception:
                continue
            title = item.get("title", "")
            overlap = len(words & _tokens(title))
            if words and not overlap:
                continue
            rows.append(_row("Hacker News", title,
                             item.get("url") or f"https://news.ycombinator.com/item?id={item.get('id')}",
                             f"{item.get('score', 0)} points; {item.get('descendants', 0)} comments",
                             dt.datetime.fromtimestamp(item.get("time", 0), dt.timezone.utc).isoformat(),
                             min(1, 0.45 + item.get("score", 0) / 500), "news"))
    return sorted(rows, key=lambda x: x["score"], reverse=True)[:limit]


def dev(query, limit=5):
    # DEV's public endpoint has no full-text search. The first meaningful word
    # is used as a tag only when it is tag-shaped; otherwise current top posts
    # are filtered locally.
    tag = next((w for w in re.findall(r"[a-z0-9]+", query.lower()) if len(w) > 2), "")
    params = {"per_page": min(30, max(limit * 4, 10)), "top": 7}
    if tag:
        params["tag"] = tag
    items = _get("https://dev.to/api/articles", params=params).json()
    return [_row("DEV Community", item.get("title"), item.get("url"),
                 item.get("description"), item.get("published_at"),
                 min(1, 0.45 + item.get("positive_reactions_count", 0) / 500), "article")
            for item in items[:limit]]


def stack_overflow(query, limit=5):
    data = _get("https://api.stackexchange.com/2.3/search/advanced", params={
        "site": "stackoverflow", "q": query, "pagesize": limit,
        "order": "desc", "sort": "relevance", "filter": "default"}).json()
    return [_row("Stack Overflow", item.get("title"), item.get("link"),
                 f"score {item.get('score', 0)}; {item.get('answer_count', 0)} answers; "
                 f"accepted answer: {bool(item.get('accepted_answer_id'))}",
                 dt.datetime.fromtimestamp(item.get("creation_date", 0), dt.timezone.utc).isoformat(),
                 min(1, 0.55 + max(item.get("score", 0), 0) / 200), "qa")
            for item in data.get("items", [])]


def _rss(url, source, limit=5):
    root = ET.fromstring(_get(url).content)
    rows = []
    for item in root.findall(".//item")[:limit]:
        rows.append(_row(source, item.findtext("title"), item.findtext("link"),
                         item.findtext("description"), item.findtext("pubDate"),
                         0.65, "news"))
    # Reddit and some feeds return Atom rather than RSS.
    ns = {"a": "http://www.w3.org/2005/Atom"}
    for entry in root.findall("a:entry", ns)[:limit - len(rows)]:
        link = entry.find("a:link", ns)
        rows.append(_row(source, entry.findtext("a:title", "", ns),
                         link.get("href", "") if link is not None else "",
                         entry.findtext("a:content", "", ns),
                         entry.findtext("a:updated", "", ns), 0.6, "discussion"))
    return rows


def reddit(query, limit=5):
    return _rss(f"https://www.reddit.com/search.rss?q={quote_plus(query)}&sort=relevance", "Reddit", limit)


def google_news(query, limit=5):
    if _is_broad_news_request(query):
        url = "https://news.google.com/rss?hl=en-IN&gl=IN&ceid=IN:en"
    else:
        url = ("https://news.google.com/rss/search?q=" + quote_plus(query)
               + "&hl=en-IN&gl=IN&ceid=IN:en")
    return _rss(url, "Google News", limit)


ADAPTERS = {"openalex": openalex, "semantic_scholar": semantic_scholar,
            "arxiv": arxiv, "crossref": crossref, "wikidata": wikidata,
            "hacker_news": hacker_news, "dev": dev,
            "stack_overflow": stack_overflow, "reddit": reddit,
            "google_news": google_news}


def _default_sources(query, purpose="knowledge"):
    text = query.lower()
    if purpose == "news":
        return NEWS
    if any(word in text for word in ("paper", "research", "study", "citation", "academic", "algorithm", "model")):
        return ACADEMIC + ("wikidata", "hacker_news")
    if any(word in text for word in ("code", "python", "javascript", "error", "programming", "developer")):
        return ("stack_overflow", "dev", "hacker_news", "wikidata")
    return ("openalex", "wikidata", "hacker_news", "google_news")


def search(query, sources=None, limit=5, purpose="knowledge"):
    query = " ".join(str(query or "").split())
    if not query:
        raise ValueError("A knowledge query is required")
    chosen = list(sources or _default_sources(query, purpose))
    chosen = [name for name in dict.fromkeys(chosen) if name in ADAPTERS]
    results, errors = [], {}
    per_source_limit = max(limit * 3, 15) if purpose == "news" else limit
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(6, len(chosen) or 1)) as pool:
        pending = {pool.submit(ADAPTERS[name], query, per_source_limit): name for name in chosen}
        for future in concurrent.futures.as_completed(pending):
            name = pending[future]
            try:
                results.extend(future.result())
            except Exception as exc:
                errors[name] = f"{type(exc).__name__}: {exc}"[:240]
    terms = _tokens(query)
    topic_terms = _news_topic_tokens(query) if purpose == "news" else terms
    broad_news = purpose == "news" and not topic_terms
    for item in results:
        words = _tokens(item["title"] + " " + item["summary"])
        matched = topic_terms & words
        overlap = len(matched)
        item["relevance_terms"] = overlap
        item["relevance_tokens"] = sorted(matched)
        item["score"] = min(1, item["score"] + min(0.24, overlap * 0.045))
        if purpose == "news":
            _score_news_freshness(item)
    if purpose == "news" and topic_terms:
        # A topical briefing stays topical. One meaningful overlap is enough:
        # requiring two discarded valid single-topic headlines (for example a
        # story whose title says only "internship" while its publisher carries
        # the remaining context).
        results = [item for item in results
                   if item.get("relevance_terms", 0) >= 1
                   or (item.get("source") == "Hacker News" and item.get("score", 0) >= 0.85)]
    selection = "broad_current_news" if broad_news else (
        "topic_relevance_and_freshness" if purpose == "news" else "relevance")
    deduped, seen = [], set()
    def rank(row):
        # A general briefing should primarily be a news briefing. Community
        # feeds remain useful fallbacks, but do not outrank a current report
        # from the actual news feed merely through engagement score.
        source_bonus = .08 if broad_news and row.get("source") == "Google News" else 0
        age = row.get("age_hours")
        freshness = .04 if isinstance(age, (int, float)) and age <= 30 else 0
        return float(row.get("score", 0)) + source_bonus + freshness

    for item in sorted(results, key=rank, reverse=True):
        key = re.sub(r"\W+", "", item["title"].casefold())[:120] or item["url"]
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return {"query": query, "sources": chosen, "selection": selection,
            "results": deduped[:max(1, min(int(limit), 30))],
            "errors": errors, "retrieved_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")}


def personal_interests(base_dir, limit=7):
    """Read only non-sensitive goal/study/project topics for news routing."""
    fallback = ["artificial intelligence", "software engineering", "student internships",
                "technology startups", "Chennai"]
    try:
        from memory.neo4j_store import store
        rows = store.query(
            """MATCH (s:Entity)-[r:FACT]->(o:Entity)
            WHERE toLower(coalesce(s.name,s.id,''))='himanshu'
              AND coalesce(r.valid_to,'')=''
              AND coalesce(r.sensitivity,'private') IN ['public','private']
              AND coalesce(r.predicate,'') IN
                  ['HAS_GOAL','IS_WORKING_ON','STUDIES_AT','HAS_INTEREST','PREFERS']
            RETURN coalesce(o.name,o.value,o.id) AS topic,
                   coalesce(r.confidence,0.5) AS confidence
            ORDER BY confidence DESC LIMIT $limit""", limit=int(limit))
        topics = []
        for row in rows:
            topic = _clean(row.get("topic"), 70)
            # Do not send contact details, identifiers, or sentence-sized
            # personal disclosures to public feeds as search terms.
            topic_tokens = _tokens(topic.replace("_", " "))
            if (3 <= len(topic) <= 70 and "@" not in topic
                    and not re.search(r"\b\d{7,}\b", topic)
                    and not (topic_tokens & _PRIVATE_NEWS_TERMS)):
                topics.append(topic)
        return topics[:limit] or fallback
    except Exception:
        return fallback


def personalized_news(base_dir, limit=6):
    interests = personal_interests(base_dir)
    # Graph objects can be sentence-sized ("a project creating..."). Feeds
    # interpret those as one impossible query. Reduce them to distinctive
    # terms, then retain broad student/technology discovery as requested.
    ordered = []
    for interest in interests:
        for token in sorted(_tokens(interest.replace("_", " "))):
            if token not in ordered:
                ordered.append(token)
    ordered = ordered[:8]
    interest_query = " OR ".join(ordered) or "student career technology"
    requested = max(1, min(int(limit), 30))
    # A useful daily briefing needs both situational awareness and personal
    # relevance.  Searching only graph interests made every briefing look like
    # an AI feed; searching only headlines made it generic.  Retrieve both and
    # merge them with provenance intact.
    broad = search("latest news today", sources=("google_news",),
                   purpose="news", limit=max(3, requested))
    personal = search(interest_query, purpose="news", limit=max(3, requested))
    for item in broad.get("results", []):
        item["news_scope"] = "general"
    for item in personal.get("results", []):
        item["news_scope"] = "personal"

    merged, seen = [], set()
    pools = [iter(broad.get("results", [])), iter(personal.get("results", []))]
    while len(merged) < requested and pools:
        remaining = []
        for pool in pools:
            try:
                item = next(pool)
            except StopIteration:
                continue
            key = re.sub(r"\W+", "", item.get("title", "").casefold())[:120]
            if key and key not in seen:
                seen.add(key)
                merged.append(item)
                if len(merged) >= requested:
                    break
            remaining.append(pool)
        pools = remaining

    errors = dict(broad.get("errors", {}))
    errors.update(personal.get("errors", {}))
    return {
        "query": f"current briefing + interests: {interest_query}",
        "sources": list(dict.fromkeys(
            list(broad.get("sources", [])) + list(personal.get("sources", [])))),
        "selection": "balanced_general_and_personal",
        "results": merged,
        "errors": errors,
        "retrieved_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "interests": interests,
    }


def news_context(base_dir, limit=6):
    data = personalized_news(base_dir, limit=limit)
    lines = ["--- CURRENT NEWS BRIEFING ---",
             "Matched interests: " + ", ".join(data.get("interests", []))]
    for item in data.get("results", []):
        scope = item.get("news_scope", "current").upper()
        lines.append(f"- [{scope}] {item['title']} ({item['source']})\n  {item['url']}")
    if not data.get("results"):
        lines.append("No relevant current stories were verified.")
    if data.get("errors"):
        lines.append("Sources temporarily unavailable: " + ", ".join(sorted(data["errors"])))
    return "\n".join(lines)


def format_for_model(data, max_chars=10000):
    rows = [f"[Federated knowledge: {data['query']}]",
            f"Sources queried: {', '.join(data['sources'])}",
            f"Selection: {data.get('selection', 'relevance')}",
            f"Retrieved: {data['retrieved_at']}"]
    for index, item in enumerate(data.get("results", []), 1):
        rows.append(f"\n{index}. {item['title']}\nSource: {item['source']} | Published: {item['published'] or 'unknown'}\nURL: {item['url']}\n{item['summary']}")
    if not data.get("results"):
        rows.append("No matching results were returned.")
    if data.get("errors"):
        rows.append("\nUnavailable sources: " + ", ".join(sorted(data["errors"])))
    return "\n".join(rows)[:max_chars]
