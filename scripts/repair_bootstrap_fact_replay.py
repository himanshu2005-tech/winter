"""One-shot guarded repair for the bootstrap-v1 replay duplication."""

from memory.neo4j_store import store


PREFIX = "bootstrap:bootstrap_v1:"
EXPECTED_GROUPS = 319
EXPECTED_RELATIONSHIPS = 638


def main():
    summary = store.query(
        """
        MATCH ()-[r:FACT]->()
        WHERE r.source_id STARTS WITH $prefix
        WITH r.fact_id AS fact_id, collect(r) AS rels
        RETURN count(*) AS groups, sum(size(rels)) AS relationships,
               min(size(rels)) AS min_per_group, max(size(rels)) AS max_per_group
        """,
        prefix=PREFIX,
    )[0]
    print({"preflight": summary})
    if not (
        summary["groups"] == EXPECTED_GROUPS
        and summary["relationships"] == EXPECTED_RELATIONSHIPS
        and summary["min_per_group"] == 2
        and summary["max_per_group"] == 2
    ):
        raise RuntimeError("Guard failed; refusing to modify the graph")

    result = store.query(
        """
        MATCH ()-[r:FACT]->()
        WHERE r.source_id STARTS WITH $prefix
        WITH r.fact_id AS fact_id, collect(r) AS rels
        WHERE fact_id IS NOT NULL AND size(rels) = 2
        WITH fact_id, head(rels) AS keep, tail(rels) AS duplicates
        SET keep.id = fact_id
        FOREACH (duplicate IN duplicates | DELETE duplicate)
        RETURN count(*) AS repaired_groups,
               sum(size(duplicates)) AS deleted_duplicate_relationships
        """,
        prefix=PREFIX,
    )[0]
    print({"repair": result})
    store.close()


if __name__ == "__main__":
    main()
