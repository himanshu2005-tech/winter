"""VANTA memory API backed exclusively by Neo4j."""
from __future__ import annotations
import hashlib, json, os, queue, re, threading
from memory.neo4j_store import chunk_text, store

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VAULT_DIR = os.path.join(BASE_DIR, "vault")
TRACKER_FILE = os.path.join(VAULT_DIR, "system", "indexed_files.json")
_index_lock = threading.Lock()
MAX_INDEX_FILE_BYTES = int(os.getenv("VANTA_MAX_INDEX_FILE_BYTES", str(25 * 1024 * 1024)))
MAX_INDEX_CHUNKS_PER_FILE = int(os.getenv("VANTA_MAX_INDEX_CHUNKS_PER_FILE", "512"))
INDEX_BATCH_SIZE = 32

EXCLUDED_INDEX_DIRS = {
    "chroma_db", "memory", "system", "neo4j", "backups", "downloads",
    "__pycache__", "cache", "cache2", "code cache", "gpu cache",
    "service worker", "node_modules", ".git", ".venv", ".pytest_cache",
    ".mypy_cache", ".ruff_cache", ".next", ".nuxt", ".build", "build",
    "dist", "coverage", "logs", "console_profile",
}
EXCLUDED_INDEX_SUFFIXES = {".log", ".map", ".tmp", ".cache", ".pyc", ".pyo"}

# Files that must never be indexed, matched on basename anywhere in the vault.
# The directory list below keeps out binary noise; this keeps out secrets. The
# credential store lives at vault/passwords.json, so indexing copied every
# secret in it into the graph as retrievable plaintext -- which walked straight
# around GET_PASSWORD, the action that is meant to be the only way to reach
# them. Retrieval needs no authorisation, so an indexed copy is a copy with the
# lock taken off. Add more with WINTER_NOINDEX=name.json,other.txt in .env.
SECRET_FILES = {
    "passwords.json", "credentials.json", "token.json", "secrets.json",
    ".env", ".env.local",
}


def _never_index(path):
    name = os.path.basename(path).lower()
    if name in SECRET_FILES:
        return True
    extra = os.getenv("WINTER_NOINDEX", "")
    return any(name == item.strip().lower()
               for item in extra.split(",") if item.strip())

def _excluded_directory(name):
    value = str(name or "").lower()
    return (value in EXCLUDED_INDEX_DIRS
            or value.startswith("browser_profile"))


def _indexable_path(path):
    """Whether a file is user knowledge rather than generated runtime noise."""
    absolute = os.path.abspath(str(path or ""))
    try:
        relative = os.path.relpath(absolute, VAULT_DIR)
    except ValueError:
        return False
    if relative == os.pardir or relative.startswith(os.pardir + os.sep):
        return False
    parts = [part.casefold() for part in relative.split(os.sep)]
    if any(_excluded_directory(part) for part in parts[:-1]):
        return False
    if os.path.splitext(parts[-1])[1] in EXCLUDED_INDEX_SUFFIXES:
        return False
    return not _never_index(absolute)


def file_index_cleanup_plan():
    """Return regenerable file-memory rows that no longer belong in search.

    Facts, conversations, interview evidence and research are intentionally
    outside this plan.  Only derived ``kind=file`` chunks are considered.
    """
    try:
        with open(TRACKER_FILE, "r", encoding="utf-8") as handle:
            tracked = json.load(handle)
    except Exception:
        tracked = {}
    rows = store.query(
        "MATCH (m:Memory {kind:'file'}) "
        "RETURN m.source AS source,coalesce(m.source_id,'') AS digest,"
        "count(*) AS chunks")
    by_source = {}
    for row in rows:
        source = str(row.get("source") or "")
        by_source.setdefault(source, []).append(
            (str(row.get("digest") or ""), int(row.get("chunks") or 0)))
    plan = []
    for source, versions in by_source.items():
        keep_digest = tracked.get(source)
        reason = None
        if not _indexable_path(source):
            keep_digest, reason = None, "excluded_generated_source"
        elif not os.path.isfile(source):
            keep_digest, reason = None, "source_missing"
        elif not keep_digest:
            # An untracked source may be a partial run. It is safe to remove:
            # the next completed index pass rebuilds it from the source file.
            reason = "untracked_derived_index"
        else:
            reason = "stale_file_version"
        count = sum(chunks for digest, chunks in versions
                    if keep_digest is None or digest != keep_digest)
        if count:
            plan.append({"source": source, "keep_digest": keep_digest,
                         "chunks": count, "reason": reason})
    return plan


def prune_stale_file_memories(*, dry_run=True):
    """Remove only derived file-index noise; source files remain untouched."""
    plan = file_index_cleanup_plan()
    if dry_run or not plan:
        return {"sources": len(plan),
                "chunks": sum(item["chunks"] for item in plan),
                "by_reason": _count_cleanup_reasons(plan), "plan": plan}
    deleted = 0
    for start in range(0, len(plan), 50):
        batch = plan[start:start + 50]
        rows = store.query(
            """
            UNWIND $rows AS row
            MATCH (m:Memory {kind:'file',source:row.source})
            WHERE row.keep_digest IS NULL
               OR coalesce(m.source_id,'')<>row.keep_digest
            WITH m DETACH DELETE m
            RETURN count(*) AS deleted
            """,
            rows=batch,
        )
        deleted += int(rows[0]["deleted"]) if rows else 0
    return {"sources": len(plan), "chunks": deleted,
            "by_reason": _count_cleanup_reasons(plan), "plan": plan}


def prune_duplicate_file_chunks(*, dry_run=True):
    """Collapse exact duplicates inside one current derived file version."""
    groups = store.query(
        """
        MATCH (m:Memory {kind:'file'})
        WHERE m.content_hash IS NOT NULL
        WITH m.source AS source,coalesce(m.source_id,'') AS source_id,
             m.content_hash AS content_hash,collect(m.id) AS ids
        WHERE size(ids)>1
        RETURN source,source_id,content_hash,ids
        """)
    remove_ids = []
    for group in groups:
        ids = sorted(str(value) for value in group.get("ids", []) if value)
        remove_ids.extend(ids[1:])
    if not dry_run:
        for start in range(0, len(remove_ids), 1000):
            store.query(
                "MATCH (m:Memory) WHERE m.id IN $ids DETACH DELETE m",
                ids=remove_ids[start:start + 1000],
            )
    return {"groups": len(groups), "chunks": len(remove_ids)}


def _count_cleanup_reasons(plan):
    counts = {}
    for item in plan:
        reason = item["reason"]
        counts[reason] = counts.get(reason, 0) + int(item["chunks"])
    return counts

def get_file_hash(path):
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as handle:
            for block in iter(lambda: handle.read(65536), b""): digest.update(block)
        return digest.hexdigest()
    except OSError: return None

def extract_text(path):
    """Extract human-meaningful text without guessing at opaque binaries."""
    ext = os.path.splitext(path)[1].lower()
    try:
        if ext == ".pdf":
            import pymupdf
            with pymupdf.open(path) as doc: return "\n".join(page.get_text() for page in doc)
        if ext == ".pptx":
            from pptx import Presentation
            return "\n".join(shape.text for slide in Presentation(path).slides for shape in slide.shapes if hasattr(shape, "text"))
        if ext == ".docx":
            import docx
            doc = docx.Document(path)
            paragraphs = [p.text for p in doc.paragraphs]
            tables = [" | ".join(cell.text for cell in row.cells)
                      for table in doc.tables for row in table.rows]
            return "\n".join(paragraphs + tables)
        if ext in {".xlsx", ".xls", ".ods", ".parquet"}:
            import pandas as pd
            if ext == ".parquet":
                frames = {"data": pd.read_parquet(path)}
            elif ext == ".ods":
                frames = pd.read_excel(path, sheet_name=None, engine="odf")
            else:
                frames = pd.read_excel(path, sheet_name=None)
            return "\n\n".join(
                f"[Sheet {name}]\n{frame.to_csv(index=False)}"
                for name, frame in frames.items())
        if ext == ".ipynb":
            with open(path, "r", encoding="utf-8", errors="replace") as handle:
                notebook = json.load(handle)
            cells = []
            for cell in notebook.get("cells", []):
                source = "".join(cell.get("source") or [])
                if source.strip():
                    cells.append(f"[{cell.get('cell_type', 'cell')}]\n{source}")
            return "\n\n".join(cells)
        if ext in {".png", ".jpg", ".jpeg", ".bmp", ".tiff", ".webp"}:
            try:
                import pytesseract
                from PIL import Image
                return pytesseract.image_to_string(Image.open(path))
            except Exception:
                return ""
        text_exts = {".txt", ".md", ".rst", ".json", ".jsonl", ".yaml", ".yml",
                     ".toml", ".ini", ".cfg", ".xml", ".html", ".htm", ".csv",
                     ".tsv", ".log", ".py", ".js", ".jsx", ".ts", ".tsx", ".java",
                     ".c", ".h", ".cpp", ".hpp", ".cs", ".go", ".rs", ".rb", ".php",
                     ".sql", ".sh", ".ps1", ".bat", ".css", ".scss", ".vue", ".svelte"}
        if ext in text_exts:
            with open(path,"r",encoding="utf-8",errors="ignore") as handle: return handle.read()
        # Extensionless and unfamiliar files may still be plain text. Null
        # bytes are a conservative binary signal; decoding quality prevents a
        # random executable from becoming graph content.
        with open(path, "rb") as handle:
            sample = handle.read(65536)
        if sample and b"\x00" not in sample:
            decoded = sample.decode("utf-8", errors="replace")
            if decoded.count("\ufffd") / max(1, len(decoded)) < 0.01:
                with open(path, "r", encoding="utf-8", errors="replace") as handle:
                    return handle.read()
    except Exception as exc: print(f"[Memory] Failed to read {path}: {exc}")
    return ""

def save_knowledge(title, text, topic="general", source="web_research"):
    if not str(text or "").strip(): return 0
    return store.save_knowledge(title,text,topic or "general",source)

def _format_rows(rows):
    if not rows: return ""
    return "\n\n".join(f"--- [{str(r.get('kind','memory')).upper()}] {r.get('title') or r.get('source','unknown')} ---\n{r.get('content','')}" for r in rows)

def search_vault(query,n_results=5,topic=None,type_filter=None):
    rows = store.search_memories(query,limit=n_results,topic=topic,kind=type_filter)
    if not rows and topic: rows = store.search_memories(query,limit=n_results,kind=type_filter)
    return _format_rows(rows) or "No relevant information found in the knowledge graph."

def save_conversation(user_text,agent_text,doc_id=None):
    return store.save_conversation(user_text,agent_text,doc_id)

def recall_conversation(query,n_results=2):
    return _format_rows(store.search_memories(query,limit=n_results,kind="conversation"))

def get_recent_conversations(n=20): return store.recent_conversations(n)

def list_vault_topics():
    rows = store.query("MATCH (m:Memory) RETURN m.kind AS kind,m.topic AS topic,count(*) AS count")
    return "Knowledge graph is empty." if not rows else "\n".join(f"{r.get('kind')} / {r.get('topic')}: {r.get('count')}" for r in rows)

def _bounded_call(fn,timeout_seconds,default,label):
    results = queue.Queue(maxsize=1)
    def worker():
        try: results.put((True,fn()),block=False)
        except Exception as exc: results.put((False,exc),block=False)
    threading.Thread(target=worker,daemon=True,name=f"vanta-{label}").start()
    try: ok,value = results.get(timeout=max(.1,float(timeout_seconds)))
    except queue.Empty: return default
    if not ok:
        print(f"[Memory] {label} failed: {value}")
        return default
    return value

def recall_conversation_timed(query,n_results=2,timeout_seconds=4.0):
    return _bounded_call(lambda:recall_conversation(query,n_results),timeout_seconds,"","conversation-recall")

def get_recent_conversations_timed(n=20,timeout_seconds=4.0):
    return _bounded_call(lambda:get_recent_conversations(n),timeout_seconds,[],"recent-conversations")

def index_vault():
    """Index changed user files without deleting or modifying their sources."""
    if not _index_lock.acquire(blocking=False): return
    try:
        os.makedirs(os.path.dirname(TRACKER_FILE),exist_ok=True)
        try:
            with open(TRACKER_FILE,"r",encoding="utf-8") as handle: old=json.load(handle)
        except Exception: old={}
        current={}
        for root,dirs,files in os.walk(VAULT_DIR):
            # browser_profile holds Chrome's leveldb and session logs: binary
            # noise that embeds into meaningless vectors and crowds out real
            # study material in every search result.
            dirs[:]=[d for d in dirs if not _excluded_directory(d)]
            for filename in files:
                path=os.path.join(root,filename)
                if not _indexable_path(path):
                    continue
                try:
                    if os.path.getsize(path) > MAX_INDEX_FILE_BYTES:
                        continue
                except OSError:
                    continue
                digest=get_file_hash(path)
                if not digest: continue
                current[path]=digest
                if old.get(path)==digest: continue
                content=extract_text(path)
                if not content.strip(): continue
                title=os.path.relpath(path,VAULT_DIR)
                parts = []
                seen_parts = set()
                for part in chunk_text(content):
                    digest_part = hashlib.sha256(
                        part.encode("utf-8")).hexdigest()
                    if digest_part in seen_parts:
                        continue
                    seen_parts.add(digest_part)
                    parts.append(part)
                    if len(parts) >= MAX_INDEX_CHUNKS_PER_FILE:
                        break
                items=[{"id":f"file_{digest}_{i}","kind":"file","topic":"vault","source":path,"source_id":digest,"title":title,"content":part,"authority":55} for i,part in enumerate(parts)]
                # One unindexable file used to kill the whole thread, and the
                # tracker was written only at the end -- so a failure halfway
                # through meant every file already embedded was re-embedded on
                # the next run, and the run after that, forever. Per-file
                # containment, and the file is left out of `current` so it is
                # retried next time rather than being marked done.
                try:
                    for start in range(0, len(items), INDEX_BATCH_SIZE):
                        store.upsert_memory_batch(items[start:start + INDEX_BATCH_SIZE])
                    # File memories are a rebuildable search index, not source
                    # evidence. Once the new version is complete, remove old
                    # chunks for this source so each edit does not permanently
                    # multiply retrieval candidates.
                    store.query(
                        """
                        MATCH (m:Memory {kind:'file',source:$source})
                        WHERE coalesce(m.source_id,'')<>$digest
                        DETACH DELETE m
                        """,
                        source=path, digest=digest,
                    )
                except Exception as exc:
                    current.pop(path, None)
                    print(f"[Memory] Could not index {title} "
                          f"({len(items)} chunks): {exc}")
        # Remove derived chunks for files that disappeared or became excluded.
        for stale_source in set(old).difference(current):
            store.query(
                "MATCH (m:Memory {kind:'file',source:$source}) DETACH DELETE m",
                source=stale_source,
            )
        with open(TRACKER_FILE,"w",encoding="utf-8") as handle: json.dump(current,handle,indent=2)
    finally: _index_lock.release()
