# -*- coding: utf-8 -*-
"""
dedupe_graph.py — collapse duplicate FACT edges in the memory graph.

Facts used to be keyed by a hash that included `source_id`, so the same claim
learned from two different turns became two separate edges. The live path no
longer does this (it merges on subject/predicate/object), but edges written
before that change are still there, inflating retrieval and making the same
fact appear several times in the model's context.

This is deliberately a separate, opt-in script rather than something that runs
at startup. Its default mode reports counts without retiring any claims.

    python dedupe_graph.py           # report only, changes nothing
    python dedupe_graph.py --apply   # retire metadata-equivalent duplicates without deleting history

Only edges with identical non-audit metadata are eligible for retirement.
Different sources, privacy levels, claim keys, review status and unknown metadata
are preserved. Retirement closes validity; it never deletes evidence or nodes.
"""

import argparse
import sys

from memory.neo4j_store import store


def say(text=""):
    """Print safely: some entity names carry characters the Windows console
    cannot encode, and a report must not die partway through on one of them."""
    encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
    print(str(text).encode(encoding, errors="replace").decode(encoding))

FIND_DUPLICATES = """
MATCH (s:Entity)-[r:FACT]->(o:Entity)
WHERE r.valid_to IS NULL
WITH s, r.predicate AS predicate, o, collect(r) AS edges
WHERE size(edges) > 1
RETURN s.name AS subject, predicate, o.name AS object, size(edges) AS copies
ORDER BY copies DESC, subject
"""

COLLAPSE = """
MATCH (s:Entity)-[r:FACT]->(o:Entity)
WHERE r.valid_to IS NULL
WITH s,r.predicate AS predicate,o,r ORDER BY elementId(r)
WITH s,predicate,o,collect(r) AS edges WHERE size(edges)>1
WITH head(edges) AS keep,tail(edges) AS candidates
WITH keep,[e IN candidates WHERE
  all(k IN keys(e) + keys(keep) WHERE
    k IN ['id','fact_id','mention_count','created_at','updated_at','last_seen','valid_from']
    OR (e[k] IS NULL AND keep[k] IS NULL) OR e[k]=keep[k])
] AS duplicates
WHERE size(duplicates)>0
SET keep.mention_count=coalesce(keep.mention_count,1)+
    reduce(total=0,e IN duplicates | total+coalesce(e.mention_count,1))
WITH keep,duplicates UNWIND duplicates AS doomed
SET doomed.valid_to=toString(datetime()),
    doomed.correction_reason='exact metadata duplicate retired; original evidence retained',
    doomed.superseded_by_fact_id=coalesce(keep.fact_id,keep.id)
RETURN count(doomed) AS removed
"""


def main():
    parser = argparse.ArgumentParser(description="Collapse duplicate FACT edges.")
    parser.add_argument("--apply", action="store_true",
                        help="retire equivalent duplicates; retain history (default: dry run)")
    args = parser.parse_args()

    try:
        health = store.health()
    except Exception as exc:
        say(f"Could not reach Neo4j: {exc}")
        return 1
    if health.get("status") != "ok":
        say(f"Neo4j is not healthy: {health}")
        return 1

    before = store.query(
        "MATCH ()-[r:FACT]->() WHERE r.valid_to IS NULL RETURN count(r) AS n"
    )[0]["n"]
    groups = store.query(FIND_DUPLICATES)
    redundant = sum(g["copies"] - 1 for g in groups)

    say(f"Current facts:      {before}")
    say(f"Duplicated claims:  {len(groups)}")
    say(f"Redundant edges:    {redundant}")
    if not groups:
        say("\nNothing to do.")
        return 0

    say("\nLargest duplicate groups:")
    for g in groups[:15]:
        say(f"  duplicate group: {g['copies']} current edges")
    if len(groups) > 15:
        say(f"  ... and {len(groups) - 15} more")

    if not args.apply:
        say(f"\nDry run. Re-run with --apply to review and retire metadata-equivalent duplicate edges.")
        return 0

    removed = store.query(COLLAPSE)
    removed_count = removed[0]["removed"] if removed else 0
    after = store.query(
        "MATCH ()-[r:FACT]->() WHERE r.valid_to IS NULL RETURN count(r) AS n"
    )[0]["n"]
    remaining = store.query(FIND_DUPLICATES)

    say(f"\nRetired {removed_count} metadata-equivalent duplicate edges; history retained.")
    say(f"Facts: {before} -> {after}")
    say(f"Duplicate claims remaining: {len(remaining)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
