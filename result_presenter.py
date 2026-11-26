"""Turn verbose tool receipts into calm, readable detail-card content.

The original receipt remains available to the agent for reasoning.  This module
only controls the human-facing projection: search engines often append entire
scraped pages after each source, which made a useful five-line answer look like
a terminal dump.
"""

from __future__ import annotations

import re
from urllib.parse import urlparse


_SECTION = re.compile(r"^\s*\[([^\]]+)\]\s*$", re.MULTILINE)
_SOURCE = re.compile(
    r"(?ms)^\s*(\d+)\.\s+(.+?)\s*\n\s*URL:\s*(https?://\S+)(.*?)(?=^\s*\d+\.\s+|\Z)"
)
_FEDERATED_SOURCE = re.compile(
    r"(?ms)^\s*(\d+)\.\s+(.+?)\s*\n"
    r"\s*Source:\s*([^|\n]+?)(?:\s*\|\s*Published:\s*([^\n]+))?\s*\n"
    r"\s*URL:\s*(https?://\S+)"
)

_NON_RESULTS = frozenset({
    "ready", "done", "ok", "okay", "online", "idle", "listening",
    "speaking", "thinking", "working", "working on it", "please wait",
    "loading", "searching", "reading",
})


def display_worthy(value: str) -> bool:
    """Whether *value* is information rather than UI/progress machinery."""
    text = str(value or "").strip()
    if not text:
        return False
    if re.search(
        r"(?im)^\s*(?:result\s*)?\[(?:browser snapshot|browser receipt|browser tabs)\]\s*[\[{]",
        text,
    ):
        return False
    normalized = re.sub(r"[^a-z0-9]+", " ", text.casefold()).strip()
    return normalized not in _NON_RESULTS


def _section(text: str, name: str) -> str:
    match = re.search(
        rf"(?ms)^\s*\[{re.escape(name)}\]\s*\n(.*?)(?=^\s*\[[^\]]+\]\s*$|\Z)",
        text,
    )
    return match.group(1).strip() if match else ""


def _one_line(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def _clean_title(value: str, url: str) -> str:
    """Keep malformed scraped markdown from becoming a giant source title."""
    title = _one_line(value)
    title = re.sub(r"!?\[([^\]]+)\]\([^)]*\)", r"\1", title)
    title = title.strip(" -*#[]()")
    if (not title or len(title) > 150 or title.count("http") > 0
            or title.count("[") + title.count("]") > 2):
        title = urlparse(url).netloc.removeprefix("www.") or "Source"
    return title[:150].rstrip()


def _federated_sources(raw: str) -> list[dict]:
    """Extract cards from KNOWLEDGE_SEARCH's compact federated receipt.

    The line after each URL repeats the headline. It is deliberately ignored;
    showing it made the result window look like an unformatted terminal log.
    """
    sources = []
    for _number, title, provider, published, url in _FEDERATED_SOURCE.findall(raw):
        clean_url = url.rstrip(".,)")
        item = {
            "title": _clean_title(title, clean_url),
            "url": clean_url,
            "source": _one_line(provider),
            "published": _one_line(published),
        }
        if item not in sources:
            sources.append(item)
    return sources


def present(action_type: str, output: str) -> dict:
    """Return title, status, display text and exact links for a result."""
    raw = str(output or "").strip()
    title = action_type.replace("_", " ").title()
    if action_type not in {"SEARCH_WEB", "KNOWLEDGE_SEARCH", "SEARCH_RECALL"}:
        return {"title": title, "status": "RESULT", "text": raw, "links": [],
                "summary": "", "quality": "", "sources": [],
                "synthesized": display_worthy(raw)}

    quality = _one_line(_section(raw, "Evidence quality"))
    summary = _one_line(_section(raw, "Summary"))
    source_block = _section(raw, "Sources")
    sources = []
    for _number, source_title, url, _body in _SOURCE.findall(source_block):
        clean_url = url.rstrip(".,)")
        item = {"title": _clean_title(source_title, clean_url),
                "url": clean_url}
        if item not in sources:
            sources.append(item)
    if not sources:
        sources = _federated_sources(raw)

    lower = summary.casefold()
    incomplete_signals = (
        "do not provide", "does not provide", "no explicit", "no publicly",
        "not found", "insufficient", "unable to", "could not",
    )
    status = "PARTIAL RESULT" if any(s in lower for s in incomplete_signals) else "SEARCH RESULTS"

    lines = []
    if summary:
        lines.extend(("SUMMARY", summary))
    if quality:
        lines.extend(("", "EVIDENCE", quality))
    if sources:
        lines.extend(("", f"SOURCES  ·  {len(sources)}"))
        for index, source in enumerate(sources, 1):
            lines.extend((f"{index:02d}  {source['title']}", source["url"]))
    elif source_block:
        lines.extend(("", "SOURCES", "Source details were returned but could not be structured."))

    return {
        "title": "Web Research",
        "status": status,
        "text": "\n".join(lines).strip() or raw,
        "links": [item["url"] for item in sources],
        "summary": summary,
        "quality": quality,
        "sources": sources,
        # A source list is evidence for the model, not yet the user's answer.
        # Only a real summary is allowed to interrupt the desktop automatically.
        "synthesized": bool(summary),
    }
