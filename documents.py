# -*- coding: utf-8 -*-
"""
documents.py — reading Himanshu's study material.

Slides and papers are where the actual work is, and until now VANTA could not
open any of them. It could search a vault of plain text and it could look at
whatever happened to be on screen, but a PDF sitting in vault/docs was opaque.

Handles PDF (PyMuPDF), PPTX (python-pptx), DOCX where available, and plain
text. Returns structure rather than a wall of characters: page and slide
boundaries survive, because "explain slide 7" is the normal request and it is
unanswerable once everything has been flattened.

Pages that carry no extractable text are reported as image-only rather than as
empty, so a scanned chapter is diagnosed instead of silently returning nothing.
"""

import os
import re

SUPPORTED = (".pdf", ".pptx", ".ppt", ".docx", ".txt", ".md", ".rst",
             ".csv", ".tsv", ".xlsx", ".xls", ".ods", ".parquet",
             ".json", ".jsonl", ".yaml", ".yml", ".toml", ".xml",
             ".html", ".htm", ".ipynb", ".py", ".js", ".jsx", ".ts",
             ".tsx", ".java", ".c", ".cpp", ".h", ".hpp", ".cs", ".go",
             ".rs", ".rb", ".php", ".sql", ".log", ".png", ".jpg",
             ".jpeg", ".bmp", ".tiff", ".webp")

MAX_CHARS_DEFAULT = 6000


def _vault_docs(base_dir):
    return os.path.join(base_dir, "vault", "docs")


def resolve(base_dir, name):
    """Find a document by name, path fragment, or exact path."""
    raw = str(name or "").strip().strip('"').strip("'")
    if not raw:
        return None, "[Error] Which document? Give a file name, or use LIST_DOCUMENTS."
    if os.path.isabs(raw) and os.path.exists(raw):
        return raw, None

    docs_dir = _vault_docs(base_dir)
    candidates = []
    for root, _dirs, files in os.walk(docs_dir):
        for fname in files:
            if fname.lower().endswith(SUPPORTED):
                candidates.append(os.path.join(root, fname))
    # Also allow a path relative to the project root.
    direct = os.path.join(base_dir, raw)
    if os.path.exists(direct) and direct.lower().endswith(SUPPORTED):
        return direct, None

    wanted = raw.casefold()
    exact = [p for p in candidates if os.path.basename(p).casefold() == wanted]
    if exact:
        return exact[0], None
    stem = [p for p in candidates
            if os.path.splitext(os.path.basename(p))[0].casefold() == wanted]
    if stem:
        return stem[0], None
    partial = [p for p in candidates if wanted in os.path.basename(p).casefold()]
    if len(partial) == 1:
        return partial[0], None
    if len(partial) > 1:
        names = ", ".join(sorted(os.path.basename(p) for p in partial[:8]))
        return None, f"[Error] '{raw}' matches several documents: {names}. Name one exactly."
    if not candidates:
        return None, (
            f"[Error] No documents found in {docs_dir}. Put PDFs or slides there, "
            "or give an absolute path."
        )
    return None, (
        f"[Error] No document matching '{raw}'. Use LIST_DOCUMENTS to see what is "
        "available, or FIND_FILES to look outside the vault. Do not guess another name."
    )


def list_documents(base_dir):
    docs_dir = _vault_docs(base_dir)
    if not os.path.isdir(docs_dir):
        return f"No document folder yet. Create {docs_dir} and put your material there."
    rows = []
    for root, _dirs, files in os.walk(docs_dir):
        for fname in sorted(files):
            if not fname.lower().endswith(SUPPORTED):
                continue
            path = os.path.join(root, fname)
            rel = os.path.relpath(path, docs_dir)
            size = os.path.getsize(path) / 1024
            rows.append(f"- {rel}  ({size:.0f} KB)")
    if not rows:
        return f"{docs_dir} has no readable documents yet."
    return f"Documents in vault/docs ({len(rows)}):\n" + "\n".join(rows)


def _clean(text):
    text = re.sub(r"[ \t]+", " ", str(text or ""))
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _read_pdf(path, first=None, last=None):
    import pymupdf
    doc = pymupdf.open(path)
    total = doc.page_count
    start = max(1, int(first or 1))
    end = min(total, int(last or total))
    sections = []
    for index in range(start - 1, end):
        page = doc.load_page(index)
        text = _clean(page.get_text("text"))
        if not text:
            images = len(page.get_images(full=True))
            text = (
                f"(no selectable text on this page{'; ' + str(images) + ' image(s)' if images else ''} "
                "— it is likely a scan or a diagram. Ask me to look at it on screen instead.)"
            )
        sections.append((f"Page {index + 1}", text))
    doc.close()
    return sections, total, "page"


def _read_pptx(path, first=None, last=None):
    from pptx import Presentation
    deck = Presentation(path)
    slides = list(deck.slides)
    total = len(slides)
    start = max(1, int(first or 1))
    end = min(total, int(last or total))
    sections = []
    for index in range(start - 1, end):
        slide = slides[index]
        title = ""
        body, notes = [], ""
        for shape in slide.shapes:
            if shape.has_text_frame:
                text = _clean(shape.text_frame.text)
                if not text:
                    continue
                if shape == slide.shapes.title and not title:
                    title = text
                else:
                    body.append(text)
            elif shape.shape_type == 13:  # picture
                body.append("(image on this slide)")
            elif getattr(shape, "has_table", False):
                rows = []
                for row in shape.table.rows:
                    rows.append(" | ".join(_clean(c.text) for c in row.cells))
                body.append("Table:\n" + "\n".join(rows))
        try:
            if slide.has_notes_slide:
                notes = _clean(slide.notes_slide.notes_text_frame.text)
        except Exception:
            notes = ""
        label = f"Slide {index + 1}" + (f" — {title}" if title else "")
        content = "\n".join(body) or "(no text on this slide)"
        if notes:
            content += f"\n[Speaker notes] {notes}"
        sections.append((label, content))
    return sections, total, "slide"


def _read_docx(path, first=None, last=None):
    import docx
    document = docx.Document(path)
    paragraphs = [_clean(p.text) for p in document.paragraphs if _clean(p.text)]
    return [("Document", "\n".join(paragraphs))], 1, "section"


def _read_text(path, first=None, last=None):
    with open(path, "r", encoding="utf-8", errors="replace") as handle:
        return [("Document", _clean(handle.read()))], 1, "section"


_READERS = {
    ".pdf": _read_pdf, ".pptx": _read_pptx, ".ppt": _read_pptx,
    ".docx": _read_docx, ".txt": _read_text, ".md": _read_text, ".csv": _read_text,
}


def read_document(base_dir, name, first=None, last=None, max_chars=MAX_CHARS_DEFAULT,
                  outline=False):
    """Extract a document's text, preserving page/slide structure."""
    path, error = resolve(base_dir, name)
    if error:
        return error
    ext = os.path.splitext(path)[1].lower()
    reader = _READERS.get(ext)
    if not reader:
        return f"[Error] I cannot read {ext} files yet."
    try:
        sections, total, unit = reader(path, first, last)
    except Exception as exc:
        return f"[Error] Could not read {os.path.basename(path)}: {exc}"
    if not sections:
        return f"[Error] {os.path.basename(path)} appears to be empty."

    header = f"{os.path.basename(path)} — {total} {unit}{'s' if total != 1 else ''}"
    if outline:
        lines = [f"{header}\n"]
        for label, body in sections:
            summary = " ".join(body.split())[:110]
            lines.append(f"- {label}: {summary}")
        return "\n".join(lines)

    if first or last:
        header += f" (showing {unit}s {max(1, int(first or 1))}–{min(total, int(last or total))})"
    parts, used = [f"{header}\n"], 0
    truncated = False
    for label, body in sections:
        block = f"--- {label} ---\n{body}\n"
        if used + len(block) > int(max_chars):
            # A single page longer than the whole budget must still be shown,
            # cut short, rather than returning a document with nothing in it.
            if not used:
                room = max(400, int(max_chars) - len(label) - 40)
                parts.append(f"--- {label} ---\n{body[:room]}\n[...page continues]\n")
            truncated = True
            break
        parts.append(block)
        used += len(block)
    if truncated:
        shown = len(parts) - 1
        parts.append(
            f"\n[Showing the first {shown} {unit}(s) of {total}. Ask for a specific range "
            f"with first= and last=, or outline=\"true\" for the whole structure.]"
        )
    return "\n".join(parts)


_SKIP_DIRS = {
    "node_modules", "__pycache__", ".git", ".venv", "venv", "AppData",
    "Windows", "Program Files", "Program Files (x86)", "$Recycle.Bin",
    "System Volume Information", "ProgramData", ".cache", "site-packages",
    "dist", "build", "browser_profile",
}

_COMMON_ROOTS = ("Desktop", "Documents", "Downloads", "Pictures", "Videos", "OneDrive")


def find_files(pattern, root="", limit=25, max_seconds=20):
    """Locate files by name anywhere Himanshu keeps things.

    Reading a file needs its path, and he rarely knows it. Without this, "open
    my resume" is unanswerable unless he types the full path himself.

    Bounded deliberately: a naive walk of C:\\ spends minutes in Windows and
    AppData and returns nothing he wanted.
    """
    import fnmatch
    import time

    raw = str(pattern or "").strip().strip('"').strip("'")
    if not raw:
        return "[Error] FIND_FILES needs something to search for."
    # A bare word means "name contains this"; a pattern with wildcards is used
    # as given.
    matcher = raw if any(ch in raw for ch in "*?[") else f"*{raw}*"

    roots = []
    if root:
        expanded = os.path.expandvars(os.path.expanduser(str(root).strip().strip('"')))
        if not os.path.isdir(expanded):
            return (f"[Error] There is no folder at {expanded}. " + "Use FIND_FILES to locate it rather than guessing another path, or ask Himanshu where it is.")
        roots = [expanded]
    else:
        home = os.path.expanduser("~")
        roots = [os.path.join(home, name) for name in _COMMON_ROOTS
                 if os.path.isdir(os.path.join(home, name))]
        roots.append(os.path.dirname(os.path.abspath(__file__)))

    started = time.monotonic()
    hits, scanned, timed_out = [], 0, False
    for base in roots:
        for current, dirs, files in os.walk(base):
            if time.monotonic() - started > max_seconds:
                timed_out = True
                break
            dirs[:] = [d for d in dirs if d not in _SKIP_DIRS and not d.startswith(".")]
            scanned += len(files)
            for name in files:
                if fnmatch.fnmatch(name.lower(), matcher.lower()):
                    path = os.path.join(current, name)
                    try:
                        stat = os.stat(path)
                        hits.append((stat.st_mtime, path, stat.st_size))
                    except OSError:
                        continue
            if len(hits) >= int(limit) * 4:
                break
        if timed_out or len(hits) >= int(limit) * 4:
            break

    if not hits:
        where = root or "your usual folders"
        return (f"No files matching '{raw}' in {where}"
                + (" (search timed out early)" if timed_out else "")
                + ". Try a different name, or give a folder to search with root=.")

    # Most recently modified first: the file he means is almost always the one
    # he touched last.
    hits.sort(reverse=True)
    import datetime
    lines = [f"{len(hits)} match(es) for '{raw}', newest first:"]
    for mtime, path, size in hits[:int(limit)]:
        when = datetime.datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M")
        lines.append(f"- {path}  ({size/1024:.0f} KB, modified {when})")
    if len(hits) > int(limit):
        lines.append(f"...and {len(hits) - int(limit)} more.")
    if timed_out:
        lines.append("(Search stopped early; narrow it with root= for more.)")
    return "\n".join(lines)


def search_documents(query, limit=6):
    """Semantic search across every indexed document.

    The vault index already holds embedded chunks of every PDF and deck, but
    the same passage is often stored under more than one kind, so an unfiltered
    result set spends half its slots repeating itself. Results are collapsed by
    source and passage before being returned, and each one is cited.
    """
    if not str(query or "").strip():
        return "[Error] SEARCH_DOCS needs a query."
    try:
        from memory.neo4j_store import store
        rows = store.search_memories(query, limit=max(int(limit) * 4, 12))
    except Exception as exc:
        return f"[Error] Document search failed: {exc}"

    seen, results = set(), []
    for row in rows:
        if str(row.get("kind")) not in {"file", "knowledge", "research"}:
            continue
        source = str(row.get("source") or row.get("title") or "")
        body = " ".join(str(row.get("content", "")).split())
        if not body:
            continue
        # The same passage indexed twice under different kinds is one result.
        fingerprint = (os.path.basename(source).casefold(), body[:120].casefold())
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        results.append((os.path.basename(source) or "unknown", body))
        if len(results) >= int(limit):
            break

    if not results:
        return (f"Nothing in your indexed documents matches '{query}'. "
                "Use INDEX_FOLDER if the material has not been indexed yet.")
    lines = [f"{len(results)} passage(s) matching '{query}':"]
    for name, body in results:
        lines.append(f"\n--- {name} ---\n{body[:700]}")
    return "\n".join(lines)


def index_folder(path, recursive=True):
    """Index any folder on disk, not just the vault."""
    raw = str(path or "").strip().strip('"').strip("'")
    if not raw:
        return "[Error] INDEX_FOLDER needs a path."
    if not os.path.isdir(raw):
        return (f"[Error] There is no folder at {raw}. " + "Use FIND_FILES to locate it rather than guessing another path, or ask Himanshu where it is.")
    try:
        from memory import chunk_text, extract_text, get_file_hash
        from memory.neo4j_store import store
    except Exception as exc:
        return f"[Error] Indexer unavailable: {exc}"

    indexed, skipped, chunks = 0, 0, 0
    for root, dirs, files in os.walk(raw):
        dirs[:] = [d for d in dirs if not d.startswith(".") and
                   d.lower() not in {"node_modules", "__pycache__", "venv", ".venv"}]
        for filename in files:
            target = os.path.join(root, filename)
            try:
                from memory import _never_index
                if _never_index(target):
                    skipped += 1
                    continue
                digest = get_file_hash(target)
                content = extract_text(target)
                if not content.strip():
                    skipped += 1
                    continue
                items = [{
                    "id": f"file_{digest}_{i}", "kind": "file", "topic": "indexed",
                    "source": target, "source_id": digest,
                    "title": os.path.relpath(target, raw), "content": part,
                    "authority": 55,
                } for i, part in enumerate(chunk_text(content))]
                store.upsert_memory_batch(items)
                indexed += 1
                chunks += len(items)
            except Exception as exc:
                print(f"[Index] Skipped {target}: {exc}")
                skipped += 1
        if not recursive:
            break
    return (f"[Success] Indexed {indexed} document(s) into {chunks} searchable passages"
            + (f"; {skipped} skipped (no extractable text)." if skipped else "."))


def ingest_path(path, recursive=True):
    """One deterministic intake entry point for a file or folder."""
    raw = os.path.abspath(os.path.expanduser(os.path.expandvars(
        str(path or "").strip().strip('"').strip("'"))))
    if not os.path.exists(raw):
        return f"[Error] There is no file or folder at {raw}. Use FIND_FILES; do not guess."
    if os.path.isdir(raw):
        # A health export is structured personal telemetry, not a document.
        health_exports = []
        for root, _dirs, files in os.walk(raw):
            health_exports.extend(os.path.join(root, name) for name in files
                                  if name.lower() == "export.xml")
        if len(health_exports) == 1:
            try:
                with open(health_exports[0], "rb") as handle:
                    is_health = b"<HealthData" in handle.read(4096)
            except OSError:
                is_health = False
            if is_health:
                import health_import
                return health_import.import_health(os.path.dirname(__file__), raw)
        return index_folder(raw, recursive=recursive)

    if os.path.basename(raw).lower() == "export.xml":
        try:
            with open(raw, "rb") as handle:
                if b"<HealthData" in handle.read(4096):
                    import health_import
                    return health_import.import_health(os.path.dirname(__file__), raw)
        except OSError:
            pass
    from memory import chunk_text, extract_text, get_file_hash, _never_index
    from memory.neo4j_store import store
    if _never_index(raw):
        return "[Error] That file is classified as a credential/secret file and cannot be indexed."
    content = extract_text(raw)
    if not content.strip():
        return (f"[Unsupported] {os.path.basename(raw)} contains no safely extractable text. "
                "The source was not altered and no empty knowledge was stored.")
    digest = get_file_hash(raw)
    items = [{"id": f"file_{digest}_{i}", "kind": "file", "topic": "indexed",
              "source": raw, "source_id": digest, "title": os.path.basename(raw),
              "content": part, "authority": 55}
             for i, part in enumerate(chunk_text(content))]
    store.upsert_memory_batch(items)
    return f"[Success] Indexed {os.path.basename(raw)} into {len(items)} searchable passages."


def page_image(base_dir, name, page=1, dpi=140):
    """Render one page/slide to an image file for on-screen display."""
    path, error = resolve(base_dir, name)
    if error:
        return None, error
    ext = os.path.splitext(path)[1].lower()
    if ext != ".pdf":
        return None, "[Error] Page rendering currently supports PDF only."
    try:
        import pymupdf
        doc = pymupdf.open(path)
        index = max(0, min(doc.page_count - 1, int(page) - 1))
        pix = doc.load_page(index).get_pixmap(dpi=int(dpi))
        out_dir = os.path.join(base_dir, "captures")
        os.makedirs(out_dir, exist_ok=True)
        out = os.path.join(out_dir, "document_page.png")
        pix.save(out)
        doc.close()
        return out, None
    except Exception as exc:
        return None, f"[Error] Could not render that page: {exc}"
