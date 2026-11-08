# -*- coding: utf-8 -*-
"""
brain_map.py -- the knowledge graph, shaped for looking at.

The graph holds facts, not pictures: ``Entity`` nodes joined by ``FACT`` edges
that carry a predicate, a confidence, a provenance and an epistemic class.
Drawn raw it is a hairball, and a misleading one -- every phone number and
email address is its own node, so 142 contacts become several hundred dots of
which most are digits. This module decides what a viewer should see:

* attributes (phone, email) are folded into their owner and shown on click;
* each node gets a group (person, place, org, project, interest, ...) from what
  is known about it, so the colours mean something;
* every edge says where it came from -- stated by him, imported from contacts,
  learned while idle, deduced, or linked across silos -- because "what did it
  work out on its own?" is the question this view exists to answer;
* anything recent is marked, so a fresh discovery is visible as it appears.

Pure functions over query rows, so the shaping is tested without Neo4j.
"""

import datetime as _dt
import re

#: Folded into the node that owns them instead of becoming nodes themselves.
ATTRIBUTE_PREDICATES = {"HAS_PHONE", "PRIMARY_EMAIL", "HAS_EMAIL", "HAS_ALIAS"}

MAX_EDGES = 8000
MAX_NODES = 3000
RECENT_DAYS = 3

#: Predicate -> the group of the OBJECT, and of the subject where stated.
_OBJECT_GROUP = {
    "LIVES_IN": "place", "CURRENT_LOCATION": "place", "TRAVEL_TO": "place",
    "WORKS_AT": "org", "STUDIES_AT": "org", "AFFILIATED_WITH": "org",
    "HAS_PREFERENCE": "interest", "HAS_GOAL": "goal", "VALUES_PRIORITY": "interest",
    "IS_WORKING_ON": "project", "SELF_REPORTS_PATTERN": "habit",
    "HAS_FATHER": "person", "HAS_MOTHER": "person", "HAS_SISTER": "person",
    "HAS_BROTHER": "person", "HAS_PARTNER": "person", "HAS_CONTACT": "person",
    "CURRENT_ROLE": "role",
}
_SUBJECT_GROUP = {"HAS_PHONE": "person", "PRIMARY_EMAIL": "person",
                  "HAS_CONTACT": "person"}

GROUP_COLORS = {
    "you": "#ffd166", "person": "#4cc9f0", "place": "#06d6a0", "org": "#b794f6",
    "project": "#ff9f1c", "interest": "#f72585", "goal": "#ef476f",
    "habit": "#80ed99", "role": "#a0aec0", "discovery": "#38bdf8",
    "entity": "#8d99ae",
}

#: Where an edge came from, read off its ``source_id`` prefix.
ORIGINS = ("stated", "contacts", "curiosity", "deduction", "linking", "other")


def origin_of(source_id, fact_class=""):
    source = str(source_id or "")
    if source.startswith("curiosity:"):
        return "curiosity"
    if source.startswith("deduction:"):
        return "deduction"
    if source.startswith("linking:"):
        return "linking"
    if source.startswith("google_contacts"):
        return "contacts"
    if fact_class in ("explicit_user_fact", "explicit_preference") or \
            source in ("user_stated", "user"):
        return "stated"
    return "other"


def _is_recent(stamp, now, days=RECENT_DAYS):
    text = str(stamp or "")
    if not text:
        return False
    try:
        when = _dt.datetime.fromisoformat(text.replace("Z", "+00:00")[:32])
    except ValueError:
        return False
    if when.tzinfo is not None:
        when = when.astimezone(_dt.timezone.utc).replace(tzinfo=None)
    return (now - when) <= _dt.timedelta(days=days)


def build_graph(rows, hub="himanshu", now=None):
    """Nodes and links for the viewer from FACT-edge rows.

    Each row: skey, subject, stype, okey, object, otype, predicate,
    confidence, source_id, fact_class, review_status, created.
    """
    now = now or _dt.datetime.utcnow()
    nodes, links, attributes = {}, [], {}
    predicates_of = {}

    def node(key, name, kind):
        entry = nodes.get(key)
        if entry is None:
            entry = nodes[key] = {"id": key, "name": name or key,
                                  "kind": kind or "", "group": "entity",
                                  "degree": 0, "origins": set(), "recent": False}
        return entry

    for row in rows:
        predicate = str(row.get("predicate") or "")
        subject = node(row["skey"], row.get("subject"), row.get("stype"))
        origin = origin_of(row.get("source_id"), row.get("fact_class"))
        recent = _is_recent(row.get("created"), now)
        predicates_of.setdefault(row["skey"], set()).add(predicate)
        if predicate in ATTRIBUTE_PREDICATES:
            attributes.setdefault(row["skey"], []).append(
                {"predicate": predicate, "value": row.get("object")})
            continue
        obj = node(row["okey"], row.get("object"), row.get("otype"))
        predicates_of.setdefault(row["okey"], set())
        for end, group in ((subject, _SUBJECT_GROUP.get(predicate)),
                           (obj, _OBJECT_GROUP.get(predicate))):
            if group and end["group"] == "entity":
                end["group"] = group
        for end in (subject, obj):
            end["degree"] += 1
            end["origins"].add(origin)
            if recent:
                end["recent"] = True
        pending = row.get("review_status") in ("pending", "proposed")
        links.append({
            "source": row["skey"], "target": row["okey"], "predicate": predicate,
            "confidence": float(row.get("confidence") if row.get("confidence")
                                is not None else 1.0),
            "origin": origin, "recent": recent, "pending": pending})

    for key, entry in nodes.items():
        if key == hub:
            entry["group"] = "you"
        elif entry["group"] == "entity":
            preds = predicates_of.get(key, set())
            if preds & {"HAS_PHONE", "PRIMARY_EMAIL"}:
                entry["group"] = "person"
            elif entry["origins"] == {"curiosity"}:
                entry["group"] = "discovery"
        entry["color"] = GROUP_COLORS.get(entry["group"], GROUP_COLORS["entity"])
        entry["origins"] = sorted(entry["origins"])
        entry["attributes"] = len(attributes.get(key, []))

    # Bound the picture, keeping the best-connected nodes and the hub.
    if len(nodes) > MAX_NODES:
        keep = set(sorted(nodes, key=lambda k: (k == hub, nodes[k]["degree"]),
                          reverse=True)[:MAX_NODES])
        nodes = {k: v for k, v in nodes.items() if k in keep}
        links = [l for l in links if l["source"] in keep and l["target"] in keep]

    # Isolated attribute-only nodes (a phone number whose owner was folded) are
    # not worth a dot; people with only a phone are kept, they are contacts.
    linked = {l["source"] for l in links} | {l["target"] for l in links}
    visible = {k: v for k, v in nodes.items()
               if k in linked or v["attributes"] or k == hub}
    return {"nodes": list(visible.values()), "links": links,
            "counts": summarize(visible, links),
            "colors": GROUP_COLORS}


def summarize(nodes, links):
    groups = {}
    for entry in nodes.values():
        groups[entry["group"]] = groups.get(entry["group"], 0) + 1
    origins = {}
    for link in links:
        origins[link["origin"]] = origins.get(link["origin"], 0) + 1
    return {"nodes": len(nodes), "links": len(links),
            "groups": groups, "origins": origins}


_GRAPH_QUERY = """
MATCH (s:Entity)-[r:FACT]->(o:Entity)
WHERE r.valid_to IS NULL
RETURN s.key AS skey, s.name AS subject, s.entity_type AS stype,
       o.key AS okey, o.name AS object, o.entity_type AS otype,
       r.predicate AS predicate, coalesce(r.confidence, 1.0) AS confidence,
       r.source_id AS source_id, r.fact_class AS fact_class,
       r.review_status AS review_status, toString(r.created_at) AS created
LIMIT $limit
"""

_NODE_QUERY = """
MATCH (s:Entity)-[r:FACT]->(o:Entity)
WHERE r.valid_to IS NULL AND (s.key = $key OR o.key = $key)
RETURN s.name AS subject, r.predicate AS predicate, o.name AS object,
       s.key AS skey, o.key AS okey,
       coalesce(r.confidence, 1.0) AS confidence, r.source_id AS source_id,
       r.fact_class AS fact_class, r.review_status AS review_status,
       coalesce(r.mention_count, 1) AS mentions,
       r.evidence_quote AS evidence, toString(r.last_seen) AS last_seen
ORDER BY coalesce(r.confidence, 0.0) DESC
LIMIT 200
"""

_SEARCH_QUERY = """
MATCH (e:Entity)
WHERE e.norm_key CONTAINS $q OR toLower(e.name) CONTAINS $q
RETURN e.key AS key, e.name AS name
LIMIT 12
"""

_VERSION_QUERY = """
MATCH ()-[r:FACT]->()
RETURN count(r) AS n, max(toString(r.updated_at)) AS latest
"""


def fetch_graph(store, hub="himanshu", limit=MAX_EDGES):
    return build_graph(store.query(_GRAPH_QUERY, limit=int(limit)), hub=hub)


def fetch_version(store):
    rows = store.query(_VERSION_QUERY)
    row = rows[0] if rows else {}
    return f"{row.get('n', 0)}:{row.get('latest') or ''}"


def search(store, query, limit=12):
    text = re.sub(r"\s+", " ", str(query or "").strip().lower())
    if len(text) < 2:
        return []
    return store.query(_SEARCH_QUERY, q=text)[:limit]


#: Properties that identify people, shown in the side panel on click.
_ATTRIBUTE_QUERY = """
MATCH (s:Entity {key: $key})-[r:FACT]->(o:Entity)
WHERE r.valid_to IS NULL AND r.predicate IN $predicates
RETURN r.predicate AS predicate, o.name AS value
"""


def node_detail(store, key):
    """Everything known about one node: facts both ways, attributes, origins."""
    key = str(key or "")
    rows = store.query(_NODE_QUERY, key=key)
    attributes = store.query(_ATTRIBUTE_QUERY, key=key,
                             predicates=sorted(ATTRIBUTE_PREDICATES))
    facts, name = [], key
    for row in rows:
        outgoing = row["skey"] == key
        if outgoing:
            name = row["subject"]
        else:
            name = name if name != key else row["object"]
        origin = origin_of(row.get("source_id"), row.get("fact_class"))
        if row["predicate"] in ATTRIBUTE_PREDICATES:
            continue
        facts.append({
            "direction": "out" if outgoing else "in",
            "predicate": row["predicate"],
            "other": row["object"] if outgoing else row["subject"],
            "other_key": row["okey"] if outgoing else row["skey"],
            "confidence": row["confidence"], "origin": origin,
            "pending": row.get("review_status") in ("pending", "proposed"),
            "mentions": row.get("mentions"), "evidence": row.get("evidence"),
            "last_seen": row.get("last_seen")})
    return {"key": key, "name": name, "facts": facts,
            "attributes": attributes,
            "origins": sorted({f["origin"] for f in facts})}
