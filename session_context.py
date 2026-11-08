import sqlite3
import uuid
import time
import queue
import threading
from collections import deque
from threading import Lock
import os
import re
from memory.neo4j_store import store as _graph_store

def _resolve_graph_handle(handle_id):
    row = _graph_store.get_handle(handle_id)
    if not row or time.time() > row["expires_at"]:
        return f"[Error] Unknown or expired handle: {handle_id}"
    return row["payload"]

from memory import save_conversation

_lock = Lock()
_session_id = uuid.uuid4().hex[:8].upper()
_turn_counter = 0

# Sliding Window and Budgets
MAX_TURNS = 5  
_turns = deque(maxlen=MAX_TURNS)

# Typed Focus State
# Keys: email, file, url, person, search_result, last_item
_focus_state = {
    "email": deque(maxlen=5),
    "file": deque(maxlen=5),
    "url": deque(maxlen=5),
    "person": deque(maxlen=5),
    "search_result": deque(maxlen=5),
    "last_item": deque(maxlen=5)
}

# Rolling Summary & Eviction
_rolling_summary = ""
_eviction_queue = queue.Queue()
_shutdown_event = threading.Event()

def get_handles_db_path():
    base_dir = os.path.dirname(os.path.abspath(__file__))
    vault_dir = os.path.join(base_dir, "vault", "memory")
    os.makedirs(vault_dir, exist_ok=True)
    return os.path.join(vault_dir, "temp_handles.db")

def _init_handles_db():
    db_path = get_handles_db_path()
    with _lock:
        conn = sqlite3.connect(db_path)
        c = conn.cursor()
        c.execute("""
            CREATE TABLE IF NOT EXISTS handles (
                handle_id TEXT PRIMARY KEY,
                description TEXT,
                payload TEXT,
                expires_at REAL
            )
        """)
        conn.commit()
        conn.close()

# Legacy handle DB is migration input only.

def start_session():
    """Initializes the session state."""
    global _session_id, _turn_counter, _rolling_summary
    with _lock:
        _session_id = uuid.uuid4().hex[:8].upper()
        _turn_counter = 0
        _turns.clear()
        for k in _focus_state:
            _focus_state[k].clear()
        _rolling_summary = ""
    print(f"[Session Context] Session {_session_id} started.")

def end_session():
    """Flushes remaining eligible turns and drains queue."""
    _shutdown_event.set()
    
    with _lock:
        # Flush remaining valid turns to eviction queue
        for t in list(_turns):
            if t["user"] and t["assistant"] and t["origin"] in ["user_stated", "verified_api"]:
                _eviction_queue.put(t)
        _turns.clear()
        
    print("[Session Context] Waiting for background eviction queue to drain...")
    _eviction_queue.join()
    print(f"[Session Context] Session {_session_id} ended.")

def get_rolling_summary():
    with _lock:
        return _rolling_summary

def get_focus_state():
    """Returns the most recent item for each active focus slot."""
    with _lock:
        current_focus = {}
        for slot, stack in _focus_state.items():
            if stack:
                current_focus[slot] = stack[-1][1]  # (timestamp, handle_id)
        return current_focus

def get_recent_chat():
    """Returns the chat window turns as text."""
    with _lock:
        if not _turns:
            return "No recent chat history."
        lines = []
        for i, turn in enumerate(_turns):
            lines.append(f"User: {turn['user']}")
            if turn.get("assistant"):
                lines.append(f"Assistant: {turn['assistant']}")
        return "\n".join(lines)

def add_handle(description, raw_data, slot=None, ttl_seconds=7200):
    """
    Stores bulky data in SQLite with a TTL.
    Updates the specified focus slot and 'last_item'.
    """
    handle_id = f"{_session_id}_H_{uuid.uuid4().hex[:6].upper()}"
    expires_at = time.time() + ttl_seconds
    
    db_path = get_handles_db_path()
    with _lock:
        conn = sqlite3.connect(db_path)
        c = conn.cursor()
        c.execute("INSERT INTO handles (handle_id, description, payload, expires_at) VALUES (?, ?, ?, ?)",
                  (handle_id, description, raw_data, expires_at))
        conn.commit()
        conn.close()
        
        now = time.time()
        _focus_state["last_item"].append((now, handle_id))
        if slot and slot in _focus_state:
            _focus_state[slot].append((now, handle_id))
            
    return f"[{description}] (Handle: {handle_id})"

def resolve_handle(handle_id):
    """Retrieves the raw data for a given handle, handling expiry gracefully."""
    db_path = get_handles_db_path()
    with _lock:
        conn = sqlite3.connect(db_path)
        c = conn.cursor()
        c.execute("SELECT payload, expires_at FROM handles WHERE handle_id = ?", (handle_id,))
        row = c.fetchone()
        conn.close()
        
        if not row:
            return f"[Error] Unknown handle: {handle_id}"
            
        payload, expires_at = row
        if time.time() > expires_at:
            # Expired. Clean it up from focus state.
            for slot, stack in _focus_state.items():
                _focus_state[slot] = deque([item for item in stack if item[1] != handle_id], maxlen=5)
            return f"[Expired] Handle {handle_id} has expired."
            
        return payload

def _eviction_worker():
    """Maintain an extractive rolling summary without another paid model call."""
    global _rolling_summary
    
    while True:
        try:
            # If shutting down and queue is empty, exit.
            if _shutdown_event.is_set() and _eviction_queue.empty():
                break
                
            turn = _eviction_queue.get(timeout=1.0)
            
            user_text = turn.get("user", "")
            assistant_text = turn.get("assistant", "")
            turn_id = turn.get("id", f"{_session_id}_unknown")
            origin = turn.get("origin", "user_stated")
            
            if origin not in ["user_stated", "verified_api"]:
                _eviction_queue.task_done()
                continue
            
            entry=f"User: {user_text[:500]}\nAssistant: {assistant_text[:800]}"
            with _lock:
                combined=(f"{_rolling_summary}\n{entry}" if _rolling_summary else entry)
                _rolling_summary=combined[-3000:]
            
            _eviction_queue.task_done()
            
        except queue.Empty:
            continue
        except Exception as e:
            print(f"[Session Context] Eviction worker error: {e}")

threading.Thread(target=_eviction_worker, daemon=True, name="vanta-evictor").start()

def get_current_turn_id():
    with _lock:
        if not _turns:
            return f"{_session_id}_unknown"
        return _turns[-1]["id"]

def get_session_id():
    with _lock:
        return _session_id

def _chat_excerpt(text, limit=1500):
    """Bound prompt context without confusing that budget with evidence storage."""
    value = ' '.join(str(text or '').split())
    if len(value) <= limit:
        return value
    marker = ' ...[excerpt]... '
    budget = max(0, limit - len(marker))
    head = budget * 2 // 3
    return value[:head] + marker + value[-(budget-head):]


def record_user(text, origin="user_stated"):
    global _turn_counter
    evidence = str(text or '').strip()
    cleaned = _chat_excerpt(evidence)
    if not cleaned:
        return
    with _lock:
        _turn_counter += 1
        turn_id = f"{_session_id}_{_turn_counter}"
        
        if len(_turns) == MAX_TURNS:
            evicted = _turns.popleft()
            if evicted["origin"] in ["user_stated", "verified_api"]:
                _eviction_queue.put(evicted)
            
        _turns.append({"id": turn_id, "user": cleaned, "assistant": "", "origin": origin})
    try:
        _graph_store.upsert_dialogue_turn(_session_id,turn_id,"user",evidence,_turn_counter*2)
    except Exception as exc:
        print(f"[Session Context] Could not persist user turn: {exc}")

def record_assistant(text):
    evidence = str(text or '').strip()
    cleaned = _chat_excerpt(evidence)
    if not cleaned:
        return
    turn_id = None
    user_text = ""
    with _lock:
        if _turns:
            _turns[-1]["assistant"] = cleaned
            turn_id = _turns[-1]["id"]
            user_text = _turns[-1]["user"]
    if turn_id:
        try:
            _graph_store.upsert_dialogue_turn(_session_id,turn_id,"assistant",evidence,_turn_counter*2+1)
            save_conversation(user_text,evidence,doc_id=turn_id)
        except Exception as exc:
            print(f"[Session Context] Could not persist assistant turn: {exc}")

def _legacy_contextualize_user_input(text):
    """Ground shorthand such as 'do it' in the immediately preceding exchange."""
    cleaned = " ".join(str(text or "").split())
    shorthand = re.fullmatch(r"(?i)(?:yes[, ]*)?(?:do|start|continue|send|make|open|save|use|try)\s*(?:it|that|this)?[.!]?",cleaned)
    references = re.search(
        r"(?i)\b(it|that|this|those|them|they|their|her|hers|him|his|"
        r"the same|go ahead)\b", cleaned)
    # Short acknowledgement/permission replies are elliptical even when they
    # omit a pronoun: "Fine, I'll grant -- how should I?" only makes sense
    # against VANTA's immediately preceding proposal.
    short_reply = len(cleaned.split()) <= 16 and re.search(
        r"(?i)^(?:fine|okay|ok|sure|yes|alright|right|great|i(?:'|’)ll|ill)\b|\b(?:grant|allow|permission|how should i)\b",
        cleaned,
    )
    artifact_reference = re.search(
        r"(?i)\b(?:it|that|this|the\s+(?:file|screenshot|image|capture|result)|"
        r"latest\s+(?:file|screenshot|image|capture))\b", cleaned)
    artifact_context = ""
    if artifact_reference:
        try:
            import artifact_context
            artifact_context = artifact_context.grounding_text()
        except Exception:
            artifact_context = ""
    if not (shorthand or references or short_reply):
        return (cleaned + ("\n\n" + artifact_context if artifact_context else ""))
    with _lock:
        prior = [turn for turn in list(_turns)[:-1] if turn.get("assistant")]
    if not prior:
        return (cleaned + ("\n\n" + artifact_context if artifact_context else ""))
    last = prior[-1]
    return (f"{cleaned}\n\n[RESOLVED CONVERSATIONAL REFERENCE]\n"
            f"Previous user: {last['user']}\nPrevious VANTA response: {last['assistant']}\n"
            "Interpret the current instruction against that exchange. Execute the most recently proposed concrete action; do not ask what 'it' means unless multiple consequential actions remain ambiguous."
            + ("\n\n" + artifact_context if artifact_context else ""))

def contextualize_user_input(text):
    """Attach bounded continuity evidence without rewriting the instruction.

    Every turn receives the same small structured exchange. The model resolves
    references; a phrase table no longer decides that an old assistant proposal
    must be executed.
    """
    cleaned = " ".join(str(text or "").split())
    with _lock:
        prior = [turn for turn in list(_turns)[:-1] if turn.get("assistant")]
    if not prior:
        return cleaned
    lines = [cleaned, "", "[RESOLVED CONVERSATIONAL REFERENCE]"]
    for turn in prior[-2:]:
        lines.append(f"Previous user: {_chat_excerpt(turn['user'], 700)}")
        lines.append(f"Previous VANTA response: {_chat_excerpt(turn['assistant'], 700)}")
    lines.append(
        "This is continuity evidence, not a new instruction. Resolve references "
        "from it when relevant; do not resume completed work or execute an old "
        "assistant proposal unless the current turn actually requests it.")
    return "\n".join(lines)


def resolve_handle(handle_id):
    return _resolve_graph_handle(handle_id)

def add_handle(description, raw_data, slot=None, ttl_seconds=7200):
    handle_id = f"{_session_id}_H_{uuid.uuid4().hex[:6].upper()}"
    _graph_store.put_handle(handle_id, description, raw_data, time.time() + ttl_seconds)
    with _lock:
        now = time.time()
        _focus_state["last_item"].append((now, handle_id))
        if slot and slot in _focus_state:
            _focus_state[slot].append((now, handle_id))
    return f"[{description}] (Handle: {handle_id})"
