"""Compile final graph-write queries using EXPLAIN; never execute the writes.

Run from the project root with the normal VANTA Python environment.
Credentials stay in the environment/keyring and are never printed.
"""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def captured_queries():
    from memory.neo4j_store import GraphStore
    from dedupe_graph import COLLAPSE
    queries = []
    graph = GraphStore()
    graph.canonical_name = lambda text: text
    def capture(cypher, **params):
        queries.append((cypher, params))
        return []
    graph.query = capture
    graph.add_fact('synthetic query check', 'LIKES', 'synthetic value')
    graph.invalidate_fact('synthetic query check', 'LIKES', 'synthetic value')
    queries.append((COLLAPSE, {}))
    return queries


def main():
    from dotenv import load_dotenv
    from neo4j import GraphDatabase
    from memory.neo4j_store import load_config
    load_dotenv(Path(__file__).resolve().parents[1] / '.env')
    uri, user, password, database = load_config()
    with GraphDatabase.driver(uri, auth=(user, password), connection_timeout=2,
                              max_transaction_retry_time=1) as driver:
        with driver.session(database=database) as session:
            for index, (cypher, params) in enumerate(captured_queries(), 1):
                session.run('EXPLAIN ' + cypher, **params).consume()
                print(f'Query {index}: Neo4j compile passed (EXPLAIN only)')
    print('No graph writes executed.')


if __name__ == '__main__':
    main()
