"""Idempotently back up and migrate VANTA's legacy stores into Neo4j."""
from __future__ import annotations
import argparse, datetime as dt, json, shutil, sqlite3, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from memory.neo4j_store import store

LEGACY = ROOT / "vault" / "memory"

def backup_sources():
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    target = ROOT / "vault" / "backups" / f"neo4j-migration-{stamp}"
    target.mkdir(parents=True, exist_ok=False)
    for name in ("hybrid_graph.db", "state.db", "temp_handles.db"):
        source = LEGACY / name
        if source.exists(): shutil.copy2(source, target / name)
    chroma = ROOT / "vault" / "chroma_db"
    if chroma.exists(): shutil.copytree(chroma, target / "chroma_db")
    return target

def table_rows(path, table):
    if not path.exists(): return []
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    try: return [dict(row) for row in connection.execute(f"SELECT * FROM {table}")]
    except sqlite3.Error: return []
    finally: connection.close()

def migrate_graph(stats):
    path = LEGACY / "hybrid_graph.db"
    entities = table_rows(path, "entities")
    for row in entities:
        try: aliases = json.loads(row.get("aliases") or "[]")
        except Exception: aliases = []
        store.upsert_entity(row["canonical_name"], row.get("type") or "Entity", aliases)
    edges = table_rows(path, "edges")
    for row in edges:
        source = f"legacy:{row.get('id')}:{row.get('source_id') or 'unknown'}"
        store.add_fact(row["subject"], row["predicate"], row["object"], row.get("confidence") or 1.0, source, row.get("valid_from"), row.get("valid_to"))
    stats.update(entities=len(entities), facts=len(edges))

def migrate_chroma(stats):
    path = ROOT / "vault" / "chroma_db"
    if not path.exists():
        stats["memories"] = 0
        return
    import chromadb
    collection = chromadb.PersistentClient(path=str(path)).get_collection("vault_memory")
    data = collection.get(include=["documents", "metadatas", "embeddings"])
    items = []
    for index, old_id in enumerate(data.get("ids", [])):
        meta = (data.get("metadatas") or [{}])[index] or {}
        vector = data["embeddings"][index]
        items.append({"id":f"legacy_{old_id}","kind":meta.get("type") or "knowledge","topic":meta.get("topic") or "general","source":meta.get("source") or "legacy_chroma","source_id":old_id,"title":meta.get("title") or "","content":data["documents"][index],"timestamp":meta.get("timestamp") or meta.get("indexed_at"),"embedding":vector.tolist() if hasattr(vector,"tolist") else vector})
    for offset in range(0, len(items), 100): store.upsert_memory_batch(items[offset:offset+100])
    stats["memories"] = len(items)

def migrate_state(stats):
    path = LEGACY / "state.db"
    mapping = {"alarms":"Alarm","approvals":"Approval","action_log":"ActionLog"}
    for table,label in mapping.items():
        rows = table_rows(path, table)
        for row in rows:
            identity = row.pop("id")
            store.query(f"MERGE (n:{label} {{id:$id}}) SET n += $props",id=identity,props=row)
        stats[table] = len(rows)
    notices = table_rows(path, "notified")
    for row in notices:
        key = f"{row.get('source')}:{row.get('id')}"
        store.query("MERGE (n:Notification {key:$key}) SET n += $props",key=key,props=row)
    stats["notifications"] = len(notices)

def migrate_handles(stats):
    rows = table_rows(LEGACY / "temp_handles.db", "handles")
    for row in rows:
        store.put_handle(row["handle_id"],row.get("description") or "",row.get("payload") or "",row.get("expires_at") or 0)
    stats["handles"] = len(rows)

def count(label):
    return store.query(f"MATCH (n:{label}) RETURN count(n) AS count")[0]["count"]

def verify(stats):
    actual = {"memories":count("Memory"),"entities":count("Entity"),"alarms":count("Alarm"),"approvals":count("Approval"),"action_log":count("ActionLog"),"notifications":count("Notification"),"handles":count("Handle")}
    actual["facts"] = store.query("MATCH ()-[r:FACT]->() RETURN count(r) AS count")[0]["count"]
    for key,value in stats.items():
        if actual.get(key,0) < value: raise RuntimeError(f"Migration verification failed for {key}")
    return actual

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-backup",action="store_true")
    args = parser.parse_args()
    backup = None if args.no_backup else backup_sources()
    store.ensure_schema()
    stats = {}
    migrate_graph(stats)
    migrate_chroma(stats)
    migrate_state(stats)
    migrate_handles(stats)
    actual = verify(stats)
    print(json.dumps({"backup":str(backup) if backup else None,"source":stats,"target":actual},indent=2))

if __name__ == "__main__":
    main()
