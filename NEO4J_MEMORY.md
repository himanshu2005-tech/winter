# VANTA unified knowledge graph

VANTA now uses one localhost-only Neo4j Community database for relational facts, semantic memories, conversations, contacts, alarms, approvals, notifications, audit logs, and temporary session handles.

## Runtime

- Start VANTA normally with `start.bat`. It starts Neo4j first and refuses to launch the agent if the database is unavailable, preventing split-brain writes.
- Neo4j listens only on `127.0.0.1` (`Bolt 7687`, HTTP `7474`).
- The database password is stored in Windows Credential Manager under service `VANTA.Neo4j`, user `neo4j`; it is not stored in the repository.
- Data, transactions, and logs live below `vault/neo4j/`. The Neo4j distribution lives below `.runtime/neo4j/`.
- Embeddings are generated locally with the cached 384-dimensional `all-MiniLM-L6-v2` ONNX model. Private memory is not sent to a cloud embedding API.

## Retrieval policy

Current focus and the recent conversation are protected first. The remaining context budget is allocated to the rolling summary, high-trust graph relationships, and then hybrid semantic/full-text memories. Semantic and full-text ranks are fused; source, confidence, authority, time, and validity remain available for auditing.

## Migration and recovery

`scripts/migrate_to_neo4j.py` is idempotent. It copies all legacy stores to `vault/backups/neo4j-migration-<timestamp>/` before importing unless `--no-backup` is explicitly supplied. It verifies that target counts are not lower than source counts. Legacy Chroma and SQLite files are retained as migration inputs and are no longer active runtime stores.

To restore, stop VANTA, preserve the current `vault/neo4j/`, restore a chosen backup, start Neo4j, and rerun the migration. Do not delete legacy files until a separate retention decision is made.

## Verification

Run `python tests/test_neo4j_integration.py` from the project virtual environment. The check validates connectivity, hybrid retrieval, and an alarm write/read round trip, then removes only its own test alarm.
