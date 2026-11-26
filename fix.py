import queue

with open('d:\\winter\\memory\\__init__.py', 'r', encoding='utf-8') as f:
    lines = f.readlines()

new_lines = []
for line in lines:
    if line.startswith('def _bounded_call'):
        break
    new_lines.append(line)

new_content = "".join(new_lines) + """def _bounded_call(fn, timeout_seconds, default, label):
    result_queue = queue.Queue(maxsize=1)

    def worker():
        try:
            result_queue.put((True, fn()), block=False)
        except Exception as exc:
            result_queue.put((False, exc), block=False)

    import threading
    threading.Thread(target=worker, daemon=True, name=f"vanta-{label}").start()
    try:
        ok, value = result_queue.get(timeout=max(0.1, float(timeout_seconds)))
    except queue.Empty:
        print(f"[Memory Timeout] {label} exceeded {timeout_seconds}s; continuing without it.")
        return default
    if not ok:
        print(f"[Memory] {label} failed: {value}")
        return default
    return value

def recall_conversation_timed(query, n_results=2, timeout_seconds=4.0):
    return _bounded_call(
        lambda: recall_conversation(query, n_results=n_results),
        timeout_seconds,
        "",
        "conversation recall",
    )

def get_recent_conversations_timed(n=20, timeout_seconds=4.0):
    return _bounded_call(
        lambda: get_recent_conversations(n=n),
        timeout_seconds,
        [],
        "recent conversation load",
    )

def save_knowledge(text, source, title, topic=None):
    if not text.strip():
        return
    if topic == "general" or not topic:
        import re
        words = re.sub(r'[^a-z0-9\s]', '', title.lower()).split()
        stopwords = {"the", "a", "an", "of", "in", "for", "and", "or", "to", "is", "on", "from", "by", "with"}
        meaningful = [w for w in words if w not in stopwords][:4]
        topic = "_".join(meaningful) if meaningful else "general"
    
    print(f"[Memory] Saving knowledge: '{title}' [topic={topic}]...")
    col = get_collection()
    chunks = chunk_text(text)
    if chunks:
        import time
        ts = int(time.time())
        ids = [f"research_{topic}_{ts}_{i}" for i in range(len(chunks))]
        metadatas = [{
            "type": "research",
            "topic": topic,
            "source": source,
            "title": title[:100],
            "chunk": i,
            "timestamp": str(ts)
        } for i in range(len(chunks))]
        col.add(documents=chunks, metadatas=metadatas, ids=ids)
"""

with open('d:\\winter\\memory\\__init__.py', 'w', encoding='utf-8') as f:
    f.write(new_content)
