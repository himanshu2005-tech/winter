"""Entity facade for VANTA's unified Neo4j store."""
from memory.neo4j_store import store

def get_canonical_name(name):
    return store.canonical_name(name)

def add_entity(canonical_name, entity_type="Entity", aliases=None):
    return store.upsert_entity(canonical_name,entity_type,aliases)
