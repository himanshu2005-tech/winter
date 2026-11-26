import os
import json
import sqlite3
import sys

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
notified_file = os.path.join(BASE_DIR, 'vault', 'system', 'notified_emails.json')
db_path = os.path.join(BASE_DIR, 'vault', 'memory', 'state.db')

def migrate():
    # Make sure DB exists and schema is updated
    from memory.state_db import init_state_db
    init_state_db()
    
    if not os.path.exists(notified_file):
        print("No notified_emails.json found. Skipping migration.")
        return

    try:
        with open(notified_file, 'r') as f:
            notified_list = json.load(f)
    except Exception as e:
        print(f"Failed to read JSON: {e}")
        return

    if not isinstance(notified_list, list):
        print("Invalid JSON format.")
        return

    print(f"Found {len(notified_list)} notified emails. Migrating to SQL...")
    
    conn = sqlite3.connect(db_path)
    c = conn.cursor()
    
    migrated_count = 0
    for email_id in notified_list:
        try:
            c.execute("INSERT OR IGNORE INTO notified (id, source) VALUES (?, ?)", (email_id, 'gmail'))
            migrated_count += 1
        except Exception as e:
            print(f"Failed to insert {email_id}: {e}")
            
    conn.commit()
    conn.close()
    
    print(f"Successfully migrated {migrated_count} records to SQL.")
    
    backup_path = notified_file + ".bak"
    os.rename(notified_file, backup_path)
    print(f"Renamed old file to {backup_path}")

if __name__ == "__main__":
    migrate()
