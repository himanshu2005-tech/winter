"""Relational-fact facade for VANTA's unified Neo4j store."""
from memory.neo4j_store import store

def add_edge(subject,predicate,object_val,confidence=1.0,source_id="user_stated",**metadata):
    return store.add_fact(subject,predicate,object_val,confidence,source_id,**metadata)

def get_current_edges(subject=None):
    return store.current_edges(subject)

def get_neighborhood(entities,hops=1):
    return store.neighborhood(entities,hops)
