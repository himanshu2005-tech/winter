# -*- coding: utf-8 -*-
"""
api_index.py -- a searchable directory of free APIs, not a catalogue of them.

The public-apis list carries 913 entries that need no key. Written into the
prompt as actions that would be roughly 274,000 characters on every single turn,
against the 22,500 assistant mode already spends. It would also be the wrong
move even if it were free, because prompt bloat is the measured failure mode
here: a model that scored 6/6 on a clean prompt failed in the real system, and
invented BROWSER_SEARCH twice while holding the browser catalogue. Nine hundred
more tools would buy capability it was then too crowded to use.

So the list is data. One action searches it, and CREATE_ACTION turns whichever
entry fits into a real, smoke-tested tool that joins the dispatcher permanently.
The prompt grows by a line; the toolset grows only where something needed it.

    need -> FIND_API -> CREATE_ACTION -> a permanent action

Refreshed from GitHub on demand and cached on disk, so a flaky connection does
not mean an empty directory.
"""

import json
import os
import re
import threading
import ipaddress
import socket
from urllib.parse import urlparse

SOURCE_URL = ("https://raw.githubusercontent.com/public-apis/public-apis/"
              "master/README.md")

_lock = threading.RLock()
_cache = {"entries": None, "path": None}

INDEX_VERSION = 2

# High-value official Indian sources are not all represented by public-apis.
# These are catalog records, not hard-coded answers or executable endpoints:
# VANTA still has to inspect their documentation and obtain the required grant.
CURATED_APIS = (
    {"name": "Open Government Data Platform India", "url": "https://data.gov.in/",
     "description": "Official Government of India open-data catalog and resource APIs",
     "category": "Government", "auth": "apiKey", "https": True,
     "cors": False, "official": True, "region": "IN", "source": "vanta_curated"},
    {"name": "API Setu", "url": "https://apisetu.gov.in/",
     "description": "Government of India platform for discovering and accessing public-service APIs",
     "category": "Government", "auth": "OAuth", "https": True,
     "cors": False, "official": True, "region": "IN", "source": "vanta_curated"},
    {"name": "RBI Database on Indian Economy", "url": "https://data.rbi.org.in/",
     "description": "Official Reserve Bank of India statistics and downloadable time-series data",
     "category": "Government", "auth": "No", "https": True,
     "cors": False, "official": True, "region": "IN", "source": "vanta_curated"},
)

_OFFICIAL_HOST_MARKERS = (
    ".gov", ".gov.in", ".nic.in", ".mil", ".europa.eu",
    "worldbank.org", "who.int", "un.org", "rbi.org.in",
)

_ROW = re.compile(
    r"^\|\s*\[([^\]]+)\]\(([^)]+)\)\s*\|([^|]*)\|([^|]*)\|([^|]*)\|([^|]*)\|",
    re.M)
_HEADING = re.compile(r"^###\s+(.+?)\s*$", re.M)


def index_path(base_dir):
    return os.path.join(base_dir, "vault", "system", "api_index.json")


def _parse(markdown):
    """Rows with their category, which the table alone does not carry."""
    # Walk headings and tables together so each row knows its section. The
    # category is most of what makes a search useful -- "weather" matches a
    # heading long before it matches any one description.
    boundaries = [(m.start(), m.group(1)) for m in _HEADING.finditer(markdown)]
    entries = []
    for match in _ROW.finditer(markdown):
        name, link, description, auth, https, cors = match.groups()
        category = ""
        for start, heading in boundaries:
            if start < match.start():
                category = heading
            else:
                break
        from urllib.parse import urlparse
        host = (urlparse(link.strip()).hostname or "").lower()
        official = any(marker in host or host.endswith(marker.lstrip("."))
                       for marker in _OFFICIAL_HOST_MARKERS)
        description_clean = " ".join(description.split())
        location_text = f"{name} {description_clean}".lower()
        region = "IN" if any(word in location_text for word in
                             ("india", "indian", "government of india")) else "GLOBAL"
        entries.append({
            "name": name.strip(),
            "url": link.strip(),
            "description": description_clean,
            "category": category,
            "auth": auth.strip().strip('`') or "No",
            "https": https.strip().lower() == "yes",
            "cors": cors.strip().lower() == "yes",
            "official": official,
            "region": region,
            "source": "public-apis/public-apis",
        })
    return entries


def refresh(base_dir, timeout=25):
    """Fetch the list and write the index. Returns a receipt."""
    try:
        import requests
        response = requests.get(SOURCE_URL, timeout=timeout,
                                headers={"User-Agent": "VANTA/1.0"})
        response.raise_for_status()
        entries = _parse(response.text)
        existing_urls = {e["url"].rstrip("/").casefold() for e in entries}
        entries.extend(dict(item) for item in CURATED_APIS
                       if item["url"].rstrip("/").casefold() not in existing_urls)
    except Exception as exc:
        return f"[Error] Could not refresh the API index: {exc}"

    if not entries:
        return "[Error] The API list parsed to nothing; leaving the index alone."

    path = index_path(base_dir)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    payload = {"schema_version": INDEX_VERSION, "source": SOURCE_URL,
               "count": len(entries), "entries": entries}
    # Written whole then moved, so a dropped connection cannot leave a
    # half-written index behind.
    temporary = path + ".tmp"
    with open(temporary, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, indent=1, ensure_ascii=False)
    os.replace(temporary, path)

    with _lock:
        _cache["entries"] = entries
        _cache["path"] = path

    categories = len({e["category"] for e in entries})
    no_auth = sum(1 for e in entries if str(e.get("auth", "No")).lower() in ("", "no"))
    return (f"[Success] Indexed {len(entries)} APIs across {categories} categories "
            f"({no_auth} need no credentials).")


def load(base_dir):
    """The cached index, or an empty list if it has never been built."""
    with _lock:
        if _cache["entries"] is not None and _cache["path"] == index_path(base_dir):
            return _cache["entries"]
    path = index_path(base_dir)
    if not os.path.exists(path):
        return []
    try:
        with open(path, encoding="utf-8") as handle:
            entries = json.load(handle).get("entries") or []
    except Exception:
        return []
    with _lock:
        _cache["entries"] = entries
        _cache["path"] = path
    return entries


def _score(entry, words, region="", prefer_official=True):
    """Keyword overlap, weighted by where the word appears.

    Deliberately not embeddings. This runs inside a turn, the corpus is under a
    thousand short rows, and a dependency that has to warm a model before it can
    answer "weather" is the wrong shape for that.
    """
    name = entry["name"].lower()
    category = entry["category"].lower()
    description = entry["description"].lower()
    score = 0
    for word in words:
        if word in name:
            score += 5
        if word in category:
            score += 3
        if word in description:
            score += 2
    # Preference signals only break ties between relevant candidates. Applying
    # them to every row made any HTTPS/key-free service match an unrelated
    # query such as "quantum tunnelling".
    if score <= 0:
        return 0
    if prefer_official and entry.get("official"):
        score += 3
    wanted_region = str(region or "").upper()
    if wanted_region and entry.get("region", "GLOBAL").upper() == wanted_region:
        score += 8
    # HTTPS is a modest tie-breaker, never a substitute for relevance.
    if entry.get("https"):
        score += 0.5
    if str(entry.get("auth", "No") or "No").lower() in ("", "no"):
        score += 1
    return score


def search(base_dir, need, limit=5, auth="any", category="", region="",
           official_only=False):
    """Entries most likely to serve *need*, best first."""
    entries = load(base_dir)
    if not entries:
        return []
    words = [w for w in re.split(r"\W+", str(need or "").lower()) if len(w) > 2]
    if not words:
        return []
    wanted_auth = str(auth or "any").lower()
    wanted_category = str(category or "").lower()
    filtered = []
    for entry in entries:
        entry_auth = str(entry.get("auth", "No") or "No").lower()
        if wanted_auth in ("none", "no", "key-free", "key_free") and entry_auth not in ("", "no"):
            continue
        if wanted_auth in ("required", "key", "oauth") and entry_auth in ("", "no"):
            continue
        if wanted_category and wanted_category not in entry.get("category", "").lower():
            continue
        if official_only and not entry.get("official", False):
            continue
        filtered.append(entry)
    scored = [(s, e) for s, e in
              ((_score(e, words, region=region), e) for e in filtered) if s]
    scored.sort(key=lambda pair: (-pair[0], pair[1]["name"]))
    return [entry for _score_value, entry in scored[:limit]]


def describe(base_dir, need, limit=5, auth="any", category="", region="",
             official_only=False):
    """What the model reads back from FIND_API."""
    entries = load(base_dir)
    if not entries:
        return ("[Error] The API directory has not been built yet. Run "
                '<ACTION type="FIND_API" refresh="true" need="..." /> once to '
                "fetch it.")

    hits = search(base_dir, need, limit=limit, auth=auth, category=category,
                  region=region, official_only=official_only)
    if not hits:
        return (f"No API in the directory looks like a match for "
                f"'{need}'. {len(entries)} entries were searched. Either the "
                "capability needs a key, or it is worth solving with "
                "RUN_PYTHON instead.")

    lines = [f"{len(hits)} candidate(s) for '{need}', out of "
             f"{len(entries)} cataloged APIs:"]
    for entry in hits:
        lines.append(
            f"- {entry['name']} ({entry['category']}): {entry['description']}"
            f"\n  {entry['url']}"
            + ("  [https]" if entry["https"] else "  [http only]")
            + f" [auth: {entry.get('auth', 'No')}]"
            + (" [official]" if entry.get("official") else "")
            + (f" [region: {entry.get('region')}]" if entry.get("region") else ""))
    lines.append(
        "Catalog entries are leads, not verified integrations. Inspect the official "
        "documentation, authentication, terms, and response schema before use. Then "
        'write the tool with CREATE_ACTION so it is available next time too -- it is '
        "validated and smoke-tested before it installs. Use RUN_PYTHON "
        "instead only if this is genuinely a one-off.")
    return "\n".join(lines)


def status(base_dir):
    entries = load(base_dir)
    if not entries:
        return "[Attention] The API catalog has not been built yet."
    no_auth = sum(1 for e in entries if str(e.get("auth", "No")).lower() in ("", "no"))
    official = sum(1 for e in entries if e.get("official"))
    india = sum(1 for e in entries if e.get("region") == "IN")
    categories = len({e.get("category", "") for e in entries})
    return (f"[Success] API catalog: {len(entries)} entries across {categories} "
            f"categories; {no_auth} key-free, {official} official-source, "
            f"{india} India-relevant.")


def check_candidate(base_dir, name, timeout=10):
    """Check catalog documentation reachability, never an arbitrary URL.

    This deliberately does not call an API endpoint or claim the service works;
    the catalog contains documentation homepages.  DNS is rejected if it maps
    to a private/special network, preventing the catalog from becoming SSRF.
    """
    wanted = str(name or "").strip().casefold()
    matches = [e for e in load(base_dir) if e.get("name", "").casefold() == wanted]
    if not matches:
        return f"[Error] No exact API catalog entry named {name!r}. Use FIND_API first."
    entry = matches[0]
    parsed = urlparse(entry.get("url", ""))
    if parsed.scheme != "https" or not parsed.hostname:
        return f"[Blocked] {entry['name']} does not provide an HTTPS documentation URL."
    try:
        addresses = {info[4][0] for info in socket.getaddrinfo(parsed.hostname, 443)}
        if any(ipaddress.ip_address(address).is_private
               or ipaddress.ip_address(address).is_loopback
               or ipaddress.ip_address(address).is_link_local
               or ipaddress.ip_address(address).is_reserved
               for address in addresses):
            return "[Blocked] Catalog hostname resolved to a private or special network."
        import requests
        response = requests.head(entry["url"], timeout=max(2, min(int(timeout), 20)),
                                 allow_redirects=False,
                                 headers={"User-Agent": "VANTA/1.0"})
        reachable = response.status_code < 500
        label = "reachable" if reachable else "unavailable"
        return (f"[{'Success' if reachable else 'Attention'}] {entry['name']} "
                f"documentation is {label} (HTTP {response.status_code}). "
                f"Auth: {entry.get('auth', 'No')}. This verifies documentation "
                "reachability, not endpoint correctness or terms.")
    except Exception as exc:
        return f"[Attention] Could not reach {entry['name']} documentation: {exc}"
