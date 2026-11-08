import os
import time
import sqlite3
import datetime
from vision import _grab_active_window, _grab_full_desktop, _collect_ocr_elements

def get_db_path(base_dir):
    vault_dir = os.path.join(base_dir, "vault")
    os.makedirs(vault_dir, exist_ok=True)
    return os.path.join(vault_dir, "system/recall.db")

def init_db(base_dir):
    db_path = get_db_path(base_dir)
    conn = sqlite3.connect(db_path)
    c = conn.cursor()
    # Create an FTS5 table for fast full-text search
    c.execute('''
        CREATE VIRTUAL TABLE IF NOT EXISTS recall USING fts5(
            timestamp UNINDEXED,
            content
        )
    ''')
    conn.commit()
    conn.close()

def search_recall(base_dir, query):
    db_path = get_db_path(base_dir)
    if not os.path.exists(db_path):
        return "Recall database is empty."
        
    try:
        conn = sqlite3.connect(db_path)
        c = conn.cursor()
        
        # Clean query for FTS5
        clean_query = query.replace('"', '').replace("'", "")
        
        c.execute('''
            SELECT timestamp, content FROM recall 
            WHERE content MATCH ? 
            ORDER BY timestamp DESC LIMIT 10
        ''', (f'"{clean_query}"',))
        
        rows = c.fetchall()
        if not rows:
            # Fallback to LIKE if MATCH fails or finds nothing
            c.execute('''
                SELECT timestamp, content FROM recall 
                WHERE content LIKE ? 
                ORDER BY timestamp DESC LIMIT 10
            ''', (f'%{clean_query}%',))
            rows = c.fetchall()
            
        conn.close()
        
        if not rows:
            return f"No visual memory found containing: '{query}'"
            
        results = [f"Found {len(rows)} memories matching '{query}':"]
        for ts, content in rows:
            # Truncate content slightly for display
            snippet = content[:500] + ("..." if len(content) > 500 else "")
            results.append(f"[{ts}] {snippet}")
            
        return "\n\n".join(results)
    except Exception as e:
        return f"Error searching recall memory: {e}"

def recall_worker(base_dir, ui_queue=None):
    print("[Recall] Photographic Memory worker started.")
    init_db(base_dir)
    db_path = get_db_path(base_dir)

    STUDY_DOMAINS = [
        "geeksforgeeks", "wikipedia", "w3schools", "studysmarter",
        "khanacademy", "coursera", "udemy", "nptel", "stackoverflow",
        "tutorialspoint", "javatpoint", "medium.com", "towardsdatascience"
    ]
    MEETING_TITLES = ["meet.google.com", "microsoft teams", "zoom meeting", "google meet"]

    study_consecutive = 0       # how many consecutive captures found study content
    meeting_active = False
    meeting_captures = []
    last_wa_text = ""

    while True:
        time.sleep(180)  # Base interval: 3 minutes

        try:
            screenshot = _grab_active_window()
            if screenshot is None:
                screenshot = _grab_full_desktop()

            if screenshot:
                if ui_queue:
                    ui_queue.put(("flash_screen", ""))

                rects = _collect_ocr_elements(screenshot, max_regions=200)
                if rects:
                    screen_text = " ".join([r[5] for r in rects if len(r) > 5 and r[5]])
                    if screen_text.strip():
                        # Save to DB
                        conn = sqlite3.connect(db_path)
                        c = conn.cursor()
                        timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                        c.execute("INSERT INTO recall (timestamp, content) VALUES (?, ?)", (timestamp, screen_text))
                        conn.commit()
                        conn.close()
                        print(f"[Recall] Saved snapshot at {timestamp} with {len(screen_text)} chars.")

                        # --- Study Detection ---
                        text_lower = screen_text.lower()
                        is_study = any(d in text_lower for d in STUDY_DOMAINS)
                        if is_study:
                            study_consecutive += 1
                            if study_consecutive >= 2 and ui_queue:
                                # Extract a likely topic (first capitalized phrase)
                                words = screen_text.split()
                                topic_words = [w for w in words[:30] if len(w) > 4 and w[0].isupper()]
                                topic = " ".join(topic_words[:4]) if topic_words else "this topic"
                                hint = f"Looks like you're studying {topic}. Hold Ctrl and ask me to summarize or quiz you."
                                print(f"[Recall] Study hint: {hint}")
                                ui_queue.put(("info", hint))
                                study_consecutive = 0  # Reset after notifying
                        else:
                            study_consecutive = 0

                        # --- WhatsApp Web Watcher ---
                        nonlocal_text = screen_text  # capture for closure
                        try:
                            import subprocess
                            titles_raw = subprocess.check_output(
                                'powershell -command "Get-Process | Where-Object {$_.MainWindowTitle -ne \'\'} | Select-Object MainWindowTitle | Format-Table -HideTableHeaders"',
                                shell=True, text=True, stderr=subprocess.DEVNULL
                            )
                            titles = titles_raw.lower()
                            if "whatsapp" in titles:
                                # Look for message-like content in OCR text
                                wa_keywords = ["message", "type a message", "pm", "am", "ago", "delivered", "read"]
                                if any(kw in text_lower for kw in wa_keywords):
                                    if screen_text != last_wa_text and len(screen_text) > 50:
                                        last_wa_text = screen_text
                                        if ui_queue:
                                            ui_queue.put(("info", f"[WhatsApp] New activity detected on screen."))
                        except Exception:
                            pass

        except Exception as e:
            print(f"[Recall Error] Failed to capture memory: {e}")

