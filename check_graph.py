import sqlite3
import os

db_path = os.path.join("vault", "memory", "hybrid_graph.db")
if not os.path.exists(db_path):
    print("Graph DB does not exist yet.")
else:
    conn = sqlite3.connect(db_path)
    try:
        nodes = conn.execute("SELECT count(*) FROM entities").fetchone()[0]
        edges = conn.execute("SELECT count(*) FROM edges").fetchone()[0]
        print(f"Graph DB has {nodes} nodes and {edges} edges.")
        
        if nodes > 0:
            print("\nNodes:")
            for row in conn.execute("SELECT id, type, properties FROM entities LIMIT 10").fetchall():
                print(row)
                
        if edges > 0:
            print("\nEdges:")
            for row in conn.execute("SELECT source_id, target_id, relation_type FROM edges LIMIT 10").fetchall():
                print(row)
    except Exception as e:
        print(f"Error querying Graph DB: {e}")
