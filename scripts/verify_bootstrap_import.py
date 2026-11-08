"""Metadata-only verification for a committed bootstrap profile import."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from memory.neo4j_store import store


QUERIES = {
    "evidence": "MATCH (m:Memory {kind:'bootstrap_answer'}) RETURN count(m) AS n",
    "raw_semantic": "MATCH (m:Memory {kind:'bootstrap_answer'}) WHERE coalesce(m.semantic_retrievable,true)=true RETURN count(m) AS n",
    "active_profiles": "MATCH (m:Memory {kind:'personal_profile',status:'active'}) RETURN count(m) AS n",
    "facts": "MATCH ()-[r:FACT]->() WHERE r.source_id STARTS WITH 'bootstrap:bootstrap_v1:' RETURN count(r) AS n",
    "missing_metadata": "MATCH ()-[r:FACT]->() WHERE r.source_id STARTS WITH 'bootstrap:bootstrap_v1:' AND (r.fact_id IS NULL OR r.predicate IS NULL OR r.fact_class IS NULL OR r.evidence_type IS NULL OR r.authority IS NULL OR r.sensitivity IS NULL OR r.claim_key IS NULL) RETURN count(r) AS n",
    "runs": "MATCH (i:ImportRun) WHERE i.profile_version='bootstrap-v1' RETURN count(i) AS n",
}


def main():
    print({name: store.query(query)[0]["n"] for name, query in QUERIES.items()})
    store.close()


if __name__ == "__main__":
    main()
