import sqlite3
import os

db_path = os.path.join("vault", "memory", "hybrid_graph.db")
conn = sqlite3.connect(db_path)

edges = conn.execute("SELECT subject, predicate, object FROM edges WHERE predicate IN ('HAS_CONTACT', 'HAS_PHONE') LIMIT 10").fetchall()

for row in edges:
    print(row)
