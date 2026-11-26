"""Start VANTA's localhost-only Neo4j service and verify Bolt readiness."""
import os
import socket
import subprocess
import sys
import time
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
homes = sorted((ROOT / ".runtime" / "neo4j").glob("neo4j-community-*"), reverse=True)
if not homes:
    raise SystemExit("Neo4j runtime is missing under .runtime/neo4j")
home = homes[0]
java = Path(os.getenv("WINTER_JAVA_HOME", r"C:\Program Files\Java\jdk-25"))
java_exe = java / "bin" / "java.exe"
if not java_exe.exists():
    raise SystemExit(f"Java runtime is missing: {java_exe}")

env = os.environ.copy()
env["JAVA_HOME"] = str(java)
env["NEO4J_HOME"] = str(home)
command = home / "bin" / "neo4j.bat"
log_dir = ROOT / "vault" / "neo4j" / "logs"
log_dir.mkdir(parents=True, exist_ok=True)
launcher_log = log_dir / "launcher.log"
STARTUP_TIMEOUT = min(
    600.0,
    max(90.0, float(os.getenv("VANTA_NEO4J_STARTUP_TIMEOUT", "180"))),
)


def bolt_ready(timeout=0.7):
    try:
        with socket.create_connection(("127.0.0.1", 7687), timeout=timeout):
            return True
    except OSError:
        return False


def database_ready():
    """A listening port is not proof that the database can execute work."""
    result = {"ok": False}
    def probe():
      try:
        from neo4j import GraphDatabase
        uri = os.getenv("NEO4J_URI", "bolt://127.0.0.1:7687")
        user = os.getenv("NEO4J_USER", "neo4j")
        database = os.getenv("NEO4J_DATABASE", "neo4j")
        password = os.getenv("NEO4J_PASSWORD", "") or os.getenv("NEO_4J", "")
        if not password:
            import keyring
            for service in ("VANTA.Neo4j", "WINTER.Neo4j"):
                password = keyring.get_password(service, user) or ""
                if password:
                    break
        if not password:
            return
        driver = GraphDatabase.driver(uri, auth=(user, password),
                                      connection_timeout=2.0,
                                      max_connection_pool_size=1)
        try:
            with driver.session(database=database) as session:
                record = session.run("RETURN true AS ok", timeout=3.0).single()
                result["ok"] = bool(record and record["ok"])
        finally:
            driver.close()
      except Exception:
        pass
    thread = threading.Thread(target=probe, daemon=True, name="neo4j-startup-health")
    thread.start()
    thread.join(6.0)
    return result["ok"]


def tail(path, lines=35):
    try:
        return "\n".join(path.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:])
    except OSError:
        return ""


def process_alive(pid):
    """Return whether a recorded launcher PID still exists on Windows."""
    try:
        pid = int(pid)
        if pid <= 0:
            return False
        if os.name == "nt":
            import ctypes
            handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
            if not handle:
                return False
            ctypes.windll.kernel32.CloseHandle(handle)
            return True
        os.kill(pid, 0)
        return True
    except (OSError, TypeError, ValueError):
        return False


def recorded_launcher():
    pid_file = home / "run" / "vanta-console.pid"
    try:
        pid = int(pid_file.read_text(encoding="ascii").strip())
    except (OSError, TypeError, ValueError):
        return None
    return pid if process_alive(pid) else None


def wait_for_database(deadline, launcher=None):
    """Wait for authenticated readiness without mistaking recovery for death."""
    exit_code = None
    while time.monotonic() < deadline:
        if bolt_ready() and database_ready():
            return True, exit_code
        if launcher is not None:
            exit_code = launcher.poll()
            if exit_code not in (None, 0):
                break
        time.sleep(0.75)
    return False, exit_code


if bolt_ready() and database_ready():
    print("[Startup] Neo4j is ready on localhost:7687")
    raise SystemExit(0)

if bolt_ready():
    print("[Startup] Neo4j owns Bolt but failed an authenticated health query; "
          "requesting a local restart.")
    try:
        subprocess.run([str(command), "stop"], cwd=str(home), env=env,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       timeout=30, check=False)
    except Exception:
        pass
    deadline = time.monotonic() + 20.0
    while bolt_ready() and time.monotonic() < deadline:
        time.sleep(0.5)

# A previous invocation can still be performing transaction/index recovery.
# Starting a second JVM against the same store would only produce a store-lock
# error.  Wait for the recorded owner before deciding that a new launch is
# necessary.  This is especially important after an interrupted VANTA start.
existing_pid = recorded_launcher()
if existing_pid:
    print(f"[Startup] Neo4j launcher {existing_pid} is still recovering; waiting up to "
          f"{int(STARTUP_TIMEOUT)} seconds for authenticated readiness.")
    ready, _ = wait_for_database(time.monotonic() + STARTUP_TIMEOUT)
    if ready:
        print("[Startup] Neo4j is ready on localhost:7687")
        raise SystemExit(0)
    # The recorded process may have ended while we waited.  Only launch a new
    # owner when that is actually true; never compete for the live store lock.
    if process_alive(existing_pid):
        raise SystemExit(
            f"Neo4j launcher {existing_pid} is still running but the database "
            f"did not become ready within {int(STARTUP_TIMEOUT)} seconds. "
            "VANTA was not started; the launcher was left intact for recovery."
        )

# `console` is tied to the launching terminal and can die when start.bat or a
# detached console closes. `start` asks Neo4j's launcher to own the background
# Java process and returns independently.
log = open(launcher_log, "a", encoding="utf-8", buffering=1)
log.write(f"\n[{time.strftime('%Y-%m-%d %H:%M:%S')}] starting {home.name}\n")
flags = (
    getattr(subprocess, "CREATE_NO_WINDOW", 0)
    | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
)
launcher = subprocess.Popen(
    ["cmd.exe", "/d", "/s", "/c", "call", str(command), "console"],
    cwd=str(home), env=env, stdout=log, stderr=subprocess.STDOUT,
    stdin=subprocess.DEVNULL, creationflags=flags,
)
(home / "run").mkdir(parents=True, exist_ok=True)
(home / "run" / "vanta-console.pid").write_text(str(launcher.pid), encoding="ascii")

deadline = time.monotonic() + STARTUP_TIMEOUT
ready, exit_code = wait_for_database(deadline, launcher=launcher)
if ready:
    print("[Startup] Neo4j is ready on localhost:7687")
    log.close()
    raise SystemExit(0)

log.close()
details = tail(launcher_log)
server_details = tail(log_dir / "neo4j.log")
message = [
    f"Neo4j failed to become query-ready within {int(STARTUP_TIMEOUT)} seconds "
    f"(launcher exit={exit_code})."
]
if details:
    message.append("Launcher log:\n" + details)
if server_details:
    message.append("Neo4j log:\n" + server_details)
raise SystemExit("\n".join(message))
