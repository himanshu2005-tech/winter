"""Read-only, sensitivity-aware graph snapshots for VANTA's visual explorer."""
from __future__ import annotations

import math
from collections import Counter


_ALLOWED_SENSITIVITIES = ("public", "private")


def load_graph_snapshot(search="", limit=70, store_obj=None):
    """Return a bounded Entity-FACT-Entity view without raw evidence payloads."""
    if store_obj is None:
        from memory.neo4j_store import store as store_obj
    query = " ".join(str(search or "").split()).casefold()[:120]
    rows = store_obj.query(
        """
        MATCH (s:Entity)-[r:FACT]->(o:Entity)
        WHERE r.valid_to IS NULL
          AND coalesce(r.sensitivity,'private') IN $sensitivities
          AND ($query = '' OR toLower(s.name) CONTAINS $query
               OR toLower(o.name) CONTAINS $query
               OR toLower(coalesce(r.predicate,'')) CONTAINS $query)
        RETURN s.key AS subject_id,s.name AS source,
               o.key AS target_id,o.name AS target,
               coalesce(r.predicate,'RELATED_TO') AS predicate,
               coalesce(r.fact_class,'legacy') AS fact_class,
               coalesce(r.confidence,1.0) AS confidence,
               coalesce(r.sensitivity,'private') AS sensitivity,
               coalesce(r.review_status,'accepted') AS review_status,
               coalesce(r.source_id,'legacy') AS evidence_source_id,
               coalesce(r.evidence_type,'legacy') AS evidence_type,
               coalesce(r.authority,'unknown') AS authority
        ORDER BY CASE WHEN toLower(s.name)='himanshu' OR toLower(o.name)='himanshu'
                      THEN 0 ELSE 1 END,
                 coalesce(r.confidence,0.0) DESC
        LIMIT $limit
        """,
        query=query, sensitivities=list(_ALLOWED_SENSITIVITIES),
        limit=max(1, min(int(limit), 120)),
    )
    return snapshot_from_rows(rows)


def snapshot_from_rows(rows):
    nodes = {}
    edges = []
    seen_edges = set()
    for row in rows or []:
        sid = str(row.get("subject_id") or row.get("source_id") or row.get("source") or "").strip()
        tid = str(row.get("target_id") or row.get("target") or "").strip()
        source = str(row.get("source") or sid).strip()
        target = str(row.get("target") or tid).strip()
        predicate = str(row.get("predicate") or "RELATED_TO").strip()
        if not sid or not tid or not source or not target:
            continue
        nodes.setdefault(sid, {"id": sid, "label": source, "degree": 0})
        nodes.setdefault(tid, {"id": tid, "label": target, "degree": 0})
        edge_key = (sid, predicate, tid)
        if edge_key in seen_edges:
            continue
        seen_edges.add(edge_key)
        nodes[sid]["degree"] += 1
        nodes[tid]["degree"] += 1
        edges.append({
            "source": sid, "target": tid, "predicate": predicate,
            "fact_class": str(row.get("fact_class") or "legacy"),
            "confidence": float(row.get("confidence") or 0.0),
            "sensitivity": str(row.get("sensitivity") or "private"),
            "review_status": str(row.get("review_status") or "accepted"),
            "evidence_source_id": str(row.get("evidence_source_id") or "legacy"),
            "evidence_type": str(row.get("evidence_type") or "legacy"),
            "authority": str(row.get("authority") or "unknown"),
        })
    ordered = sorted(nodes.values(), key=lambda item: (item["label"].casefold() != "himanshu", -item["degree"], item["label"].casefold()))
    if not edges:
        return {"nodes": ordered, "edges": []}
    classes = Counter(edge["fact_class"] for edge in edges)
    reviews = Counter(edge["review_status"] for edge in edges)
    return {"nodes": ordered, "edges": edges,
            "summary": {"fact_classes": dict(classes),
                        "review_statuses": dict(reviews)}}


def radial_layout(snapshot, width, height):
    """Deterministic degree-weighted layout requiring no extra GUI packages."""
    nodes = list((snapshot or {}).get("nodes") or [])
    if not nodes:
        return {}
    width, height = max(200, int(width)), max(160, int(height))
    cx, cy = width / 2.0, height / 2.0
    positions = {nodes[0]["id"]: (cx, cy)}
    remaining = nodes[1:]
    ring_capacity = 10
    offset = 0
    ring = 1
    while offset < len(remaining):
        group = remaining[offset:offset + ring_capacity * ring]
        radius = min(width, height) * min(0.16 * ring, 0.44)
        count = len(group)
        for index, node in enumerate(group):
            angle = -math.pi / 2 + (2 * math.pi * index / max(1, count))
            positions[node["id"]] = (
                cx + radius * math.cos(angle),
                cy + radius * math.sin(angle),
            )
        offset += count
        ring += 1
    return positions
