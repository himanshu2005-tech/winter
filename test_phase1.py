import os
import time
import datetime
from memory.state_db import init_state_db, execute_read, execute_write
import alarms

print("=== Phase 1: state.db & Alarms Scheduler Tests ===")

# Test 1: Migration Idempotency
print("\n[Test 1] Migration Idempotency")
try:
    init_state_db()
    init_state_db() # Call twice
    rows = execute_read("SELECT version FROM schema_version")
    print(f"Schema versions (should be [1]): {[r[0] for r in rows]}")
    if len(rows) == 1 and rows[0][0] == 1:
        print("-> PASS")
    else:
        print("-> FAIL")
except Exception as e:
    print(f"-> FAIL: {e}")

# Test 2: Write-Verify on set_alarm and Timezones
print("\n[Test 2] Write-Verify & Timezones")
# Create an alarm for exactly 2 seconds from now to test real-time firing
now = datetime.datetime.now().astimezone()
future = now + datetime.timedelta(seconds=3)
res = alarms.set_alarm(future.strftime("%H:%M"), "Test Verify Alarm", recurrence="none")
print(f"set_alarm result: {res}")
if "Alarm set for" in res:
    # Check SQLite
    rows = execute_read("SELECT label, status FROM alarms WHERE label = 'Test Verify Alarm' ORDER BY created_at DESC LIMIT 1")
    if rows and rows[0][1] == 'pending':
        print(f"Found in DB: {rows[0]}")
        print("-> PASS")
    else:
        print(f"-> FAIL: Not found pending in DB. Rows: {rows}")
else:
    print("-> FAIL: Write-verify blocked creation.")

# Test 3: Missed-Alarm Policy & Recurrence
print("\n[Test 3] Missed-Alarm & Recurrence")
# Inject a missed alarm (set 20 mins ago) with daily recurrence
past = now - datetime.timedelta(minutes=20)
execute_write(
    "INSERT INTO alarms (id, label, fire_at, recurrence, status) VALUES ('missed_1', 'Missed Daily', ?, 'daily', 'pending')",
    (past.isoformat(),)
)

# Run the alarm_worker once manually by calling its startup block
def mock_speak(text):
    print(f"[SPEAK MOCK]: {text}")

# We will spin up the worker in a thread, let it run for 4 seconds to catch the missed alarm and the live alarm, then kill the script
import threading
t = threading.Thread(target=alarms.alarm_worker, args=(mock_speak,), daemon=True)
t.start()

time.sleep(5)

# Verify DB state
missed = execute_read("SELECT status FROM alarms WHERE id = 'missed_1'")
print(f"Missed alarm status (should be 'missed'): {missed}")

# Verify Recurrence was created
recurring = execute_read("SELECT id, fire_at, status FROM alarms WHERE label = 'Missed Daily' AND status = 'pending'")
if recurring:
    print(f"Recurring instance created: {recurring}")
    print("-> PASS")
else:
    print("-> FAIL: Recurrence missing.")

print("\n=== All Tests Completed ===")
