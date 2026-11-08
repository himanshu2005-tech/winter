# -*- coding: utf-8 -*-
"""
report_html.py -- turning a model's report into something worth looking at.

The old result window put Markdown into a text widget: every line the same size,
tables as aligned text, a Mermaid diagram as its own source. This renders the
same input as a designed page -- typography, tables with real column alignment
and status badges, KPI tiles, charts and flow diagrams drawn as SVG, callouts,
checklists, progress bars -- with motion that explains rather than decorates
(bars grow from zero, lines draw themselves, flow edges carry a moving dash,
numbers count up, sections rise in sequence).

SAFETY
------
Nothing the model writes is ever passed through as HTML. Text is escaped first
and only a fixed set of inline marks (bold, italic, code, highlight, strike,
http(s) links) is rebuilt from it. SVG is generated here from parsed numbers and
escaped labels. A report is therefore data, not markup.

The functions are pure (text in, HTML string out) so the parsing -- where the
mistakes live -- is tested without a window.
"""

import base64
import html
import io
import math
import re
import json

# --------------------------------------------------------------------------- #
# Inline marks
# --------------------------------------------------------------------------- #
_LINK = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")
_URL = re.compile(r"(?<![\"'>=])\bhttps?://[^\s<>\"')\]]+")


def inline(text):
    """Escaped HTML for **bold**, *italic*, `code`, ==mark==, ~~strike~~, links."""
    source = str(text or "")
    placeholders = []

    def stash(markup):
        placeholders.append(markup)
        return f"\u0000{len(placeholders) - 1}\u0000"

    source = re.sub(r"`([^`]+)`",
                    lambda m: stash(f"<code>{html.escape(m.group(1))}</code>"), source)
    source = re.sub(r'\[\[([^\[\]\n]{1,100})\]\]',
                    lambda m: stash('<button type="button" class="explain-term" data-explain="'
                                    + html.escape(m.group(1), quote=True) + '">'
                                    + html.escape(m.group(1)) + '</button>'), source)
    source = _LINK.sub(
        lambda m: stash(f'<a href="{html.escape(m.group(2), quote=True)}" target="_blank" '
                        f'rel="noopener noreferrer">{html.escape(m.group(1))}</a>'), source)
    source = _URL.sub(
        lambda m: stash(f'<a href="{html.escape(m.group(0).rstrip(".,;:"), quote=True)}" '
                        f'target="_blank" rel="noopener noreferrer">'
                        f'{html.escape(_short_url(m.group(0).rstrip(".,;:")))}</a>'
                        + html.escape(m.group(0)[len(m.group(0).rstrip(".,;:")):])), source)
    escaped = html.escape(source, quote=False)
    escaped = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", escaped)
    escaped = re.sub(r"__(.+?)__", r"<strong>\1</strong>", escaped)
    escaped = re.sub(r"(?<![\w*])\*([^*\s][^*]*?)\*(?![\w*])", r"<em>\1</em>", escaped)
    escaped = re.sub(r"(?<![\w_])_([^_\s][^_]*?)_(?![\w_])", r"<em>\1</em>", escaped)
    escaped = re.sub(r"==(.+?)==", r"<mark>\1</mark>", escaped)
    escaped = re.sub(r"~~(.+?)~~", r"<del>\1</del>", escaped)
    return re.sub(r"\u0000(\d+)\u0000", lambda m: placeholders[int(m.group(1))], escaped)


def _short_url(url):
    stripped = re.sub(r"^https?://(www\.)?", "", url)
    return stripped if len(stripped) <= 48 else stripped[:45] + "..."


def plain(text):
    """Text with the inline marks removed (for tooltips, ids, measuring)."""
    cleaned = re.sub(r"`([^`]*)`", r"\1", str(text or ""))
    cleaned = _LINK.sub(r"\1", cleaned)
    cleaned = re.sub(r'\[\[([^\[\]\n]+)\]\]', r'\1', cleaned)
    cleaned = re.sub(r"(\*\*|__|==|~~)(.+?)\1", r"\2", cleaned)
    cleaned = re.sub(r"(?<![\w*])\*([^*]+?)\*(?![\w*])", r"\1", cleaned)
    return cleaned.strip()


def slug(text, taken=()):
    base = re.sub(r"[^a-z0-9]+", "-", plain(text).lower()).strip("-")[:40] or "section"
    candidate, count = base, 2
    while candidate in taken:
        candidate = f"{base}-{count}"
        count += 1
    return candidate


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #
_BULLET = re.compile(r"^(\s*)([-*+•·]|\d{1,2}[.)])\s+(.*)$")
_TODO = re.compile(r"^\[( |x|X)\]\s+(.*)$")
_LABELLED = re.compile(r"^\s*([A-Za-z][A-Za-z0-9 /'()&-]{1,28}):\s+(\S.*)$")
_MD_HEADING = re.compile(r"^\s*(#{1,6})\s+(.*?)\s*#*\s*$")
_BOLD_ONLY = re.compile(r"^\s*\*\*(.+?)\*\*:?\s*$")
_RULE = re.compile(r"^\s*([-=_*])\1{2,}\s*$")
_FENCE = re.compile(r"^\s*```\s*([A-Za-z0-9_+-]*)\s*(.*)$")
_TABLE_SEP = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")
_CALLOUT = re.compile(r"^>\s*\[!(\w+)\]\s*(.*)$")
_PROGRESS = re.compile(r"^\[progress\s+(\d{1,3})\s*%?\]\s*(.*)$", re.I)
_QUANTITY = re.compile(
    r"^\s*(?:[₹$€£]|Rs\.?\s?)?[-+]?\d[\d,.]*\s*"
    r"(?:%|C|°C|km/h|km|m|kg|hrs?|hours?|mins?|minutes?|days?|weeks?|"
    r"steps|L|k|lpa|x)?\s*$", re.I)
CALLOUT_KINDS = {"note": "Note", "info": "Note", "tip": "Tip", "success": "Done",
                 "done": "Done", "warning": "Heads up", "warn": "Heads up",
                 "danger": "Important", "error": "Important", "important": "Important",
                 "question": "Question"}

MAX_HEADING_WORDS = 7
MAX_HEADING_CHARS = 48


def _split_cells(line):
    inner = line.strip()
    if inner.startswith("|"):
        inner = inner[1:]
    if inner.endswith("|"):
        inner = inner[:-1]
    return [cell.strip() for cell in inner.split("|")]


def _align(cell):
    cell = cell.strip()
    if cell.startswith(":") and cell.endswith(":"):
        return "center"
    if cell.endswith(":"):
        return "right"
    return "left"


def _heading_by_shape(line, following):
    text = plain(line)
    if not text or len(text) > MAX_HEADING_CHARS or len(text.split()) > MAX_HEADING_WORDS:
        return False
    if text.endswith((".", "!", "?", ",", ";")) or _BULLET.match(line) or "http" in text:
        return False
    if not str(following or "").strip():
        return False
    return text.isupper() or text.endswith(":")


def parse(text):
    """Typed blocks. See the module docstring for what each kind is."""
    lines = str(text or "").replace("\r\n", "\n").replace("\t", "    ").split("\n")
    blocks, index = [], 0
    while index < len(lines):
        line = lines[index].rstrip()
        stripped = line.strip()
        following = next((c for c in lines[index + 1:] if c.strip()), "")
        if not stripped:
            index += 1
            continue
        image = re.fullmatch(r'!\[([^\]]*)\]\((.+)\)', stripped)
        if image:
            blocks.append({"kind": "fence", "language": "image", "arg": "",
                           "text": json.dumps({"src": image.group(2), "alt": image.group(1)})})
            index += 1
            continue
        # Display mathematics is a first-class report block.  Keep it out of
        # paragraph joining so a derivation remains a derivation instead of
        # becoming escaped source text.  Both compact and multi-line $$ forms
        # are accepted because models naturally produce both.
        if stripped.startswith("$$"):
            if stripped.endswith("$$") and len(stripped) > 4:
                formula = stripped[2:-2].strip()
                index += 1
            else:
                body = [stripped[2:]]
                index += 1
                while index < len(lines):
                    candidate = lines[index]
                    if candidate.rstrip().endswith("$$"):
                        body.append(candidate.rstrip()[:-2])
                        index += 1
                        break
                    body.append(candidate)
                    index += 1
                formula = "\n".join(body).strip()
            blocks.append({"kind": "math", "text": formula})
            continue
        fence = _FENCE.match(line)
        if fence:
            language, rest = fence.group(1).lower(), fence.group(2).strip()
            body = []
            index += 1
            while index < len(lines) and not lines[index].strip().startswith("```"):
                body.append(lines[index])
                index += 1
            index += 1
            content = "\n".join(body).strip("\n")
            if language in {"latex", "math", "tex"}:
                blocks.append({"kind": "math", "text": content})
            else:
                blocks.append({"kind": "fence", "language": language, "arg": rest,
                               "text": content})
            continue
        if "|" in line and index + 1 < len(lines) and _TABLE_SEP.match(lines[index + 1]):
            headers = _split_cells(line)
            aligns = [_align(c) for c in _split_cells(lines[index + 1])]
            rows = []
            index += 2
            while index < len(lines) and "|" in lines[index] and lines[index].strip():
                row = _split_cells(lines[index])
                rows.append((row + [""] * len(headers))[:len(headers)])
                index += 1
            aligns = (aligns + ["left"] * len(headers))[:len(headers)]
            blocks.append({"kind": "table", "headers": headers, "rows": rows, "aligns": aligns})
            continue
        if _RULE.match(line):
            blocks.append({"kind": "rule"})
            index += 1
            continue
        heading = _MD_HEADING.match(line)
        if heading:
            blocks.append({"kind": "heading", "level": min(len(heading.group(1)), 4),
                           "text": heading.group(2)})
            index += 1
            continue
        bold = _BOLD_ONLY.match(line)
        if bold and following:
            blocks.append({"kind": "heading", "level": 2, "text": bold.group(1)})
            index += 1
            continue
        callout = _CALLOUT.match(stripped)
        if callout:
            body = [callout.group(2)]
            index += 1
            while index < len(lines) and lines[index].strip().startswith(">"):
                body.append(lines[index].strip().lstrip("> ").strip())
                index += 1
            kind = callout.group(1).lower()
            blocks.append({"kind": "callout", "tone": kind if kind in CALLOUT_KINDS else "note",
                           "text": " ".join(b for b in body if b)})
            continue
        if stripped.startswith(">"):
            body = []
            while index < len(lines) and lines[index].strip().startswith(">"):
                body.append(lines[index].strip().lstrip("> ").strip())
                index += 1
            blocks.append({"kind": "quote", "text": " ".join(body)})
            continue
        progress = _PROGRESS.match(stripped)
        if progress:
            blocks.append({"kind": "progress", "value": min(100, int(progress.group(1))),
                           "text": progress.group(2)})
            index += 1
            continue
        bullet = _BULLET.match(line)
        if bullet:
            level = min(3, len(bullet.group(1)) // 2)
            body = bullet.group(3)
            todo = _TODO.match(body)
            ordered = bullet.group(2)[0].isdigit()
            item = {"level": level, "ordered": ordered, "text": body}
            if todo:
                item.update(text=todo.group(2), todo=True, done=todo.group(1).lower() == "x")
            if blocks and blocks[-1]["kind"] == "list":
                blocks[-1]["items"].append(item)
            else:
                blocks.append({"kind": "list", "items": [item]})
            index += 1
            continue
        if _heading_by_shape(line, following):
            blocks.append({"kind": "heading", "level": 2, "text": plain(line).rstrip(":")})
            index += 1
            continue
        labelled = _LABELLED.match(stripped)
        if labelled and not stripped.lower().startswith(("http", "note", "tip")):
            row = (labelled.group(1).strip(), labelled.group(2).strip())
            if blocks and blocks[-1]["kind"] == "facts":
                blocks[-1]["items"].append(row)
            else:
                blocks.append({"kind": "facts", "items": [row]})
            index += 1
            continue
        if blocks and blocks[-1]["kind"] == "para" and lines[index - 1].strip():
            blocks[-1]["text"] += " " + stripped
        else:
            blocks.append({"kind": "para", "text": stripped})
        index += 1
    return blocks


# --------------------------------------------------------------------------- #
# Tables, facts, lists
# --------------------------------------------------------------------------- #
_STATUS = {
    "done": "ok", "complete": "ok", "completed": "ok", "passed": "ok", "success": "ok",
    "yes": "ok", "ready": "ok", "applied": "info", "shortlisted": "ok", "selected": "ok",
    "offer": "ok", "active": "ok", "open": "info", "in progress": "warn",
    "pending": "warn", "ongoing": "warn", "waiting": "warn", "review": "warn",
    "overdue": "bad", "failed": "bad", "rejected": "bad", "blocked": "bad", "no": "bad",
    "cancelled": "bad", "high": "bad", "medium": "warn", "low": "ok", "todo": "info",
}
_NUMBERISH = re.compile(r"^[₹$€£+\-]?\s*\d[\d,]*(\.\d+)?\s*(%|[kKmMbB]|x)?$")


def _cell(value):
    key = plain(value).strip().lower()
    if key in _STATUS:
        return f'<span class="badge {_STATUS[key]}">{inline(value)}</span>'
    return inline(value)


def render_table(block):
    heads = block["headers"]
    aligns = list(block.get("aligns") or ["left"] * len(heads))
    # A column of numbers reads right-aligned, whatever the separator said.
    for c in range(len(heads)):
        column = [plain(row[c]) for row in block["rows"] if row[c].strip()]
        if column and all(_NUMBERISH.match(v) for v in column) and aligns[c] == "left":
            aligns[c] = "right"
    head = "".join(f'<th class="{aligns[i]}" data-col="{i}">{inline(h)}'
                   f'<span class="sort"></span></th>' for i, h in enumerate(heads))
    body = "".join(
        "<tr>" + "".join(f'<td class="{aligns[i]}">{_cell(v)}</td>'
                         for i, v in enumerate(row)) + "</tr>"
        for row in block["rows"])
    return (f'<div class="table-wrap reveal"><table class="data"><thead><tr>{head}</tr></thead>'
            f"<tbody>{body}</tbody></table></div>")


def render_facts(block):
    items = block["items"]
    numeric = [i for i in items if _QUANTITY.match(i[1]) and len(i[1]) <= 14]
    if len(items) >= 2 and len(numeric) >= max(2, len(items) // 2 + 1):
        tiles = "".join(
            f'<div class="tile"><div class="tile-label">{inline(k)}</div>'
            f'<div class="tile-value" data-count="{html.escape(v, quote=True)}">'
            f"{html.escape(v)}</div></div>" for k, v in items)
        return f'<div class="tiles reveal">{tiles}</div>'
    rows = "".join(f"<dt>{inline(k)}</dt><dd>{inline(v)}</dd>" for k, v in items)
    return f'<dl class="facts reveal">{rows}</dl>'


def render_list(block):
    items = block["items"]
    out, stack = [], []

    def close_to(depth):
        while len(stack) > depth:
            out.append(f"</{stack.pop()}>")

    for item in items:
        depth = item["level"] + 1
        tag = "ol" if item["ordered"] else "ul"
        while len(stack) < depth:
            out.append(f'<{tag} class="{"todo" if item.get("todo") else ""}">')
            stack.append(tag)
        close_to(depth)
        if item.get("todo"):
            box = f'<span class="check {"on" if item["done"] else ""}"></span>'
            out.append(f'<li class="todo-item {"done" if item["done"] else ""}">'
                       f"{box}{inline(item['text'])}</li>")
        else:
            out.append(f"<li>{inline(item['text'])}</li>")
    close_to(0)
    return f'<div class="list reveal">{"".join(out)}</div>'


# --------------------------------------------------------------------------- #
# Charts
# --------------------------------------------------------------------------- #
#: Series colours live in the stylesheet (classes f0..f5 fill, k0..k5 stroke, b0..b5
#: background) so they follow the theme and the whole palette is one decision.
PALETTE = ("s0", "s1", "s2", "s3", "s4", "s5")


def parse_chart(arg, body):
    """chart <type> [title] with 'categories:' and 'Series: n, n, n' lines."""
    parts = str(arg).split(None, 1)
    kind = parts[0].lower() if parts else "column"
    title = parts[1].strip().strip('"') if len(parts) > 1 else ""
    if kind not in {"column", "bar", "line", "area", "pie", "donut", "doughnut", "hbar"}:
        title, kind = str(arg).strip().strip('"'), "column"
    kind = {"doughnut": "donut", "bar": "hbar"}.get(kind, kind)
    categories, series = [], []
    for raw in body.splitlines():
        if ":" not in raw:
            continue
        name, values = raw.split(":", 1)
        parts = [p.strip() for p in re.split(r";|,\s+|,(?!\d{3}(?!\d))", values) if p.strip()]
        if name.strip().lower() in ("categories", "labels", "x"):
            categories = parts
            continue
        nums = []
        for p in parts:
            try:
                nums.append(float(re.sub(r"[^0-9eE+\-.]", "", p.replace(",", "")) or "nan"))
            except ValueError:
                nums.append(float("nan"))
        series.append((name.strip(), [0.0 if math.isnan(n) else n for n in nums]))
    if not categories and series:
        categories = [str(i + 1) for i in range(len(series[0][1]))]
    return kind, title, categories, series


def _nice_max(value):
    if value <= 0:
        return 1.0
    exponent = 10 ** math.floor(math.log10(value))
    for step in (1, 2, 2.5, 5, 10):
        if value <= step * exponent:
            return step * exponent
    return 10 * exponent


def _fmt(number):
    if abs(number) >= 1000 and float(number).is_integer():
        return f"{int(number):,}"
    return f"{number:g}"


def render_chart(arg, body):
    kind, title, categories, series = parse_chart(arg, body)
    if not series or not categories:
        return None
    header = f'<div class="chart-title">{inline(title)}</div>' if title else ""
    if kind in ("pie", "donut"):
        svg = _svg_pie(categories, series[0][1], donut=(kind == "donut"))
    elif kind == "hbar":
        svg = _svg_hbar(categories, series)
    elif kind in ("line", "area"):
        svg = _svg_line(categories, series, area=(kind == "area"))
    else:
        svg = _svg_columns(categories, series)
    legend = ""
    if len(series) > 1 and kind not in ("pie", "donut"):
        legend = '<div class="legend">' + "".join(
            f'<span><i class="b{i % 6}"></i>{html.escape(n)}</span>'
            for i, (n, _) in enumerate(series)) + "</div>"
    return f'<figure class="chart reveal">{header}{svg}{legend}</figure>'


def _svg_columns(categories, series):
    width, height, left, bottom, top = 720, 300, 46, 36, 16
    peak = _nice_max(max((max(v) if v else 0) for _, v in series))
    inner_w, inner_h = width - left - 12, height - bottom - top
    group = inner_w / max(1, len(categories))
    bar_w = min(54, group * 0.7 / len(series))
    out = [f'<svg viewBox="0 0 {width} {height}" class="svg-chart" role="img">']
    for step in range(5):
        y = top + inner_h - inner_h * step / 4
        out.append(f'<line x1="{left}" x2="{width - 8}" y1="{y:.1f}" y2="{y:.1f}" class="grid"/>'
                   f'<text x="{left - 8}" y="{y + 4:.1f}" class="axis" text-anchor="end">'
                   f'{_fmt(peak * step / 4)}</text>')
    for ci, cat in enumerate(categories):
        cx = left + group * ci + group / 2
        for si, (_, values) in enumerate(series):
            value = values[ci] if ci < len(values) else 0
            h = inner_h * (value / peak if peak else 0)
            x = cx - (bar_w * len(series)) / 2 + si * bar_w
            out.append(
                f'<rect class="bar f{si % 6}" style="--d:{(ci * len(series) + si) * 45}ms" x="{x:.1f}" '
                f'y="{top + inner_h - h:.1f}" width="{bar_w - 3:.1f}" height="{h:.1f}" rx="5" '
                f'><title>{html.escape(cat)}: {_fmt(value)}'
                f'</title></rect>')
            if len(categories) * len(series) <= 14:
                out.append(f'<text x="{x + (bar_w - 3) / 2:.1f}" y="{top + inner_h - h - 6:.1f}" '
                           f'class="val" text-anchor="middle">{_fmt(value)}</text>')
        out.append(f'<text x="{cx:.1f}" y="{height - 12}" class="axis" text-anchor="middle">'
                   f'{html.escape(cat[:14])}</text>')
    out.append("</svg>")
    return "".join(out)


def _svg_hbar(categories, series):
    values = series[0][1]
    peak = _nice_max(max(values) if values else 1)
    row_h, left, width = 38, 150, 720
    height = row_h * len(categories) + 16
    out = [f'<svg viewBox="0 0 {width} {height}" class="svg-chart" role="img">']
    for i, (cat, value) in enumerate(zip(categories, values)):
        y = 8 + i * row_h
        w = (width - left - 70) * (value / peak if peak else 0)
        out.append(
            f'<text x="{left - 12}" y="{y + 22}" class="axis" text-anchor="end">{html.escape(cat[:22])}</text>'
            f'<rect x="{left}" y="{y + 6}" width="{width - left - 70}" height="18" rx="9" class="track"/>'
            f'<rect class="bar h f{i % 6}" style="--d:{i * 60}ms" x="{left}" y="{y + 6}" width="{w:.1f}" '
            f'height="18" rx="9"/>'
            f'<text x="{left + w + 10:.1f}" y="{y + 21}" class="val">{_fmt(value)}</text>')
    out.append("</svg>")
    return "".join(out)


def _svg_line(categories, series, area=False):
    width, height, left, bottom, top = 720, 300, 46, 36, 18
    peak = _nice_max(max((max(v) if v else 0) for _, v in series))
    inner_w, inner_h = width - left - 16, height - bottom - top
    n = max(1, len(categories) - 1)
    out = [f'<svg viewBox="0 0 {width} {height}" class="svg-chart" role="img">']
    for step in range(5):
        y = top + inner_h - inner_h * step / 4
        out.append(f'<line x1="{left}" x2="{width - 8}" y1="{y:.1f}" y2="{y:.1f}" class="grid"/>'
                   f'<text x="{left - 8}" y="{y + 4:.1f}" class="axis" text-anchor="end">'
                   f'{_fmt(peak * step / 4)}</text>')
    for si, (name, values) in enumerate(series):
        colour = si % 6
        pts = [(left + inner_w * i / n,
                top + inner_h - inner_h * ((values[i] if i < len(values) else 0) / peak))
               for i in range(len(categories))]
        path = "M" + " L".join(f"{x:.1f},{y:.1f}" for x, y in pts)
        if area:
            out.append(f'<path d="{path} L{pts[-1][0]:.1f},{top + inner_h} L{pts[0][0]:.1f},'
                       f'{top + inner_h} Z" class="area f{colour}"/>')
        out.append(f'<path d="{path}" fill="none" stroke-width="2.5" '
                   f'stroke-linecap="round" stroke-linejoin="round" class="line k{colour}" pathLength="1"/>')
        for i, (x, y) in enumerate(pts):
            out.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="3.5" class="dot f{colour}" '
                       f'style="--d:{600 + i * 60}ms"><title>{html.escape(name)}: '
                       f'{_fmt(values[i] if i < len(values) else 0)}</title></circle>')
    for i, cat in enumerate(categories):
        out.append(f'<text x="{left + inner_w * i / n:.1f}" y="{height - 12}" class="axis" '
                   f'text-anchor="middle">{html.escape(cat[:14])}</text>')
    out.append("</svg>")
    return "".join(out)


def _svg_pie(categories, values, donut=False):
    total = sum(v for v in values if v > 0) or 1
    size, radius = 240, 100
    cx = cy = size / 2
    out = [f'<div class="pie-row"><svg viewBox="0 0 {size} {size}" class="svg-pie" role="img">']
    start = -math.pi / 2
    for i, value in enumerate(values):
        if value <= 0:
            continue
        sweep = 2 * math.pi * value / total
        if sweep >= 2 * math.pi - 1e-6:
            sweep = 2 * math.pi - 1e-4
        x1, y1 = cx + radius * math.cos(start), cy + radius * math.sin(start)
        x2, y2 = cx + radius * math.cos(start + sweep), cy + radius * math.sin(start + sweep)
        large = 1 if sweep > math.pi else 0
        colour = i % 6
        if donut:
            inner = radius * 0.78
            x3, y3 = cx + inner * math.cos(start + sweep), cy + inner * math.sin(start + sweep)
            x4, y4 = cx + inner * math.cos(start), cy + inner * math.sin(start)
            d = (f"M{x1:.1f},{y1:.1f} A{radius},{radius} 0 {large} 1 {x2:.1f},{y2:.1f} "
                 f"L{x3:.1f},{y3:.1f} A{inner:.1f},{inner:.1f} 0 {large} 0 {x4:.1f},{y4:.1f} Z")
        else:
            d = (f"M{cx},{cy} L{x1:.1f},{y1:.1f} A{radius},{radius} 0 {large} 1 "
                 f"{x2:.1f},{y2:.1f} Z")
        out.append(f'<path d="{d}" class="slice f{colour}" style="--d:{i * 70}ms">'
                   f"<title>{html.escape(categories[i] if i < len(categories) else '')}: "
                   f"{_fmt(value)}</title></path>")
        start += sweep
    out.append("</svg><ul class=\"pie-legend\">")
    for i, value in enumerate(values):
        if value > 0:
            label = categories[i] if i < len(categories) else str(i + 1)
            out.append(f'<li><i class="b{i % 6}"></i>'
                       f"{html.escape(label)}<b>{value / total * 100:.0f}%</b></li>")
    out.append("</ul></div>")
    return "".join(out)


# --------------------------------------------------------------------------- #
# Diagrams (Mermaid flowchart / sequence / pie subset)
# --------------------------------------------------------------------------- #
_NODE = re.compile(r"""^\s*([A-Za-z0-9_]+)\s*(?:
    \(\((.+?)\)\) | \(\[(.+?)\]\) | \[\((.+?)\)\] | \{(.+?)\} | \[(.+?)\] | \((.+?)\) | >(.+?)\] )?""",
                   re.X)
_EDGE_SPLIT = re.compile(r"\s*(<?-{2,}>?|<?={2,}>?|-\.+->?|-\s[^->]+?\s-+>|--\s[^->]+?\s-->)\s*")


def _clean_label(text):
    value = str(text or "").strip().strip('"').replace("<br/>", " ").replace("<br>", " ")
    return plain(value)


def parse_flow(source):
    """(direction, nodes{id:(label,shape)}, edges[(a,b,label,style)]) or None."""
    lines = [ln.strip() for ln in str(source).splitlines() if ln.strip()
             and not ln.strip().startswith(("%%", "classDef", "class ", "style ", "linkStyle",
                                            "click ", "end", "subgraph", "direction"))]
    if not lines or not re.match(r"^(graph|flowchart)\b", lines[0], re.I):
        return None
    head = lines[0].split()
    direction = (head[1].upper() if len(head) > 1 else "TD")
    direction = {"TB": "TD"}.get(direction, direction)
    nodes, edges = {}, []

    def node_from(token):
        match = _NODE.match(token)
        if not match:
            return None
        ident = match.group(1)
        label = next((g for g in match.groups()[1:] if g), None)
        shape = "round"
        if match.group(2):
            shape = "circle"
        elif match.group(3):
            shape = "pill"
        elif match.group(4):
            shape = "cyl"
        elif match.group(5):
            shape = "diamond"
        elif match.group(6):
            shape = "rect"
        if label is not None:
            nodes[ident] = (_clean_label(label), shape)
        else:
            nodes.setdefault(ident, (ident, "round"))
        return ident

    for line in lines[1:]:
        line = line.rstrip(";")
        pieces = re.split(r"\s+(-->|---|==>|-\.->|-\.-|--[ox]|<-->)\s+|\s*(-->\|[^|]*\||---\|[^|]*\|)\s*",
                          line)
        pieces = [p for p in pieces if p is not None and p != ""]
        previous, pending_label, pending_style = None, "", "solid"
        for piece in pieces:
            token = piece.strip()
            if re.fullmatch(r"-->|---|==>|-\.->|-\.-|--[ox]|<-->", token):
                pending_style = "dash" if "." in token else ("thick" if "=" in token else "solid")
                continue
            labelled = re.fullmatch(r"(-->|---)\|([^|]*)\|", token)
            if labelled:
                pending_label = _clean_label(labelled.group(2))
                continue
            inline_edge = re.match(r"^(.+?)\s+--\s+(.+?)\s+-->\s+(.+)$", token)
            if inline_edge:
                a = node_from(inline_edge.group(1))
                b = node_from(inline_edge.group(3))
                if a and b:
                    edges.append((a, b, _clean_label(inline_edge.group(2)), "solid"))
                previous = b
                continue
            ident = node_from(re.sub(r"^\|[^|]*\|\s*", "", token))
            if ident is None:
                continue
            if previous is not None:
                edges.append((previous, ident, pending_label, pending_style))
                pending_label, pending_style = "", "solid"
            previous = ident
    return (direction, nodes, edges) if nodes else None


def _layout(nodes, edges, direction):
    """Layered layout. Returns ({id:(x,y,w,h)}, width, height)."""
    ids = list(nodes)
    out_edges = {i: [] for i in ids}
    in_edges = {i: [] for i in ids}
    for a, b, *_ in edges:
        if a in out_edges and b in in_edges and a != b:
            out_edges[a].append(b)
            in_edges[b].append(a)
    # Ranks by longest path, ignoring edges that close a cycle.
    rank, state = {}, {}

    def visit(node, trail=()):
        if node in rank:
            return rank[node]
        if node in trail:
            return 0
        best = 0
        for parent in in_edges[node]:
            if parent in trail or parent == node:
                continue
            best = max(best, visit(parent, trail + (node,)) + 1)
        rank[node] = best
        return best

    for node in ids:
        visit(node)
    layers = {}
    for node in ids:
        layers.setdefault(rank[node], []).append(node)
    order = {n: i for layer in layers.values() for i, n in enumerate(layer)}
    for _ in range(3):
        for r in sorted(layers):
            if r == 0:
                continue
            layers[r].sort(key=lambda n: sum(order[p] for p in in_edges[n] if rank.get(p, 0) < r)
                           / max(1, len([p for p in in_edges[n] if rank.get(p, 0) < r])))
            for i, n in enumerate(layers[r]):
                order[n] = i

    def box(node):
        label = nodes[node][0]
        shape = nodes[node][1]
        lines = _wrap(label, 22)
        w = max(92, max(len(l) for l in lines) * 8.6 + 36)
        h = 46 + 18 * (len(lines) - 1)
        if shape == "diamond":
            w, h = w * 1.25, h * 1.35
        if shape == "circle":
            w = h = max(w, h)
        return w, h

    sizes = {n: box(n) for n in ids}
    horizontal = direction in ("LR", "RL")
    gap_rank, gap_node = (90, 28) if horizontal else (64, 34)
    positions, cursor = {}, 0.0
    layer_extent = {}
    for r in sorted(layers):
        extent = sum((sizes[n][1] if horizontal else sizes[n][0]) for n in layers[r]) \
            + gap_node * (len(layers[r]) - 1)
        layer_extent[r] = extent
    widest = max(layer_extent.values()) if layer_extent else 0
    for r in sorted(layers):
        thick = max((sizes[n][0] if horizontal else sizes[n][1]) for n in layers[r])
        offset = (widest - layer_extent[r]) / 2
        run = offset
        for n in layers[r]:
            w, h = sizes[n]
            if horizontal:
                positions[n] = (cursor + (thick - w) / 2, run, w, h)
                run += h + gap_node
            else:
                positions[n] = (run, cursor + (thick - h) / 2, w, h)
                run += w + gap_node
        cursor += thick + gap_rank
    cursor -= gap_rank
    if direction in ("RL", "BT"):
        total = cursor
        positions = {n: ((total - x - w) if horizontal else x,
                         y if horizontal else (total - y - h), w, h)
                     for n, (x, y, w, h) in positions.items()}
    width = (cursor if horizontal else widest) + 40
    height = (widest if horizontal else cursor) + 40
    return positions, width, height


def _wrap(text, limit):
    words, lines, current = str(text).split(), [], ""
    for word in words:
        if current and len(current) + 1 + len(word) > limit:
            lines.append(current)
            current = word
        else:
            current = f"{current} {word}".strip()
    if current:
        lines.append(current)
    return lines[:4] or [""]


def render_flow(source):
    parsed = parse_flow(source)
    if parsed is None:
        return None
    direction, nodes, edges = parsed
    if len(nodes) > 40:
        return None
    positions, width, height = _layout(nodes, edges, direction)
    pad = 20
    out = [f'<svg viewBox="0 0 {width:.0f} {height:.0f}" class="svg-flow" role="img">'
           '<defs><marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" '
           'markerHeight="7" orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z" '
           'class="arrowhead"/></marker></defs>']
    horizontal = direction in ("LR", "RL")
    for index, (a, b, label, style) in enumerate(edges):
        if a not in positions or b not in positions:
            continue
        ax, ay, aw, ah = positions[a]
        bx, by, bw, bh = positions[b]
        ax += pad
        ay += pad
        bx += pad
        by += pad
        if horizontal:
            forward = bx >= ax
            sx = ax + aw if forward else ax
            ex = bx if forward else bx + bw
            sy, ey = ay + ah / 2, by + bh / 2
            mid = (sx + ex) / 2
            d = f"M{sx:.1f},{sy:.1f} C{mid:.1f},{sy:.1f} {mid:.1f},{ey:.1f} {ex:.1f},{ey:.1f}"
            lx, ly = mid, (sy + ey) / 2 - 8
        else:
            down = by >= ay
            sy = ay + ah if down else ay
            ey = by if down else by + bh
            sx, ex = ax + aw / 2, bx + bw / 2
            mid = (sy + ey) / 2
            d = f"M{sx:.1f},{sy:.1f} C{sx:.1f},{mid:.1f} {ex:.1f},{mid:.1f} {ex:.1f},{ey:.1f}"
            lx, ly = (sx + ex) / 2 + 6, mid - 4
        out.append(f'<path d="{d}" class="edge {style}" marker-end="url(#arrow)" '
                   f'style="--d:{index * 90}ms"/>')
        if label:
            out.append(f'<text x="{lx:.1f}" y="{ly:.1f}" class="edge-label" '
                       f'text-anchor="middle">{html.escape(label)}</text>')
    for order, (ident, (x, y, w, h)) in enumerate(positions.items()):
        x += pad
        y += pad
        label, shape = nodes[ident]
        lines = _wrap(label, 22)
        cx, cy = x + w / 2, y + h / 2
        style = f'style="--d:{order * 70}ms"'
        if shape == "diamond":
            shape_svg = (f'<polygon points="{cx:.1f},{y:.1f} {x + w:.1f},{cy:.1f} {cx:.1f},'
                         f'{y + h:.1f} {x:.1f},{cy:.1f}" class="node-shape"/>')
        elif shape == "circle":
            shape_svg = f'<ellipse cx="{cx:.1f}" cy="{cy:.1f}" rx="{w / 2:.1f}" ry="{h / 2:.1f}" class="node-shape"/>'
        elif shape == "pill":
            shape_svg = f'<rect x="{x:.1f}" y="{y:.1f}" width="{w:.1f}" height="{h:.1f}" rx="{h / 2:.1f}" class="node-shape"/>'
        elif shape == "rect":
            shape_svg = f'<rect x="{x:.1f}" y="{y:.1f}" width="{w:.1f}" height="{h:.1f}" rx="6" class="node-shape"/>'
        else:
            shape_svg = f'<rect x="{x:.1f}" y="{y:.1f}" width="{w:.1f}" height="{h:.1f}" rx="14" class="node-shape"/>'
        text_y = cy - (len(lines) - 1) * 9 + 5
        text = "".join(f'<tspan x="{cx:.1f}" y="{text_y + i * 18:.1f}">{html.escape(l)}</tspan>'
                       for i, l in enumerate(lines))
        out.append(f'<g class="node tone{order % 5}" {style}>{shape_svg}'
                   f'<text text-anchor="middle" class="node-text">{text}</text></g>')
    out.append("</svg>")
    return f'<figure class="diagram reveal">{"".join(out)}</figure>'


def render_sequence(source):
    lines = [ln.strip() for ln in source.splitlines() if ln.strip()]
    if not lines or not lines[0].lower().startswith("sequencediagram"):
        return None
    actors, messages = [], []

    def actor(name):
        name = name.strip()
        if name and name not in actors:
            actors.append(name)

    for ln in lines[1:]:
        match = re.match(r"^(participant|actor)\s+(\S+)(?:\s+as\s+(.+))?$", ln, re.I)
        if match:
            actor(match.group(3) or match.group(2))
            continue
        match = re.match(r"^(.+?)\s*(-->>|->>|-->|->|--x|-x|--\)|-\))\s*(.+?)\s*:\s*(.*)$", ln)
        if match:
            a, arrow, b, text = match.groups()
            actor(a)
            actor(b)
            messages.append((a.strip(), b.strip(), text.strip(), "--" in arrow))
    if not actors or not messages:
        return None
    col, top, row = 170, 60, 52
    width = col * len(actors) + 40
    height = top + row * len(messages) + 40
    cx = {a: 20 + col / 2 + col * i for i, a in enumerate(actors)}
    out = [f'<svg viewBox="0 0 {width} {height}" class="svg-flow" role="img"><defs>'
           '<marker id="sarrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" '
           'markerHeight="7" orient="auto"><path d="M0,0 L10,5 L0,10 z" class="arrowhead"/>'
           "</marker></defs>"]
    for a in actors:
        out.append(f'<rect x="{cx[a] - 62}" y="10" width="124" height="34" rx="10" class="node-shape tone0"/>'
                   f'<text x="{cx[a]}" y="32" text-anchor="middle" class="node-text">{html.escape(a[:16])}</text>'
                   f'<line x1="{cx[a]}" x2="{cx[a]}" y1="44" y2="{height - 16}" class="lifeline"/>')
    for i, (a, b, text, dashed) in enumerate(messages):
        y = top + 12 + i * row
        x1, x2 = cx[a], cx[b]
        out.append(f'<line x1="{x1}" x2="{x2 - (8 if x2 > x1 else -8)}" y1="{y}" y2="{y}" '
                   f'class="edge {"dash" if dashed else "solid"}" marker-end="url(#sarrow)" '
                   f'style="--d:{i * 100}ms"/>'
                   f'<text x="{(x1 + x2) / 2}" y="{y - 8}" text-anchor="middle" class="edge-label">'
                   f'{html.escape(text[:40])}</text>')
    out.append("</svg>")
    return f'<figure class="diagram reveal">{"".join(out)}</figure>'


def render_mermaid_pie(source):
    lines = [ln.strip() for ln in source.splitlines() if ln.strip()]
    if not lines or not lines[0].lower().startswith("pie"):
        return None
    title_match = re.match(r"^pie(?:\s+showdata)?\s*(?:title\s+(.*))?$", lines[0], re.I)
    title = title_match.group(1) if title_match and title_match.group(1) else ""
    labels, values = [], []
    for ln in lines[1:]:
        if ln.lower().startswith("title "):
            title = ln[6:]
            continue
        match = re.match(r'^"?(.+?)"?\s*:\s*([\d.]+)$', ln)
        if match:
            labels.append(match.group(1))
            values.append(float(match.group(2)))
    if not values:
        return None
    heading = f'<div class="chart-title">{inline(title)}</div>' if title else ""
    return f'<figure class="chart reveal">{heading}{_svg_pie(labels, values, donut=True)}</figure>'


# --------------------------------------------------------------------------- #
# Blocks -> HTML
# --------------------------------------------------------------------------- #
def render_code(language, text):
    label = html.escape(language) if language else "text"
    return (f'<div class="code reveal"><div class="code-head"><span>{label}</span>'
            f'<button class="copy" type="button">Copy</button></div>'
            f"<pre><code>{html.escape(text)}</code></pre></div>")


def render_math(source):
    """Render a LaTeX display formula as a self-contained SVG image.

    The report window is deliberately offline and its CSP forbids remote
    scripts, so loading MathJax from a CDN would make formula rendering both
    brittle and less private.  Matplotlib is already part of VANTA's visual
    stack; its mathtext renderer gives us deterministic local SVG.  Unsupported
    LaTeX remains visible as source instead of disappearing.
    """
    latex = str(source or "").strip()
    if not latex:
        return ""
    expression = latex if latex.startswith("$") and latex.endswith("$") else f"${latex}$"
    try:
        from matplotlib.mathtext import math_to_image
        from matplotlib import rc_context
        from matplotlib.font_manager import FontProperties

        target = io.BytesIO()
        # math_to_image otherwise inherits savefig's white background, which
        # makes light glyphs unreadable on both report themes. Keep the SVG
        # transparent and let CSS adapt its dark glyphs to the active theme.
        with rc_context({'savefig.transparent': True, 'savefig.facecolor': 'none',
                         'figure.facecolor': 'none', 'savefig.edgecolor': 'none'}):
            math_to_image(expression, target, format="svg", dpi=180,
                          color="#1c1c1e", prop=FontProperties(size=20))
        encoded = base64.b64encode(target.getvalue()).decode("ascii")
        alt = html.escape(latex, quote=True)
        return (f'<figure class="formula reveal"><img '
                f'src="data:image/svg+xml;base64,{encoded}" alt="{alt}" /></figure>')
    except Exception:
        return (f'<figure class="formula formula-source reveal" role="math">'
                f'<code>{html.escape(latex)}</code></figure>')


def render_timeline(source):
    """Render the basic Mermaid timeline grammar locally; never print it as code."""
    lines = [line.strip() for line in source.splitlines() if line.strip() and not line.strip().startswith('%%')]
    if not lines or lines[0].lower() != 'timeline':
        return None
    title, rows, previous = '', [], ''
    for line in lines[1:]:
        if line.lower().startswith('title '):
            title = line[6:][:160]
        elif line.lower().startswith('section '):
            rows.append((line[8:][:80], '', True))
        elif ':' in line:
            period, _, event = line.partition(':')
            previous = period.strip() or previous
            if not previous or not event.strip():
                return None
            rows.append((previous[:80], event.strip()[:600], False))
        else:
            return None
    if not rows or len(rows)>40:
        return None
    heights = [max(54, max(len(_wrap(period, 18)), len(_wrap(event, 62)))*19+24) for period,event,_ in rows]
    height = sum(heights)+(50 if title else 18)+12
    output = [f'<figure class="diagram timeline reveal"><svg class="svg-flow" viewBox="0 0 720 {height}" role="img" aria-label="{html.escape(title or "Timeline", quote=True)}">']
    y = 42 if title else 20
    if title:
        output.append(f'<text x="16" y="23" class="node-text">{html.escape(title)}</text>')
    output.append(f'<line x1="164" y1="{y}" x2="164" y2="{height-20}" class="lifeline"/>')
    for (period,event,section),row_height in zip(rows,heights):
        for i,line in enumerate(_wrap(period, 18)):
            output.append(f'<text x="148" y="{y+16+i*19}" text-anchor="end" class="node-text">{html.escape(line)}</text>')
        output.append(f'<circle cx="164" cy="{y+12}" r="4" style="fill:var(--accent)"/>')
        for i,line in enumerate(_wrap(event, 62)):
            output.append(f'<text x="184" y="{y+16+i*19}" class="node-text">{html.escape(line)}</text>')
        y += row_height
    return ''.join(output)+'</svg></figure>'


_UNSUPPORTED_MERMAID = {"gantt", "erdiagram", "classdiagram", "statediagram", "statediagram-v2",
                        "journey", "mindmap", "gitgraph", "quadrantchart",
                        "requirementdiagram", "c4context", "sankey-beta", "xychart-beta"}


def render_fence(block):
    language, arg, text = block["language"], block["arg"], block["text"]
    if language in {"image", "scene3d", "model3d", "animation", "questionnaire", "explorer", "lesson", "walkthrough"}:
        from report_media import render_media
        return render_media(language, text)
    if language == "chart":
        chart = render_chart(arg, text)
        if chart:
            return chart
    if language in ("mermaid", "diagram", "flow", "flowchart"):
        head = text.lstrip().split(None, 1)[0].lower() if text.strip() else ""
        if head == 'timeline':
            return render_timeline(text) or render_code(language, text)
        if head in _UNSUPPORTED_MERMAID:
            return render_code(language, text)
        source = text if text.lstrip().lower().startswith(
            ("graph", "flowchart", "sequencediagram", "pie")) else "graph TD\n" + text
        for renderer in (render_flow, render_sequence, render_mermaid_pie):
            drawn = renderer(source)
            if drawn:
                return drawn
    return render_code(language, text)


def render_blocks(blocks, block_cache=None):
    """(html, toc[(id, text, level)])"""
    parts, toc, taken = [], [], set()
    def visual(block, render):
        key = json.dumps(block, sort_keys=True)
        if block_cache is not None and key in block_cache:
            return block_cache[key]
        result = render()
        if block_cache is not None and len(key) + len(result) <= 2_000_000:
            if len(block_cache) >= 8:
                block_cache.pop(next(iter(block_cache)))
            block_cache[key] = result
        return result
    for block in blocks:
        kind = block["kind"]
        if kind == "heading":
            ident = slug(block["text"], taken)
            taken.add(ident)
            level = block["level"]
            toc.append((ident, plain(block["text"]), level))
            parts.append(f'<h{level + 1} id="{ident}" class="reveal">{inline(block["text"])}'
                         f"</h{level + 1}>")
        elif kind == "para":
            parts.append(f'<p class="reveal">{inline(block["text"])}</p>')
        elif kind == "list":
            parts.append(render_list(block))
        elif kind == "table":
            parts.append(render_table(block))
        elif kind == "facts":
            parts.append(render_facts(block))
        elif kind == "fence":
            parts.append(visual(block, lambda: render_fence(block))
                         if block['language'] in {'image', 'scene3d', 'model3d', 'animation', 'questionnaire', 'mermaid', 'chart', 'explorer', 'lesson', 'walkthrough'}
                         else render_fence(block))
        elif kind == "math":
            parts.append(visual(block, lambda: render_math(block["text"])))
        elif kind == "callout":
            label = CALLOUT_KINDS.get(block["tone"], "Note")
            parts.append(f'<aside class="callout {block["tone"]} reveal"><b>{label}</b>'
                         f'<div>{inline(block["text"])}</div></aside>')
        elif kind == "quote":
            parts.append(f'<blockquote class="reveal">{inline(block["text"])}</blockquote>')
        elif kind == "progress":
            parts.append(f'<div class="progress reveal"><div class="progress-label">'
                         f'{inline(block["text"])}<b>{block["value"]}%</b></div><div class="track">'
                         f'<div class="fill" style="--w:{block["value"]}%"></div></div></div>')
        elif kind == "rule":
            parts.append('<hr class="reveal"/>')
    return "\n".join(parts), toc


def _structured_diagram(block):
    """Mermaid text from {'nodes': [{id, label}], 'edges': [{from, to, label}]}."""
    def ident(value):
        return re.sub(r"\W+", "_", str(value)) or "n"

    lines = ["graph TD"]
    for node in block.get("nodes") or []:
        if isinstance(node, dict) and node.get("id") is not None:
            label = str(node.get("label") or node["id"]).replace('"', "'")
            lines.append(f'{ident(node["id"])}["{label}"]')
    for edge in block.get("edges") or []:
        if not isinstance(edge, dict):
            continue
        a = edge.get("from", edge.get("source"))
        b = edge.get("to", edge.get("target"))
        if a is None or b is None:
            continue
        label = str(edge.get("label") or "").replace("|", "/")
        lines.append(f"{ident(a)} -->|{label}| {ident(b)}" if label
                     else f"{ident(a)} --> {ident(b)}")
    return "\n".join(lines)


def blocks_from_payload(payload):
    """Markdown text for a rich payload ({'blocks': [...]}) or a plain one."""
    if isinstance(payload, dict) and isinstance(payload.get("blocks"), list):
        pieces, actions = [], []
        for block in payload["blocks"]:
            if not isinstance(block, dict):
                continue
            kind = str(block.get("type") or block.get("kind") or "markdown").lower()
            if kind in ("markdown", "text"):
                pieces.append(str(block.get("text") or block.get("content") or ""))
            elif kind == "table":
                headers = [str(h) for h in block.get("headers") or block.get("columns") or []]
                rows = block.get("rows") or []
                if headers:
                    pieces.append("| " + " | ".join(headers) + " |\n|"
                                  + "|".join("---" for _ in headers) + "|\n"
                                  + "\n".join("| " + " | ".join(str(c) for c in r) + " |"
                                              for r in rows))
            elif kind == "diagram":
                source = str(block.get("source") or block.get("text") or "")
                if not source and (block.get("nodes") or block.get("edges")):
                    source = _structured_diagram(block)
                pieces.append("```mermaid\n" + source + "\n```")
            elif kind in ("image", "scene3d", "model3d", "animation", "questionnaire", "explorer", "lesson", "walkthrough"):
                # JSON escapes newlines in captions/labels; they cannot close a fence.
                pieces.append("```" + kind + "\n" + json.dumps(block) + "\n```")
            elif kind == "code":
                pieces.append("```" + str(block.get("language") or "") + "\n"
                              + str(block.get("text") or block.get("code") or "") + "\n```")
            elif kind in ("math", "formula", "latex"):
                pieces.append("$$\n" + str(block.get("latex") or block.get("text")
                                             or block.get("content") or "") + "\n$$")
            elif kind == "chart":
                pieces.append("```chart " + str(block.get("chart") or "column") + " "
                              + str(block.get("title") or "") + "\n"
                              + str(block.get("text") or "") + "\n```")
            elif kind == "actions":
                for item in block.get("actions") or block.get("items") or []:
                    if isinstance(item, dict) and item.get("label") and item.get("command"):
                        actions.append({"label": str(item["label"])[:60],
                                        "command": str(item["command"])[:300]})
        return "\n\n".join(pieces), actions
    text = ""
    if isinstance(payload, dict):
        text = str(payload.get("content") or payload.get("text") or "")
    else:
        text = str(payload or "")
    return text, []


# --------------------------------------------------------------------------- #
# The document
# --------------------------------------------------------------------------- #
def render_report(markdown, title="VANTA Report", actions=(), subtitle="", block_cache=None):
    """A dict for the report window: {'title', 'html', 'toc', 'actions'}."""
    blocks = parse(markdown)
    # A leading "# Title" is the page title, not a second heading under it.
    if blocks and blocks[0]["kind"] == "heading" and blocks[0]["level"] == 1:
        lead = plain(blocks[0]["text"])
        if not title or title in ("VANTA Result", "VANTA Report", "Result")                 or lead.lower() == str(title).lower():
            title = lead
        blocks = blocks[1:]
    # The shallowest heading left is the top section, whatever it was written
    # as: "## Goals" under a dropped "# Title" is a section, not a subsection.
    levels = [b["level"] for b in blocks if b["kind"] == "heading"]
    if levels and min(levels) > 1:
        shift = min(levels) - 1
        blocks = [dict(b, level=b["level"] - shift) if b["kind"] == "heading" else b
                  for b in blocks]
    body, toc = render_blocks(blocks, block_cache)
    if not body.strip():
        body = '<p class="reveal">Nothing to show.</p>'
    buttons = "".join(
        f'<button type="button" class="action" data-command="{html.escape(a["command"], quote=True)}">'
        f'{html.escape(a["label"])}</button>' for a in actions)
    action_bar = f'<div class="actions reveal">{buttons}</div>' if buttons else ""
    from report_forms import forms_in_markup
    return {"title": str(title or "VANTA Report"), "subtitle": subtitle,
            "forms": forms_in_markup(body),
            "html": body + action_bar,
            "toc": [{"id": i, "text": t, "level": l} for i, t, l in toc if l <= 2],
            "source": str(markdown or "")}


def validate_scenes(markdown, depth=0, in_animation=False):
    """Return actionable 3D contract errors before a report claims delivery."""
    from report_media import scene_data
    errors = []
    if depth > 3:
        return ['Nested report animation exceeds the supported depth']
    for block in parse(markdown):
        if block.get('kind') != 'fence' or block.get('language') not in {'scene3d', 'model3d', 'animation', 'questionnaire', 'explorer', 'lesson', 'walkthrough'}:
            continue
        try:
            data = json.loads(block['text'])
            if block['language'] == 'questionnaire':
                if in_animation:
                    raise ValueError('Questionnaires must be standalone report blocks, outside animation frames')
                from report_forms import normalize
                normalize(data)
            elif block['language'] in {'explorer', 'lesson', 'walkthrough'}:
                from report_learning import validate
                validate(block['language'], data, depth=depth, in_animation=in_animation)
            elif block['language'] == 'animation':
                if not isinstance(data, dict) or not isinstance(data.get('frames'), list):
                    raise ValueError('Animation requires a frames array')
                for frame in data['frames'][:20]:
                    if isinstance(frame, dict):
                        nested, _ = blocks_from_payload(frame)
                        errors.extend(validate_scenes(nested, depth+1, in_animation=True))
            else:
                scene_data(data)
        except (ValueError, TypeError, KeyError, OSError, OverflowError, RecursionError) as exc:
            errors.append(str(exc))
        if len(errors) >= 3:
            break
    return errors[:3]
