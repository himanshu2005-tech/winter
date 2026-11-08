# -*- coding: utf-8 -*-
"""
life_tools.py -- the small things that make a day easier.

Each function returns a plain string receipt that states what was observed, not
what was requested: a path that exists, a byte count read back from disk, a
count of files actually extracted. None of them overwrites anything, and the two
that touch the network or unpack archives carry hard limits (size, count,
destination) because those are the two places a careless tool does damage.
"""

import ast
import datetime
import json
import math
import operator
import os
import re
import shutil
import subprocess
import time
import zipfile
from pathlib import Path

from office_docs import resolve_output

DOWNLOAD_LIMIT_BYTES = 300 * 1024 * 1024
ZIP_MAX_FILES = 20_000
ZIP_MAX_UNCOMPRESSED = 3 * 1024 ** 3


def _home():
    return Path(os.path.expanduser("~"))


def _clean_path(path):
    return Path(os.path.expandvars(os.path.expanduser(str(path or "").strip().strip('"'))))


def _unique(target):
    target, counter = Path(target), 2
    candidate = target
    while candidate.exists():
        candidate = target.with_name(f"{target.stem} ({counter}){target.suffix}")
        counter += 1
    return candidate


# --------------------------------------------------------------------------- #
# Open
# --------------------------------------------------------------------------- #
def open_path(path, reveal=False):
    """Open a file or folder in its default app, or show it in Explorer."""
    target = _clean_path(path)
    if not target.exists():
        raise ValueError(f"nothing exists at {target}")
    if reveal:
        if os.name == "nt":
            subprocess.Popen(["explorer", "/select,", str(target)])
        else:
            subprocess.Popen(["xdg-open", str(target.parent)])
        return f"[Success] Showing {target} in its folder."
    if os.name == "nt":
        os.startfile(str(target))  # noqa: S606 - the user's own file, opened by them
    else:
        subprocess.Popen(["xdg-open", str(target)])
    kind = "folder" if target.is_dir() else "file"
    return f"[Success] Opened the {kind} {target} in its default app."


# --------------------------------------------------------------------------- #
# Download
# --------------------------------------------------------------------------- #
def _filename_from(response, url):
    disposition = response.headers.get("Content-Disposition", "")
    match = re.search(r"filename\*?=(?:UTF-8'')?\"?([^\";]+)\"?", disposition, re.I)
    if match:
        from urllib.parse import unquote
        name = unquote(match.group(1))
    else:
        from urllib.parse import unquote, urlparse
        name = unquote(os.path.basename(urlparse(url).path)) or "download"
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .") or "download"
    return name[:150]


def download_file(url, destination="", name=""):
    """Download an http(s) file into Downloads (or a folder), never overwriting."""
    import requests
    import browser
    safe_url = browser._validate_url(url)     # http/https only, no private networks
    folder = _clean_path(destination) if destination else _home() / "Downloads"
    if folder.suffix and not folder.is_dir():
        folder = folder.parent
    folder.mkdir(parents=True, exist_ok=True)
    with requests.get(safe_url, stream=True, timeout=60,
                      headers={"User-Agent": "Mozilla/5.0 VANTA"}) as response:
        response.raise_for_status()
        declared = int(response.headers.get("Content-Length") or 0)
        if declared > DOWNLOAD_LIMIT_BYTES:
            raise ValueError(f"the file is {declared:,} bytes, over the "
                             f"{DOWNLOAD_LIMIT_BYTES:,}-byte limit")
        filename = re.sub(r'[<>:"/\\|?*]', "_", name).strip() if name else \
            _filename_from(response, safe_url)
        target = _unique(folder / filename)
        written = 0
        partial = target.with_name(target.name + ".part")
        try:
            with open(partial, "wb") as handle:
                for chunk in response.iter_content(chunk_size=1024 * 256):
                    written += len(chunk)
                    if written > DOWNLOAD_LIMIT_BYTES:
                        raise ValueError("the download passed the size limit and was stopped")
                    handle.write(chunk)
            os.replace(partial, target)
        finally:
            if partial.exists():
                partial.unlink()
        kind = response.headers.get("Content-Type", "unknown type").split(";")[0]
    size = target.stat().st_size
    if size == 0:
        target.unlink()
        raise ValueError("the server returned an empty file")
    return f"[Success] Downloaded {target.name} ({size:,} bytes, {kind}) to {target}."


# --------------------------------------------------------------------------- #
# Zip
# --------------------------------------------------------------------------- #
def zip_path(path, destination=""):
    source = _clean_path(path)
    if not source.exists():
        raise ValueError(f"nothing exists at {source}")
    if destination:
        target = _clean_path(destination)
        if target.is_dir():
            target = target / (source.stem + ".zip")
        if target.suffix.lower() != ".zip":
            target = target.with_suffix(".zip")
    else:
        target = source.with_suffix(".zip") if source.is_file() else \
            source.parent / (source.name + ".zip")
    target.parent.mkdir(parents=True, exist_ok=True)
    target = _unique(target)
    count = 0
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
        if source.is_file():
            archive.write(source, source.name)
            count = 1
        else:
            for item in sorted(source.rglob("*")):
                if item.is_file() and item.resolve() != target.resolve():
                    archive.write(item, str(item.relative_to(source.parent)))
                    count += 1
                    if count > ZIP_MAX_FILES:
                        raise ValueError(f"more than {ZIP_MAX_FILES:,} files; zip a smaller folder")
    return (f"[Success] Zipped {count} file(s) into {target} "
            f"({target.stat().st_size:,} bytes).")


def unzip_file(path, destination=""):
    source = _clean_path(path)
    if not source.is_file() or not zipfile.is_zipfile(source):
        raise ValueError(f"{source} is not a zip file")
    folder = _clean_path(destination) if destination else source.parent / source.stem
    folder = _unique(folder) if folder.exists() and any(folder.iterdir()) and not destination \
        else folder
    folder.mkdir(parents=True, exist_ok=True)
    root = folder.resolve()
    extracted = 0
    with zipfile.ZipFile(source) as archive:
        members = archive.infolist()
        if len(members) > ZIP_MAX_FILES:
            raise ValueError(f"the archive holds {len(members):,} entries; refusing")
        if sum(m.file_size for m in members) > ZIP_MAX_UNCOMPRESSED:
            raise ValueError("the archive would expand past the size limit; refusing")
        for member in members:
            target = (root / member.filename).resolve()
            # A path that climbs out of the folder ("..\\..\\x") is how an
            # archive writes over files it has no business touching.
            if root != target and root not in target.parents:
                raise ValueError(f"unsafe path in the archive: {member.filename}")
            if member.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                target = _unique(target)
            with archive.open(member) as src, open(target, "wb") as dst:
                shutil.copyfileobj(src, dst)
            extracted += 1
    return f"[Success] Extracted {extracted} file(s) from {source.name} into {folder}."


# --------------------------------------------------------------------------- #
# Calculator
# --------------------------------------------------------------------------- #
_OPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
        ast.Div: operator.truediv, ast.FloorDiv: operator.floordiv,
        ast.Mod: operator.mod, ast.Pow: operator.pow,
        ast.USub: operator.neg, ast.UAdd: operator.pos}
_FUNCS = {"sqrt": math.sqrt, "sin": math.sin, "cos": math.cos, "tan": math.tan,
          "asin": math.asin, "acos": math.acos, "atan": math.atan,
          "log": math.log10, "log10": math.log10, "ln": math.log, "log2": math.log2,
          "exp": math.exp, "abs": abs, "round": round, "floor": math.floor,
          "ceil": math.ceil, "min": min, "max": max, "factorial": math.factorial,
          "radians": math.radians, "degrees": math.degrees, "pow": pow,
          "sum": lambda *a: sum(a)}
_CONSTS = {"pi": math.pi, "e": math.e, "tau": math.tau}


def _prepare_expression(text):
    value = str(text or "").strip().lower()
    value = value.replace("×", "*").replace("÷", "/").replace("^", "**")
    value = value.replace(",", "")
    # "15% of 2400" and "2400 * 15%"
    value = re.sub(r"(\d+(?:\.\d+)?)\s*%\s*of\s*", r"(\1/100)*", value)
    value = re.sub(r"(\d+(?:\.\d+)?)\s*%", r"(\1/100)", value)
    value = re.sub(r"\bsquare root of\s*(\d+(?:\.\d+)?)", r"sqrt(\1)", value)
    return value


def calculate(expression):
    source = _prepare_expression(expression)
    if not source or len(source) > 300:
        raise ValueError("give a short arithmetic expression, e.g. 15% of 2400 or sqrt(144)+3^2")

    def evaluate(node):
        if isinstance(node, ast.Expression):
            return evaluate(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) \
                and not isinstance(node.value, bool):
            return node.value
        if isinstance(node, ast.BinOp) and type(node.op) in _OPS:
            left, right = evaluate(node.left), evaluate(node.right)
            if isinstance(node.op, ast.Pow) and abs(right) > 1000:
                raise ValueError("that exponent is too large")
            return _OPS[type(node.op)](left, right)
        if isinstance(node, ast.UnaryOp) and type(node.op) in _OPS:
            return _OPS[type(node.op)](evaluate(node.operand))
        if isinstance(node, ast.Name) and node.id in _CONSTS:
            return _CONSTS[node.id]
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id in _FUNCS and not node.keywords):
            return _FUNCS[node.func.id](*[evaluate(a) for a in node.args])
        raise ValueError("unsupported expression")

    try:
        result = evaluate(ast.parse(source, mode="eval"))
    except ZeroDivisionError:
        raise ValueError("division by zero") from None
    except (SyntaxError, TypeError, OverflowError) as exc:
        raise ValueError(f"could not evaluate that ({exc.__class__.__name__})") from None
    if isinstance(result, float):
        if math.isnan(result) or math.isinf(result):
            raise ValueError("the result is not a finite number")
        shown = f"{result:,.10g}" if abs(result) < 1e15 else f"{result:.6e}"
    else:
        shown = f"{result:,}"
    return f"{str(expression).strip()} = {shown}"


# --------------------------------------------------------------------------- #
# World time
# --------------------------------------------------------------------------- #
_ALIASES = {
    "india": "Asia/Kolkata", "chennai": "Asia/Kolkata", "hyderabad": "Asia/Kolkata",
    "delhi": "Asia/Kolkata", "mumbai": "Asia/Kolkata", "bangalore": "Asia/Kolkata",
    "bengaluru": "Asia/Kolkata", "ist": "Asia/Kolkata",
    "uk": "Europe/London", "england": "Europe/London", "usa": "America/New_York",
    "us": "America/New_York", "california": "America/Los_Angeles",
    "san francisco": "America/Los_Angeles", "seattle": "America/Los_Angeles",
    "texas": "America/Chicago", "uae": "Asia/Dubai", "dubai": "Asia/Dubai",
    "singapore": "Asia/Singapore", "japan": "Asia/Tokyo", "china": "Asia/Shanghai",
    "beijing": "Asia/Shanghai", "germany": "Europe/Berlin", "france": "Europe/Paris",
    "australia": "Australia/Sydney", "pst": "America/Los_Angeles",
    "est": "America/New_York", "cst": "America/Chicago", "utc": "UTC", "gmt": "UTC",
    "new york": "America/New_York", "los angeles": "America/Los_Angeles",
}


def _find_zone(place):
    import zoneinfo
    key = re.sub(r"\s+", " ", str(place or "").strip().lower())
    if not key:
        raise ValueError("name a city, country or time zone")
    if key in _ALIASES:
        return zoneinfo.ZoneInfo(_ALIASES[key]), _ALIASES[key]
    zones = zoneinfo.available_timezones()
    exact = [z for z in zones if z.lower() == key]
    if exact:
        return zoneinfo.ZoneInfo(exact[0]), exact[0]
    wanted = key.replace(" ", "_")
    matches = sorted(z for z in zones if z.lower().split("/")[-1] == wanted)
    if not matches:
        matches = sorted(z for z in zones if wanted in z.lower())
    if matches:
        return zoneinfo.ZoneInfo(matches[0]), matches[0]
    raise ValueError(f"I don't know a time zone for {place!r}; try a city like Tokyo "
                     "or a zone like Europe/London")


def time_in(place, at=""):
    """Current time in a place, or the conversion of `at` (HH:MM, local) to it."""
    import zoneinfo
    zone, name = _find_zone(place)
    local_now = datetime.datetime.now().astimezone()
    if at:
        match = re.match(r"^\s*(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\s*$", str(at), re.I)
        if not match:
            raise ValueError("give the time as HH:MM or 5pm")
        hour, minute = int(match.group(1)), int(match.group(2) or 0)
        meridian = (match.group(3) or "").lower()
        if meridian == "pm" and hour < 12:
            hour += 12
        if meridian == "am" and hour == 12:
            hour = 0
        if hour > 23 or minute > 59:
            raise ValueError("that is not a valid time")
        base = local_now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        there = base.astimezone(zone)
        return (f"{base:%H:%M} here is {there:%H:%M} in {name} "
                f"({there:%a %d %b}, {there.tzname()}).")
    there = local_now.astimezone(zone)
    return (f"It is {there:%H:%M} on {there:%A %d %B} in {name} "
            f"({there.tzname()}); {local_now:%H:%M} here.")


# --------------------------------------------------------------------------- #
# Notes
# --------------------------------------------------------------------------- #
def _notes_file(base_dir):
    folder = Path(base_dir) / "vault" / "notes"
    folder.mkdir(parents=True, exist_ok=True)
    return folder / "notes.jsonl"


def _read_notes(base_dir):
    path = _notes_file(base_dir)
    notes = []
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict) and row.get("id"):
                notes.append(row)
    return notes


def _write_notes(base_dir, notes):
    path = _notes_file(base_dir)
    temp = path.with_suffix(".tmp")
    temp.write_text("".join(json.dumps(n, ensure_ascii=False) + "\n" for n in notes),
                    encoding="utf-8")
    os.replace(temp, path)


def add_note(base_dir, text, tags=""):
    text = str(text or "").strip()
    if not text:
        raise ValueError("a note needs some text")
    notes = _read_notes(base_dir)
    note = {"id": f"n{int(time.time() * 1000) % 10**9:x}{len(notes) + 1}",
            "at": datetime.datetime.now().isoformat(timespec="seconds"),
            "text": text[:4000],
            "tags": [t.strip().lower() for t in re.split(r"[,#\s]+", str(tags or "")) if t.strip()]}
    notes.append(note)
    _write_notes(base_dir, notes)
    stored = [n for n in _read_notes(base_dir) if n["id"] == note["id"]]
    if not stored:
        raise RuntimeError("the note was not written")
    return f"[Success] Noted [{note['id']}]: {text[:120]}" + \
        (f" (tags: {', '.join(note['tags'])})" if note["tags"] else "")


def list_notes(base_dir, query="", limit=10):
    notes = _read_notes(base_dir)
    terms = [t for t in re.findall(r"\w+", str(query or "").lower()) if len(t) > 1]
    if terms:
        def score(note):
            hay = (note["text"] + " " + " ".join(note.get("tags", []))).lower()
            return sum(1 for t in terms if t in hay)
        scored = [(score(n), n) for n in notes]
        notes = [n for s, n in sorted(scored, key=lambda p: (p[0], p[1]["at"]), reverse=True)
                 if s > 0]
    else:
        notes = sorted(notes, key=lambda n: n["at"], reverse=True)
    limit = max(1, min(int(limit or 10), 50))
    shown = notes[:limit]
    if not shown:
        return "No notes found." if terms else "There are no notes yet."
    lines = [f"Notes{f' matching {query!r}' if terms else ''} ({len(shown)} of {len(notes)}):"]
    for n in shown:
        tags = f" #{' #'.join(n['tags'])}" if n.get("tags") else ""
        lines.append(f"- [{n['id']}] {n['at'][:16].replace('T', ' ')}: {n['text']}{tags}")
    return "\n".join(lines)


def delete_note(base_dir, note_id):
    notes = _read_notes(base_dir)
    kept = [n for n in notes if n["id"] != str(note_id).strip()]
    if len(kept) == len(notes):
        raise ValueError(f"no note with id {note_id!r}")
    _write_notes(base_dir, kept)
    return f"[Success] Deleted note {note_id}."


# --------------------------------------------------------------------------- #
# PDF tools
# --------------------------------------------------------------------------- #
def _fitz():
    try:
        import pymupdf as fitz
    except ImportError:
        import fitz
    return fitz


def pdf_merge(paths, name=""):
    fitz = _fitz()
    files = [_clean_path(p) for p in re.split(r"[;|\n]|,(?=\s*[A-Za-z]:|\s*/|\s*~)", str(paths or ""))
             if p.strip()]
    if len(files) < 2:
        raise ValueError("give at least two PDF paths separated by ;")
    for file in files:
        if not file.is_file() or file.suffix.lower() != ".pdf":
            raise ValueError(f"not a PDF file: {file}")
    out = resolve_output(name or "", "merged", "pdf") if name else \
        _unique(files[0].with_name(files[0].stem + "-merged.pdf"))
    merged = fitz.open()
    try:
        for file in files:
            with fitz.open(str(file)) as part:
                merged.insert_pdf(part)
        merged.save(str(out))
        pages = merged.page_count
    finally:
        merged.close()
    return f"[Success] Merged {len(files)} PDFs into {out} ({pages} pages)."


def parse_pages(spec, total):
    pages = []
    for part in re.split(r"[,\s]+", str(spec or "").strip()):
        if not part:
            continue
        match = re.fullmatch(r"(\d+)(?:-(\d+))?", part)
        if not match:
            raise ValueError(f"bad page range {part!r}; use 1-3,5")
        first, last = int(match.group(1)), int(match.group(2) or match.group(1))
        if first < 1 or last < first or last > total:
            raise ValueError(f"pages {part} are outside 1-{total}")
        pages.extend(range(first, last + 1))
    if not pages:
        raise ValueError("say which pages, e.g. pages=1-3,5")
    return pages


def pdf_extract(path, pages, name=""):
    fitz = _fitz()
    source = _clean_path(path)
    if not source.is_file() or source.suffix.lower() != ".pdf":
        raise ValueError(f"not a PDF file: {source}")
    with fitz.open(str(source)) as document:
        chosen = parse_pages(pages, document.page_count)
        out = resolve_output(name or "", source.stem + "-pages", "pdf") if name else \
            _unique(source.with_name(f"{source.stem}-pages.pdf"))
        extract = fitz.open()
        try:
            for number in chosen:
                extract.insert_pdf(document, from_page=number - 1, to_page=number - 1)
            extract.save(str(out))
        finally:
            extract.close()
    return f"[Success] Saved {len(chosen)} page(s) of {source.name} to {out}."
