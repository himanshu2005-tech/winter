import sqlite3
import os

def main():
    db_path = os.path.join(os.path.dirname(__file__), 'vault', 'memory', 'state.db')
    if not os.path.exists(db_path):
        print(f"No DB found at {db_path}")
        return

    conn = sqlite3.connect(db_path)
    c = conn.cursor()

    print(f"Connecting to: {db_path}\n")

    # Schema
    print("--- SCHEMA VERSION ---")
    c.execute("SELECT version, applied_at FROM schema_version ORDER BY version DESC")
    print(c.fetchone())
    print()

    # Alarms
    print("--- ALARMS TABLE ---")
    try:
        alarms = c.execute("SELECT label, fire_at, recurrence, status, created_at FROM alarms").fetchall()
        for a in alarms:
            print(f"[{a[3].upper()}] '{a[0]}' | Fire At: {a[1]} | Recurrence: {a[2]} | Created: {a[4]}")
        if not alarms:
            print("No alarms found.")
    except Exception as e:
        print(f"Error querying alarms: {e}")
    print()
    
    # Notified
    print("--- NOTIFIED (Deduplication) ---")
    try:
        notified = c.execute("SELECT id, source, created_at FROM notified ORDER BY created_at DESC LIMIT 5").fetchall()
        for n in notified:
            print(f"[{n[1]}] {n[0]} | Logged: {n[2]}")
        if not notified:
            print("No notified records found.")
    except Exception as e:
        print(f"Error querying notified: {e}")
    print()
    
    # Action Log
    print("--- ACTION LOG ---")
    try:
        actions = c.execute("SELECT action_type, outcome, verified_flag, created_at FROM action_log ORDER BY created_at DESC LIMIT 5").fetchall()
        for a in actions:
            print(f"[{a[0]}] Verified: {a[2]} | Outcome: {a[1]} | At: {a[3]}")
        if not actions:
            print("No actions logged yet.")
    except Exception as e:
        print(f"Error querying action_log: {e}")
    print()
    
    # Approvals
    print("--- APPROVALS PIPELINE ---")
    try:
        approvals = c.execute("SELECT action_type, status, expires_at FROM approvals ORDER BY created_at DESC LIMIT 5").fetchall()
        for a in approvals:
            print(f"[{a[1].upper()}] {a[0]} | Expires: {a[2]}")
        if not approvals:
            print("No approvals queued.")
    except Exception as e:
        print(f"Error querying approvals: {e}")
    print()

    conn.close()

if __name__ == "__main__":
    main()
