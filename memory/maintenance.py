"""Conservative maintenance for VANTA's production knowledge graph.

This module never decides that two claims mean the same thing.  It repairs
technical identity collisions and delegates rebuildable file-index cleanup to
``memory``.  Conflicting values and historical FACT edges are retained.
"""
from __future__ import annotations

import hashlib

from memory.neo4j_store import store


def audit_counts():
    queries = {
        "entities": "MATCH (n:Entity) RETURN count(n) AS n",
        "facts_total": "MATCH ()-[r:FACT]->() RETURN count(r) AS n",
        "facts_current": (
            "MATCH ()-[r:FACT]->() WHERE r.valid_to IS NULL "
            "RETURN count(r) AS n"),
        "memories": "MATCH (m:Memory) RETURN count(m) AS n",
        "exact_current_fact_duplicate_groups": """
            MATCH (s:Entity)-[r:FACT]->(o:Entity)
            WHERE r.valid_to IS NULL
            WITH s.key AS subject,r.predicate AS predicate,o.key AS object,
                 count(*) AS n WHERE n>1 RETURN count(*) AS n
        """,
        "duplicate_fact_id_groups": """
            MATCH ()-[r:FACT]->()
            WHERE coalesce(r.fact_id,r.id) IS NOT NULL
            WITH coalesce(r.fact_id,r.id) AS fact_id,count(*) AS n
            WHERE n>1 RETURN count(*) AS n
        """,
    }
    result = {}
    for name, query in queries.items():
        rows = store.query(query)
        result[name] = int(rows[0]["n"]) if rows else 0
    return result


def repair_duplicate_fact_ids(*, dry_run=True):
    """Give historical FACT episodes unique ids without deleting claims."""
    referenced = store.query(
        """
        MATCH ()-[r:FACT]->()
        WHERE coalesce(r.fact_id,r.id) IS NOT NULL
        WITH coalesce(r.fact_id,r.id) AS fact_id,collect(r) AS rels,count(*) AS n
        WHERE n>1 UNWIND rels AS r
        WITH fact_id,r
        WHERE r.supersedes_fact_id IS NOT NULL
           OR r.superseded_by_fact_id IS NOT NULL
        RETURN count(*) AS n
        """)
    if referenced and int(referenced[0]["n"]):
        raise RuntimeError(
            "Duplicate fact ids participate in supersession; manual migration required")
    groups = store.query(
        """
        MATCH ()-[r:FACT]->()
        WHERE coalesce(r.fact_id,r.id) IS NOT NULL
        WITH coalesce(r.fact_id,r.id) AS fact_id,
             collect({relationship_id:elementId(r),
                      current:r.valid_to IS NULL,
                      observed:coalesce(toString(r.valid_from),
                                         toString(r.created_at),'')}) AS members
        WHERE size(members)>1
        RETURN fact_id,members
        """)
    changes = []
    for group in groups:
        old_id = str(group["fact_id"])
        members = sorted(
            group.get("members") or [],
            key=lambda row: (bool(row.get("current")),
                             str(row.get("observed") or "")),
            reverse=True,
        )
        for member in members[1:]:
            relationship_id = str(member["relationship_id"])
            digest = hashlib.sha256(
                f"{old_id}|{relationship_id}".encode("utf-8")).hexdigest()[:32]
            changes.append({
                "relationship_id": relationship_id,
                "old_id": old_id,
                "new_id": f"fact_history_{digest}",
            })
    if not dry_run:
        for start in range(0, len(changes), 500):
            store.query(
                """
                UNWIND $rows AS row
                MATCH ()-[r:FACT]->() WHERE elementId(r)=row.relationship_id
                SET r.legacy_fact_id=row.old_id,r.fact_id=row.new_id,r.id=row.new_id,
                    r.id_repaired_at=datetime()
                """,
                rows=changes[start:start + 500],
            )
    return {"groups": len(groups), "relationships": len(changes)}

