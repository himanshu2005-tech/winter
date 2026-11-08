# -*- coding: utf-8 -*-
"""
office_docs.py -- making presentations, documents, spreadsheets and PDFs.

One input language for all four: plain Markdown-style text, because that is
what a language model writes fluently and what survives being placed between
<ACTION ...> and </ACTION> (XML attributes mangle quotes and newlines).

    # Slide or section title
    ## Subheading
    - bullet            (indent two spaces per level)
    1. numbered item
    | a | b |           (a table; the |---| row is optional)
    ![caption](C:\\path\\image.png)
    > a pull quote
    Notes: speaker notes (presentations)
    ```chart column Revenue by quarter
    categories: Q1, Q2, Q3
    2025: 10, 12, 14
    ```
    ---                  (a new slide / a rule)

DESIGN RULES THAT KEEP THE OUTPUT FROM BEING A BUG
--------------------------------------------------
* Nothing is ever overwritten. An existing file gets " (2)" appended.
* Nothing is drawn outside its box. Text is fitted by estimating its wrapped
  height and shrinking the font; if it still does not fit at the minimum size
  the slide is split into a continuation rather than overflowing.
* Every slide is drawn on the blank layout with explicit geometry, so the result
  does not depend on a template whose placeholders may be missing.
* Failures are specific ("image not found: ...") and happen before anything is
  written, so a half-made file is never left behind.
"""

import math
import os
import re
from pathlib import Path

# --------------------------------------------------------------------------- #
# Paths
# --------------------------------------------------------------------------- #
FOLDER_ALIASES = {"desktop": "Desktop", "documents": "Documents",
                  "downloads": "Downloads", "pictures": "Pictures"}
DEFAULT_FOLDER = ("Documents", "VANTA")


def _home():
    return Path(os.path.expanduser("~"))


_DOC_EXTENSIONS = {".pptx", ".docx", ".xlsx", ".pdf", ".ppt", ".doc", ".xls", ".txt",
                   ".md", ".csv", ".odt", ".odp", ".ods", ".rtf"}


def slugify(text, fallback="untitled"):
    slug = re.sub(r"[^A-Za-z0-9]+", "-", str(text or "")).strip("-").lower()
    return (slug[:60] or fallback)


def resolve_output(path, title, extension, base_dir=None):
    """Where to write, never onto an existing file.

    Blank -> Documents\\VANTA\\<title>.<ext>. "Desktop\\x" and "Downloads\\x"
    expand. A relative path goes under Documents\\VANTA. A directory gets the
    title as its filename. The wrong extension is corrected rather than refused.
    """
    raw = str(path or "").strip().strip('"')
    ext = "." + extension.lstrip(".")
    if not raw:
        target = _home().joinpath(*DEFAULT_FOLDER) / (slugify(title) + ext)
    else:
        raw = os.path.expandvars(os.path.expanduser(raw))
        parts = re.split(r"[\\/]", raw)
        if parts and parts[0].lower() in FOLDER_ALIASES and not os.path.isabs(raw):
            raw = str(_home().joinpath(FOLDER_ALIASES[parts[0].lower()], *parts[1:]))
        target = Path(raw)
        if not target.is_absolute():
            target = _home().joinpath(*DEFAULT_FOLDER) / target
    if target.is_dir() or str(raw).endswith(("\\", "/")):
        target = target / (slugify(title) + ext)
    if target.suffix.lower() != ext:
        # Replace a document extension ("talk.docx" -> "talk.pptx") but only
        # append to anything else: "plan v1.2" must not become "plan v1.pptx".
        if target.suffix.lower() in _DOC_EXTENSIONS:
            target = target.with_suffix(ext)
        else:
            target = target.with_name(target.name + ext)
    target.parent.mkdir(parents=True, exist_ok=True)
    candidate, counter = target, 2
    while candidate.exists():
        candidate = target.with_name(f"{target.stem} ({counter}){target.suffix}")
        counter += 1
    return candidate


def resolve_image(path, near=None):
    """An existing image file for a Markdown image reference, or raise."""
    raw = os.path.expandvars(os.path.expanduser(str(path or "").strip().strip('"')))
    candidates = [Path(raw)]
    if not os.path.isabs(raw):
        for folder in filter(None, (near, _home().joinpath(*DEFAULT_FOLDER),
                                    _home() / "Pictures", _home() / "Desktop")):
            candidates.append(Path(folder) / raw)
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise ValueError(f"image not found: {path}")


# --------------------------------------------------------------------------- #
# The shared Markdown-ish parser
# --------------------------------------------------------------------------- #
_INLINE = re.compile(r"(\*\*[^*]+\*\*|__[^_]+__|\*[^*\s][^*]*\*|`[^`]+`)")


def inline_runs(text):
    """[(text, {'bold','italic','code'})] for **bold**, *italic* and `code`."""
    runs, position = [], 0
    for match in _INLINE.finditer(str(text)):
        if match.start() > position:
            runs.append((text[position:match.start()], {}))
        token = match.group(0)
        if token.startswith(("**", "__")):
            runs.append((token[2:-2], {"bold": True}))
        elif token.startswith("`"):
            runs.append((token[1:-1], {"code": True}))
        else:
            runs.append((token[1:-1], {"italic": True}))
        position = match.end()
    if position < len(text):
        runs.append((text[position:], {}))
    return runs or [("", {})]


def plain(text):
    return "".join(piece for piece, _ in inline_runs(text))


def _split_row(line):
    cells = line.strip().strip("|").split("|")
    return [cell.strip() for cell in cells]


def _is_separator(line):
    return bool(re.fullmatch(r"\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*", line))


def parse_blocks(text):
    """Blocks in order: dicts with a 'kind'.

    kinds: heading(level,text) para(text) bullet(level,text,ordered)
    table(rows) image(path,caption) quote(text) notes(text)
    code(text) chart(...) rule
    """
    blocks, lines = [], str(text or "").replace("\r\n", "\n").split("\n")
    index = 0
    while index < len(lines):
        raw = lines[index]
        line = raw.rstrip()
        stripped = line.strip()
        index += 1
        if not stripped:
            continue
        fence = re.match(r"^```\s*(\w+)?\s*(.*)$", stripped)
        if fence:
            kind, rest = (fence.group(1) or "").lower(), fence.group(2).strip()
            body = []
            while index < len(lines) and not lines[index].strip().startswith("```"):
                body.append(lines[index])
                index += 1
            index += 1
            if kind == "chart":
                blocks.append(_parse_chart(rest, body))
            else:
                blocks.append({"kind": "code", "text": "\n".join(body)})
            continue
        if re.fullmatch(r"-{3,}|\*{3,}|_{3,}", stripped):
            blocks.append({"kind": "rule"})
            continue
        if re.fullmatch(r"={3,}", stripped):
            blocks.append({"kind": "pagebreak"})
            continue
        heading = re.match(r"^(#{1,6})\s+(.*)$", stripped)
        if heading:
            blocks.append({"kind": "heading", "level": len(heading.group(1)),
                           "text": heading.group(2).strip()})
            continue
        if stripped.startswith("|"):
            rows = []
            index -= 1
            while index < len(lines) and lines[index].strip().startswith("|"):
                if not _is_separator(lines[index]):
                    rows.append(_split_row(lines[index]))
                index += 1
            if rows:
                width = max(len(r) for r in rows)
                rows = [r + [""] * (width - len(r)) for r in rows]
                blocks.append({"kind": "table", "rows": rows})
            continue
        image = re.match(r"^!\[([^\]]*)\]\(([^)]+)\)\s*$", stripped)
        if image:
            blocks.append({"kind": "image", "caption": image.group(1).strip(),
                           "path": image.group(2).strip()})
            continue
        if stripped.lower().startswith(("notes:", "note:", "speaker notes:")):
            body = stripped.split(":", 1)[1].strip()
            while (index < len(lines) and lines[index].strip()
                   and not re.match(r"^(#|[-*+]\s|\d+[.)]\s|\||---|```|!\[)", lines[index].strip())):
                body += " " + lines[index].strip()
                index += 1
            blocks.append({"kind": "notes", "text": body})
            continue
        if stripped.startswith(">"):
            blocks.append({"kind": "quote", "text": stripped.lstrip("> ").strip()})
            continue
        bullet = re.match(r"^(\s*)([-*+•]|\d+[.)])\s+(.*)$", line)
        if bullet:
            level = min(3, len(bullet.group(1).replace("\t", "  ")) // 2)
            blocks.append({"kind": "bullet", "level": level,
                           "ordered": bullet.group(2)[0].isdigit(),
                           "text": bullet.group(3).strip()})
            continue
        # A plain line continues the previous paragraph if there is one.
        if blocks and blocks[-1]["kind"] == "para" and lines[index - 2].strip():
            blocks[-1]["text"] += " " + stripped
        else:
            blocks.append({"kind": "para", "text": stripped})
    return blocks


def split_values(text):
    """Split "10, 12, 14" or "$1,200, 15%" into values.

    A comma followed by exactly three digits is a thousands separator, not a
    boundary -- splitting "$1,200" into 1 and 200 silently turned a revenue
    figure into two data points.
    """
    parts = re.split(r";|,\s+|,(?!\d{3}(?!\d))", str(text))
    return [p.strip() for p in parts if p.strip()]


def _parse_chart(header, body):
    parts = header.split(None, 1)
    kind = (parts[0].lower() if parts else "column")
    title = parts[1].strip().strip('"') if len(parts) > 1 else ""
    if kind not in {"column", "bar", "line", "pie", "area", "doughnut"}:
        title, kind = (header or "").strip(), "column"
    categories, series = [], []
    for line in body:
        if ":" not in line:
            continue
        name, values = line.split(":", 1)
        name, values = name.strip(), split_values(values)
        if name.lower() in ("categories", "labels", "x"):
            categories = values
            continue
        numbers = []
        for value in values:
            cleaned = re.sub(r"[^0-9eE+\-.]", "", value.replace(",", ""))
            try:
                numbers.append(float(cleaned))
            except ValueError:
                numbers.append(0.0)
        series.append((name, numbers))
    if not categories and series:
        categories = [str(i + 1) for i in range(len(series[0][1]))]
    return {"kind": "chart", "type": kind, "title": title,
            "categories": categories, "series": series}


# --------------------------------------------------------------------------- #
# Presentations
# --------------------------------------------------------------------------- #
THEMES = {
    "dark": dict(bg="0F172A", title="F8FAFC", text="E2E8F0", muted="94A3B8",
                 accent="38BDF8", panel="1E293B", header_text="0F172A"),
    "light": dict(bg="FFFFFF", title="0F172A", text="1E293B", muted="64748B",
                  accent="2563EB", panel="F1F5F9", header_text="FFFFFF"),
    "midnight": dict(bg="111827", title="FFFFFF", text="E5E7EB", muted="9CA3AF",
                     accent="F59E0B", panel="1F2937", header_text="111827"),
    "forest": dict(bg="0B241B", title="ECFDF5", text="D1FAE5", muted="86EFAC",
                   accent="34D399", panel="123A2B", header_text="0B241B"),
    "minimal": dict(bg="FAFAF9", title="18181B", text="27272A", muted="71717A",
                    accent="18181B", panel="F0EFEC", header_text="FAFAF9"),
    "corporate": dict(bg="FFFFFF", title="0B2545", text="1F2933", muted="52606D",
                      accent="13315C", panel="EEF2F7", header_text="FFFFFF"),
}
CHART_COLORS = ("2563EB", "F59E0B", "10B981", "EF4444", "8B5CF6", "06B6D4")

SLIDE_W, SLIDE_H = 13.333, 7.5
MARGIN = 0.75
TITLE_FONT_SIZE = 34
FONT = "Segoe UI"
MIN_BODY = 14
MAX_BODY = 28
#: Below this a slide is split rather than shrunk: 22pt is the smallest body
#: text that reads comfortably from across a room.
SPLIT_MIN = 22
MAX_TABLE_ROWS = 9


def _rgb(hex_value):
    from pptx.dml.color import RGBColor
    return RGBColor.from_string(hex_value)


def estimate_lines(text, size_pt, width_in):
    """Wrapped line count for `text` at `size_pt` in a box `width_in` wide.

    Average glyph width in Segoe UI is about 0.48 em (measured against a real
    PowerPoint render); word wrapping wastes some of every line, so a 5%
    allowance is added. Deliberately a little pessimistic:
    shrinking the font a point too early is invisible, overflowing is not.
    """
    chars_per_line = max(6, int((width_in * 72.0) / (size_pt * 0.48 * 1.05)))
    lines = 0
    for paragraph in str(text).split("\n"):
        lines += max(1, math.ceil(len(paragraph) / chars_per_line))
    return lines


def fit_bullets(items, width_in, height_in, start=MAX_BODY, minimum=MIN_BODY):
    """Largest font size at which all `items` [(level,text)] fit, or None."""
    size = start
    while size >= minimum:
        total = 0.0
        for level, text in items:
            indent = 0.4 * level + 0.35
            lines = estimate_lines(plain(text), size - 2 * level, width_in - indent)
            total += lines * (size * 1.2) / 72.0 + (size * 0.55) / 72.0
        if total <= height_in:
            return size
        size -= 1
    return None


def split_to_fit(items, width_in, height_in):
    """Chunks of bullets that each fit at a readable size."""
    chunks, current = [], []
    for item in items:
        trial = current + [item]
        if fit_bullets(trial, width_in, height_in, start=SPLIT_MIN, minimum=SPLIT_MIN) is None and current:
            chunks.append(current)
            current = [item]
        else:
            current = trial
    if current:
        chunks.append(current)
    # 20 + 1 leaves a slide with one orphan line. Move items from the previous
    # slide to the last one while both still fit and the last is shorter.
    def fits(group):
        return fit_bullets(group, width_in, height_in, start=SPLIT_MIN,
                           minimum=SPLIT_MIN) is not None

    for index in range(len(chunks) - 1, 0, -1):
        while len(chunks[index]) + 1 < len(chunks[index - 1]):
            candidate_prev = chunks[index - 1][:-1]
            candidate_last = [chunks[index - 1][-1]] + chunks[index]
            if candidate_prev and fits(candidate_prev) and fits(candidate_last):
                chunks[index - 1], chunks[index] = candidate_prev, candidate_last
            else:
                break
    return chunks


def group_slides(blocks):
    """Split blocks into slides: a level-1 heading or a rule starts a new one."""
    slides, current = [], None

    def start(title=""):
        nonlocal current
        current = {"title": title, "subtitle": "", "bullets": [], "table": None,
                   "image": None, "chart": None, "quote": "", "notes": [],
                   "paras": [], "code": ""}
        slides.append(current)

    def exclusive(slide):
        return bool(slide["table"] or slide["quote"] or slide["code"])

    def holds_content(slide):
        return bool(slide["bullets"] or slide["image"] or slide["chart"]
                    or slide["table"] or slide["quote"] or slide["code"])

    def room_for(kind):
        """Whether the current slide can take another element of this kind.

        A slide shows bullets beside ONE picture or chart, or a table, quote or
        code block on its own. Anything that does not fit that shape used to be
        dropped without a word (a quote after a table, a second picture), so it
        now continues on the next slide instead.
        """
        slide = current
        if kind in ("bullet", "para"):
            return not exclusive(slide)
        if kind in ("image", "chart"):
            return not exclusive(slide) and not (slide["image"] or slide["chart"])
        if kind in ("table", "quote", "code"):
            return not holds_content(slide)
        return True

    def continue_slide():
        base = current["title"] or ""
        title = base if base.endswith("(cont.)") or not base else base + " (cont.)"
        start(title)

    for block in blocks:
        kind = block["kind"]
        if kind == "heading" and block["level"] == 1:
            start(block["text"])
            continue
        if kind in ("rule", "pagebreak"):
            current = None
            continue
        if current is None:
            start()
        if kind in ("bullet", "para", "image", "chart", "table", "quote", "code") \
                and not room_for(kind):
            continue_slide()
        if kind == "heading":
            if not current["title"]:
                current["title"] = block["text"]
            elif not current["subtitle"] and not current["bullets"]:
                current["subtitle"] = block["text"]
            else:
                current["bullets"].append((0, "**" + block["text"] + "**"))
        elif kind == "bullet":
            marker = "\u2022 " if not block["ordered"] else ""
            current["bullets"].append((block["level"], block["text"]))
        elif kind == "para":
            if current["title"] and not current["bullets"] and not current["subtitle"] \
                    and len(slides) == 1 and not (current["table"] or current["chart"]):
                current["subtitle"] = block["text"]
            else:
                current["bullets"].append((0, block["text"]))
        elif kind == "table":
            current["table"] = block["rows"]
        elif kind == "image":
            current["image"] = block
        elif kind == "chart":
            current["chart"] = block
        elif kind == "quote":
            current["quote"] = block["text"]
        elif kind == "notes":
            current["notes"].append(block["text"])
        elif kind == "code":
            current["code"] = block["text"]
    return [s for s in slides if any((s["title"], s["subtitle"], s["bullets"],
                                       s["table"], s["image"], s["chart"],
                                       s["quote"], s["code"]))]


def _expand_for_fit(slide):
    """Continuation slides for bullets or table rows that cannot fit."""
    out = []
    body_w = SLIDE_W - 2 * MARGIN - (4.6 if slide["image"] or slide["chart"] else 0)
    body_h = SLIDE_H - 2.3
    if slide["table"] and len(slide["table"]) > MAX_TABLE_ROWS + 1:
        header, rows = slide["table"][0], slide["table"][1:]
        for part, start in enumerate(range(0, len(rows), MAX_TABLE_ROWS)):
            copy = dict(slide, table=[header] + rows[start:start + MAX_TABLE_ROWS],
                        title=slide["title"] + (" (cont.)" if part else ""),
                        notes=slide["notes"] if not part else [])
            out.append(copy)
        return out
    if slide["bullets"] and fit_bullets(slide["bullets"], body_w, body_h) is None:
        chunks = split_to_fit(slide["bullets"], body_w, body_h)
        for part, chunk in enumerate(chunks):
            out.append(dict(slide, bullets=chunk,
                            title=slide["title"] + (" (cont.)" if part else ""),
                            image=slide["image"] if not part else None,
                            chart=slide["chart"] if not part else None,
                            notes=slide["notes"] if not part else []))
        return out
    return [slide]


def _set_background(slide, color):
    fill = slide.background.fill
    fill.solid()
    fill.fore_color.rgb = _rgb(color)


def _add_text(slide, left, top, width, height, runs_or_text, size, color,
              bold=False, italic=False, align=None, anchor=None, font=FONT):
    from pptx.enum.text import MSO_ANCHOR, PP_ALIGN
    from pptx.util import Inches, Pt
    box = slide.shapes.add_textbox(Inches(left), Inches(top), Inches(width), Inches(height))
    frame = box.text_frame
    frame.word_wrap = True
    frame.margin_left = frame.margin_right = Inches(0.05)
    frame.margin_top = frame.margin_bottom = Inches(0.03)
    frame.vertical_anchor = anchor or MSO_ANCHOR.TOP
    paragraph = frame.paragraphs[0]
    if align:
        paragraph.alignment = align
    _fill_runs(paragraph, runs_or_text, size, color, bold, italic, font)
    return box


def _fill_runs(paragraph, text, size, color, bold=False, italic=False, font=FONT, accent=None):
    from pptx.util import Pt
    for piece, style in inline_runs(text):
        run = paragraph.add_run()
        run.text = piece
        run.font.size = Pt(size)
        run.font.name = "Consolas" if style.get("code") else font
        run.font.bold = bool(bold or style.get("bold"))
        run.font.italic = bool(italic or style.get("italic"))
        run.font.color.rgb = _rgb(accent if (style.get("code") and accent) else color)


def _add_rect(slide, left, top, width, height, color):
    from pptx.enum.shapes import MSO_SHAPE
    from pptx.util import Inches
    shape = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, Inches(left), Inches(top),
                                   Inches(width), Inches(height))
    shape.fill.solid()
    shape.fill.fore_color.rgb = _rgb(color)
    shape.line.fill.background()
    shape.shadow.inherit = False
    return shape


def _draw_title(slide, theme, title, subtitle=""):
    _add_rect(slide, MARGIN - 0.2, 0.62, 0.09, 0.78, theme["accent"])
    size = TITLE_FONT_SIZE if len(title) <= 48 else (28 if len(title) <= 80 else 24)
    _add_text(slide, MARGIN, 0.5, SLIDE_W - 2 * MARGIN, 0.95, title, size,
              theme["title"], bold=True)
    if subtitle:
        _add_text(slide, MARGIN, 1.38, SLIDE_W - 2 * MARGIN, 0.5, subtitle, 18,
                  theme["muted"])


def _draw_footer(slide, theme, number, footer):
    from pptx.enum.text import PP_ALIGN
    if footer:
        _add_text(slide, MARGIN, SLIDE_H - 0.55, 8.0, 0.35, footer, 11, theme["muted"])
    _add_text(slide, SLIDE_W - MARGIN - 1.0, SLIDE_H - 0.55, 1.0, 0.35, str(number), 11,
              theme["muted"], align=PP_ALIGN.RIGHT)


def _draw_bullets(slide, theme, items, left, top, width, height):
    from pptx.util import Inches, Pt
    size = fit_bullets(items, width, height) or MIN_BODY
    box = slide.shapes.add_textbox(Inches(left), Inches(top), Inches(width), Inches(height))
    frame = box.text_frame
    frame.word_wrap = True
    first = True
    for level, text in items:
        paragraph = frame.paragraphs[0] if first else frame.add_paragraph()
        first = False
        paragraph.space_after = Pt(size * 0.55)
        indent = 0.4 * level
        marker = "\u2022  " if level == 0 else ("\u2013  " if level == 1 else "\u00b7  ")
        if re.match(r"^\*\*.+\*\*$", text.strip()):
            marker = ""
        paragraph.level = 0
        pPr = paragraph._p.get_or_add_pPr()
        pPr.set("marL", str(int(Inches(0.38 + indent))))
        pPr.set("indent", str(int(-Inches(0.3))) if marker else "0")
        from pptx.oxml.ns import qn
        if marker:
            for tag in ("a:buNone", "a:buChar", "a:buAutoNum"):
                for node in pPr.findall(qn(tag)):
                    pPr.remove(node)
            bullet = pPr.makeelement(qn("a:buChar"), {"char": marker.strip()})
            pPr.append(bullet)
        _fill_runs(paragraph, text, size - 2 * level, theme["text"], accent=theme["accent"])
    return box


def _draw_table(slide, theme, rows, left, top, width, height):
    from pptx.enum.text import MSO_ANCHOR
    from pptx.util import Inches, Pt
    cols = len(rows[0])
    row_h = min(0.7, height / max(1, len(rows)))
    shape = slide.shapes.add_table(len(rows), cols, Inches(left), Inches(top),
                                   Inches(width), Inches(row_h * len(rows)))
    table = shape.table
    font_size = 22 if cols <= 3 else (18 if cols <= 4 else (15 if cols <= 6 else 12))
    if len(rows) > 6:
        font_size = max(11, font_size - 3)
    for r, row in enumerate(rows):
        for c, value in enumerate(row):
            cell = table.cell(r, c)
            cell.vertical_anchor = MSO_ANCHOR.MIDDLE
            cell.margin_left = cell.margin_right = Inches(0.1)
            cell.fill.solid()
            if r == 0:
                cell.fill.fore_color.rgb = _rgb(theme["accent"])
            else:
                cell.fill.fore_color.rgb = _rgb(theme["panel"] if r % 2 else theme["bg"])
            frame = cell.text_frame
            frame.word_wrap = True
            paragraph = frame.paragraphs[0]
            color = theme["header_text"] if r == 0 else theme["text"]
            _fill_runs(paragraph, value, font_size, color, bold=(r == 0))
    return shape


def _draw_chart(slide, theme, spec, left, top, width, height):
    from pptx.chart.data import CategoryChartData
    from pptx.enum.chart import XL_CHART_TYPE, XL_LEGEND_POSITION
    from pptx.util import Inches, Pt
    kinds = {"column": XL_CHART_TYPE.COLUMN_CLUSTERED, "bar": XL_CHART_TYPE.BAR_CLUSTERED,
             "line": XL_CHART_TYPE.LINE_MARKERS, "pie": XL_CHART_TYPE.PIE,
             "area": XL_CHART_TYPE.AREA, "doughnut": XL_CHART_TYPE.DOUGHNUT}
    data = CategoryChartData()
    data.categories = spec["categories"]
    for name, values in spec["series"]:
        padded = (values + [0.0] * len(spec["categories"]))[:len(spec["categories"])]
        data.add_series(name, padded)
    frame = slide.shapes.add_chart(kinds[spec["type"]], Inches(left), Inches(top),
                                   Inches(width), Inches(height), data)
    chart = frame.chart
    chart.font.size = Pt(15)
    chart.font.name = FONT
    chart.font.color.rgb = _rgb(theme["text"])
    chart.has_title = bool(spec["title"])
    if spec["title"]:
        chart.chart_title.text_frame.text = spec["title"]
        for paragraph in chart.chart_title.text_frame.paragraphs:
            for run in paragraph.runs:
                run.font.size = Pt(20)
                run.font.bold = True
                run.font.color.rgb = _rgb(theme["title"])
    many = len(spec["series"]) > 1 or spec["type"] in ("pie", "doughnut")
    chart.has_legend = many
    if many:
        chart.legend.position = XL_LEGEND_POSITION.BOTTOM
        chart.legend.include_in_layout = False
    if spec["type"] in ("pie", "doughnut"):
        points = chart.plots[0].series[0].points
        for i in range(len(spec["categories"])):
            points[i].format.fill.solid()
            points[i].format.fill.fore_color.rgb = _rgb(CHART_COLORS[i % len(CHART_COLORS)])
        chart.plots[0].has_data_labels = True
        chart.plots[0].data_labels.font.size = Pt(12)
    else:
        for i, series in enumerate(chart.plots[0].series):
            color = _rgb(CHART_COLORS[i % len(CHART_COLORS)])
            if spec["type"] == "line":
                series.format.line.color.rgb = color
                series.format.line.width = Pt(2.5)
            else:
                series.format.fill.solid()
                series.format.fill.fore_color.rgb = color
        try:
            chart.value_axis.major_gridlines.format.line.color.rgb = _rgb(theme["panel"])
            chart.value_axis.format.line.fill.background()
        except Exception:
            pass
    return frame


def _draw_picture(slide, theme, block, left, top, width, height, near=None):
    from pptx.util import Inches
    path = resolve_image(block["path"], near)
    from PIL import Image
    with Image.open(path) as image:
        ratio = image.width / float(image.height)
    cap = 0.4 if block.get("caption") else 0.0
    box_h = height - cap
    w, h = width, width / ratio
    if h > box_h:
        h, w = box_h, box_h * ratio
    x = left + (width - w) / 2
    slide.shapes.add_picture(str(path), Inches(x), Inches(top), Inches(w), Inches(h))
    if block.get("caption"):
        from pptx.enum.text import PP_ALIGN
        _add_text(slide, left, top + h + 0.05, width, 0.35, block["caption"], 12,
                  theme["muted"], italic=True, align=PP_ALIGN.CENTER)


def validate_images(slides, near=None):
    for slide in slides:
        if slide["image"]:
            resolve_image(slide["image"]["path"], near)


def build_presentation(markdown, title="", theme="dark", footer=None, author="",
                       output=None, near=None):
    """Write a .pptx from Markdown. Returns a summary dict."""
    from pptx import Presentation
    from pptx.enum.text import MSO_ANCHOR, PP_ALIGN
    from pptx.util import Inches, Pt

    colors = THEMES.get(str(theme or "dark").lower(), THEMES["dark"])
    blocks = parse_blocks(markdown)
    slides = group_slides(blocks)
    if not slides:
        raise ValueError("the outline is empty: give each slide a '# Title' and bullets")
    if not title:
        title = slides[0]["title"] or "Presentation"
    validate_images(slides, near)
    # He named the deck but the outline opens straight into content: give it a
    # title slide rather than starting mid-argument.
    first = slides[0]
    if title and (first["bullets"] or first["table"] or first["chart"]
                  or first["image"] or first["quote"] or first["code"]):
        slides.insert(0, {"title": title, "subtitle": author, "bullets": [],
                          "table": None, "image": None, "chart": None, "quote": "",
                          "notes": [], "paras": [], "code": ""})

    # An outline that is only bullets under no title still makes a deck: the
    # first slide is the title slide, the rest follow.
    expanded = []
    for position, slide in enumerate(slides):
        is_title = position == 0 and not (slide["bullets"] or slide["table"]
                                          or slide["chart"] or slide["image"])
        if is_title:
            expanded.append(slide)
        else:
            expanded.extend(_expand_for_fit(slide))

    prs = Presentation()
    prs.slide_width, prs.slide_height = Inches(SLIDE_W), Inches(SLIDE_H)
    blank = prs.slide_layouts[6]
    if footer is None:
        footer = title
    if author:
        prs.core_properties.author = author
    prs.core_properties.title = title

    for number, spec in enumerate(expanded, 1):
        slide = prs.slides.add_slide(blank)
        _set_background(slide, colors["bg"])
        content = (spec["bullets"] or spec["table"] or spec["chart"]
                   or spec["image"] or spec["quote"] or spec["code"])
        if number == 1 and not content:
            _add_rect(slide, MARGIN, 3.05, 1.4, 0.09, colors["accent"])
            _add_text(slide, MARGIN, 1.2, SLIDE_W - 2 * MARGIN, 1.8, spec["title"] or title,
                      48 if len(spec["title"] or title) <= 40 else 36, colors["title"],
                      bold=True, anchor=MSO_ANCHOR.BOTTOM)
            sub = spec["subtitle"] or (author and f"{author}") or ""
            if sub:
                _add_text(slide, MARGIN, 3.35, SLIDE_W - 2 * MARGIN, 1.2, sub, 22, colors["muted"])
        elif not content:
            _add_rect(slide, 0, 0, 0.35, SLIDE_H, colors["accent"])
            _add_text(slide, MARGIN + 0.4, 2.4, SLIDE_W - 2 * MARGIN - 0.4, 1.8,
                      spec["title"], 44, colors["title"], bold=True, anchor=MSO_ANCHOR.MIDDLE)
            if spec["subtitle"]:
                _add_text(slide, MARGIN + 0.4, 4.3, SLIDE_W - 2 * MARGIN - 0.4, 1.0,
                          spec["subtitle"], 20, colors["muted"])
            _draw_footer(slide, colors, number, footer)
        else:
            _draw_title(slide, colors, spec["title"], spec["subtitle"])
            top = 2.0 if spec["subtitle"] else 1.75
            height = SLIDE_H - top - 0.85
            full_w = SLIDE_W - 2 * MARGIN
            side = spec["image"] or spec["chart"]
            if spec["table"]:
                _draw_table(slide, colors, spec["table"], MARGIN, top, full_w, height)
                if spec["bullets"]:
                    # Takeaways under a table, kept short.
                    pass
            elif spec["quote"]:
                _add_text(slide, MARGIN + 0.6, top + 0.3, full_w - 1.2, height - 0.6,
                          "\u201c" + spec["quote"] + "\u201d", 32 if len(spec["quote"]) < 140 else 24,
                          colors["title"], italic=True, anchor=MSO_ANCHOR.MIDDLE)
            elif spec["code"]:
                _add_rect(slide, MARGIN, top, full_w, height, colors["panel"])
                box = _add_text(slide, MARGIN + 0.25, top + 0.2, full_w - 0.5, height - 0.4,
                                spec["code"], 14, colors["text"], font="Consolas")
            elif side and spec["bullets"]:
                text_w = full_w * 0.52
                _draw_bullets(slide, colors, spec["bullets"], MARGIN, top, text_w, height)
                left = MARGIN + text_w + 0.35
                width = full_w - text_w - 0.35
                if spec["chart"]:
                    _draw_chart(slide, colors, spec["chart"], left, top, width, height)
                else:
                    _draw_picture(slide, colors, spec["image"], left, top, width, height, near)
            elif spec["chart"]:
                _draw_chart(slide, colors, spec["chart"], MARGIN, top, full_w, height)
            elif spec["image"]:
                _draw_picture(slide, colors, spec["image"], MARGIN, top, full_w, height, near)
            else:
                _draw_bullets(slide, colors, spec["bullets"], MARGIN, top, full_w, height)
            _draw_footer(slide, colors, number, footer)
        if spec["notes"]:
            slide.notes_slide.notes_text_frame.text = "\n".join(spec["notes"])

    prs.save(str(output))
    return {"path": str(output), "slides": len(expanded), "title": title,
            "theme": str(theme)}


# --------------------------------------------------------------------------- #
# Word documents
# --------------------------------------------------------------------------- #
def _docx_runs(paragraph, text):
    from docx.shared import Pt
    for piece, style in inline_runs(text):
        run = paragraph.add_run(piece)
        run.bold = bool(style.get("bold")) or None
        run.italic = bool(style.get("italic")) or None
        if style.get("code"):
            run.font.name = "Consolas"
            run.font.size = Pt(10)


def _page_number_field(paragraph):
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    run = paragraph.add_run()
    for kind, text in (("begin", None), (None, "PAGE"), ("end", None)):
        if kind:
            element = OxmlElement("w:fldChar")
            element.set(qn("w:fldCharType"), kind)
        else:
            element = OxmlElement("w:instrText")
            element.set(qn("xml:space"), "preserve")
            element.text = text
        run._r.append(element)


def build_document(markdown, title="", author="", output=None, near=None,
                   accent="1F4E79"):
    """Write a .docx from Markdown. Returns a summary dict."""
    from docx import Document
    from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_BREAK
    from docx.shared import Inches, Pt, RGBColor

    blocks = parse_blocks(markdown)
    if not blocks and not title:
        raise ValueError("the document is empty")
    for block in blocks:
        if block["kind"] == "image":
            resolve_image(block["path"], near)

    doc = Document()
    section = doc.sections[0]
    section.left_margin = section.right_margin = Inches(1)
    section.top_margin = section.bottom_margin = Inches(1)
    normal = doc.styles["Normal"]
    normal.font.name = "Calibri"
    normal.font.size = Pt(11)
    for level in range(1, 4):
        style = doc.styles[f"Heading {level}"]
        style.font.name = "Calibri"
        style.font.color.rgb = RGBColor.from_string(accent)
    if author:
        doc.core_properties.author = author

    if not title and blocks and blocks[0]["kind"] == "heading" and blocks[0]["level"] == 1:
        title = blocks[0]["text"]
        blocks = blocks[1:]
    if title:
        doc.core_properties.title = title
        doc.add_heading(title, level=0)
        if author:
            byline = doc.add_paragraph()
            byline.add_run(author).italic = True

    footer = section.footer.paragraphs[0]
    footer.alignment = WD_ALIGN_PARAGRAPH.CENTER
    _page_number_field(footer)

    for block in blocks:
        kind = block["kind"]
        if kind == "heading":
            doc.add_heading(plain(block["text"]), level=min(block["level"], 4))
        elif kind == "para":
            _docx_runs(doc.add_paragraph(), block["text"])
        elif kind == "bullet":
            base = "List Number" if block["ordered"] else "List Bullet"
            name = base + (f" {block['level'] + 1}" if block["level"] else "")
            try:
                paragraph = doc.add_paragraph(style=name)
            except KeyError:
                paragraph = doc.add_paragraph(style=base)
            _docx_runs(paragraph, block["text"])
        elif kind == "table":
            rows = block["rows"]
            table = doc.add_table(rows=len(rows), cols=len(rows[0]))
            table.style = "Light Grid Accent 1"
            for r, row in enumerate(rows):
                for c, value in enumerate(row):
                    cell = table.cell(r, c)
                    cell.text = ""
                    _docx_runs(cell.paragraphs[0], value)
                    if r == 0:
                        for run in cell.paragraphs[0].runs:
                            run.bold = True
            doc.add_paragraph()
        elif kind == "image":
            path = resolve_image(block["path"], near)
            from PIL import Image
            with Image.open(path) as image:
                ratio = image.width / float(image.height)
            width = min(6.0, 6.0)
            doc.add_picture(str(path), width=Inches(width if ratio >= 0.7 else 4.0))
            doc.paragraphs[-1].alignment = WD_ALIGN_PARAGRAPH.CENTER
            if block.get("caption"):
                caption = doc.add_paragraph()
                caption.alignment = WD_ALIGN_PARAGRAPH.CENTER
                run = caption.add_run(block["caption"])
                run.italic = True
                run.font.size = Pt(9)
        elif kind == "quote":
            paragraph = doc.add_paragraph(style="Intense Quote")
            _docx_runs(paragraph, block["text"])
        elif kind == "code":
            paragraph = doc.add_paragraph()
            run = paragraph.add_run(block["text"])
            run.font.name = "Consolas"
            run.font.size = Pt(9.5)
        elif kind == "chart":
            # A chart in a document is a small table of its numbers; the
            # presentation and spreadsheet builders draw real charts.
            spec = block
            header = [""] + [name for name, _ in spec["series"]]
            rows = [header] + [[cat] + [f"{series[1][i]:g}" if i < len(series[1]) else ""
                                        for series in spec["series"]]
                               for i, cat in enumerate(spec["categories"])]
            if spec["title"]:
                doc.add_paragraph().add_run(spec["title"]).bold = True
            table = doc.add_table(rows=len(rows), cols=len(header))
            table.style = "Light Grid Accent 1"
            for r, row in enumerate(rows):
                for c, value in enumerate(row):
                    table.cell(r, c).text = value
        elif kind == "pagebreak":
            doc.add_paragraph().add_run().add_break(WD_BREAK.PAGE)
        elif kind == "notes":
            paragraph = doc.add_paragraph()
            run = paragraph.add_run(block["text"])
            run.italic = True
    doc.save(str(output))
    words = sum(len(b.get("text", "").split()) for b in blocks)
    return {"path": str(output), "words": words, "title": title}


# --------------------------------------------------------------------------- #
# Spreadsheets
# --------------------------------------------------------------------------- #
_NUMBER = re.compile(r"^[-+]?(\d{1,3}(,\d{3})+|\d+)(\.\d+)?$")
_CURRENCY = re.compile(r"^([\u20b9$\u20ac\u00a3])\s*([-+]?[\d,]+(\.\d+)?)$")
_PERCENT = re.compile(r"^([-+]?\d+(\.\d+)?)\s*%$")
_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def typed_cell(value):
    """(python value, number format or None) for a text cell."""
    text = str(value).strip()
    if text == "":
        return None, None
    if text.startswith("="):
        return text, None
    match = _PERCENT.match(text)
    if match:
        return float(match.group(1)) / 100.0, "0.0%" if "." in match.group(1) else "0%"
    match = _CURRENCY.match(text)
    if match:
        number = float(match.group(2).replace(",", ""))
        symbol = match.group(1)
        decimals = "0.00" if "." in match.group(2) else "#,##0"
        return number, f'"{symbol}"{decimals if decimals != "0.00" else "#,##0.00"}'
    if _NUMBER.match(text):
        number = float(text.replace(",", ""))
        if number.is_integer() and "." not in text:
            return int(number), ("#,##0" if "," in text or abs(number) >= 10000 else "0")
        return number, "#,##0.00" if "," in text else "0.00"
    if _ISO_DATE.match(text):
        import datetime
        try:
            return datetime.date.fromisoformat(text), "yyyy-mm-dd"
        except ValueError:
            pass
    return text, None


def parse_sheets(text):
    """[(sheet name, rows, directives)] from Markdown/CSV-ish text."""
    sheets, current = [], None
    lines = str(text or "").replace("\r\n", "\n").split("\n")
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        heading = re.match(r"^#{1,6}\s+(.*)$", stripped)
        if heading:
            current = {"name": heading.group(1).strip(), "rows": [], "charts": [],
                       "totals": []}
            sheets.append(current)
            continue
        if current is None:
            current = {"name": "Sheet1", "rows": [], "charts": [], "totals": []}
            sheets.append(current)
        lowered = stripped.lower()
        if lowered.startswith("chart:"):
            current["charts"].append(stripped.split(":", 1)[1].strip())
            continue
        if lowered.startswith("total:"):
            current["totals"] = [c.strip() for c in stripped.split(":", 1)[1].split(",")
                                 if c.strip()]
            continue
        if stripped.startswith("|"):
            if not _is_separator(stripped):
                current["rows"].append(_split_row(stripped))
        elif "," in stripped or "\t" in stripped:
            import csv
            delimiter = "\t" if "\t" in stripped else ","
            current["rows"].append(next(csv.reader([stripped], delimiter=delimiter)))
    return [s for s in sheets if s["rows"]]


def _safe_sheet_name(name, used):
    cleaned = re.sub(r"[\[\]\*\?/\\:]", "-", name)[:31] or "Sheet"
    base, counter = cleaned, 2
    while cleaned.lower() in used:
        cleaned = (base[:28] + f"-{counter}")
        counter += 1
    used.add(cleaned.lower())
    return cleaned


def build_spreadsheet(text, title="", output=None, accent="1F4E79"):
    """Write an .xlsx from Markdown tables or CSV. Returns a summary dict."""
    from openpyxl import Workbook
    from openpyxl.chart import BarChart, LineChart, PieChart, Reference
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter

    sheets = parse_sheets(text)
    if not sheets:
        raise ValueError("no rows found: give a table (| a | b |) or comma-separated lines")
    workbook = Workbook()
    workbook.remove(workbook.active)
    used = set()
    summary = []
    header_fill = PatternFill("solid", fgColor=accent)
    thin = Side(style="thin", color="D9D9D9")
    for spec in sheets:
        sheet = workbook.create_sheet(_safe_sheet_name(spec["name"], used))
        rows = spec["rows"]
        width = max(len(r) for r in rows)
        rows = [r + [""] * (width - len(r)) for r in rows]
        for r, row in enumerate(rows, 1):
            for c, raw in enumerate(row, 1):
                value, number_format = (raw, None) if r == 1 else typed_cell(raw)
                cell = sheet.cell(row=r, column=c, value=value)
                if number_format:
                    cell.number_format = number_format
                cell.border = Border(top=thin, bottom=thin, left=thin, right=thin)
                if r == 1:
                    cell.font = Font(bold=True, color="FFFFFF")
                    cell.fill = header_fill
                    cell.alignment = Alignment(horizontal="center", vertical="center",
                                               wrap_text=True)
        last = len(rows)
        if spec["totals"]:
            header = [str(h).strip().lower() for h in rows[0]]
            sheet.cell(row=last + 1, column=1, value="Total").font = Font(bold=True)
            for name in spec["totals"]:
                if name.strip().lower() in header:
                    col = header.index(name.strip().lower()) + 1
                    letter = get_column_letter(col)
                    cell = sheet.cell(row=last + 1, column=col,
                                      value=f"=SUM({letter}2:{letter}{last})")
                    cell.font = Font(bold=True)
                    source = sheet.cell(row=2, column=col)
                    if source.number_format:
                        cell.number_format = source.number_format
        sheet.freeze_panes = "A2"
        sheet.auto_filter.ref = f"A1:{get_column_letter(width)}{last}"
        for c in range(1, width + 1):
            longest = max(len(str(sheet.cell(row=r, column=c).value or ""))
                          for r in range(1, last + 1))
            sheet.column_dimensions[get_column_letter(c)].width = min(60, max(10, longest + 3))
        anchor_row = last + (4 if spec["totals"] else 3)
        for chart_spec in spec["charts"]:
            _add_sheet_chart(sheet, rows, chart_spec, anchor_row, last,
                             BarChart, LineChart, PieChart, Reference)
            anchor_row += 18
        summary.append({"sheet": sheet.title, "rows": last - 1, "columns": width})
    workbook.properties.title = title or sheets[0]["name"]
    workbook.save(str(output))
    return {"path": str(output), "sheets": summary, "title": title}


def _add_sheet_chart(sheet, rows, spec, anchor_row, last, BarChart, LineChart, PieChart,
                     Reference):
    """chart: column "Title" x=Month y=Revenue,Cost  (names are header cells)."""
    kind = (spec.split(None, 1)[0].lower() if spec else "column")
    title_match = re.search(r'"([^"]*)"', spec)
    title = title_match.group(1) if title_match else ""
    x_match = re.search(r"\bx=([^\s]+(?:\s+(?!y=)[^\s=]+)*)", spec)
    y_match = re.search(r"\by=(.+)$", spec)
    header = [str(h).strip().lower() for h in rows[0]]

    def column_of(name):
        return header.index(name.strip().lower()) + 1 if name.strip().lower() in header else None

    x_col = column_of(x_match.group(1)) if x_match else 1
    y_names = [n for n in (y_match.group(1).split(",") if y_match else []) if n.strip()]
    y_cols = [column_of(n) for n in y_names if column_of(n)]
    if not y_cols:
        y_cols = [c for c in range(1, len(header) + 1) if c != x_col][:3]
    if kind == "pie":
        chart = PieChart()
    elif kind == "line":
        chart = LineChart()
    else:
        chart = BarChart()
        chart.type = "bar" if kind == "bar" else "col"
    chart.title = title or None
    chart.height, chart.width = 8.5, 16
    for col in y_cols:
        data = Reference(sheet, min_col=col, min_row=1, max_row=last)
        chart.add_data(data, titles_from_data=True)
    chart.set_categories(Reference(sheet, min_col=x_col or 1, min_row=2, max_row=last))
    sheet.add_chart(chart, f"A{anchor_row}")


# --------------------------------------------------------------------------- #
# PDF
# --------------------------------------------------------------------------- #
_PDF_CSS = """
body { font-family: sans-serif; font-size: 11pt; line-height: 1.45; color: #1e293b; }
h1 { font-size: 24pt; color: #0f172a; margin-bottom: 4pt; }
h2 { font-size: 17pt; color: #1f4e79; margin-top: 14pt; margin-bottom: 3pt; }
h3 { font-size: 13pt; color: #1f4e79; margin-top: 10pt; margin-bottom: 2pt; }
p { margin: 0 0 7pt 0; }
li { margin-bottom: 3pt; }
table { border-collapse: collapse; width: 100%; margin: 6pt 0 10pt 0; }
th { background-color: #1f4e79; color: white; padding: 4pt 6pt; text-align: left; }
td { border: 1px solid #cbd5e1; padding: 4pt 6pt; }
blockquote { border-left: 3px solid #1f4e79; margin-left: 0; padding-left: 10pt; color: #475569; font-style: italic; }
pre { background-color: #f1f5f9; padding: 6pt; font-size: 9pt; }
img { max-width: 100%; }
"""


def _html_inline(text):
    import html
    out = []
    for piece, style in inline_runs(text):
        piece = html.escape(piece)
        if style.get("bold"):
            piece = f"<b>{piece}</b>"
        if style.get("italic"):
            piece = f"<i>{piece}</i>"
        if style.get("code"):
            piece = f"<code>{piece}</code>"
        out.append(piece)
    return "".join(out)


def blocks_to_html(blocks, title="", near=None):
    import html
    out, stack = [], []

    def close_lists(to=0):
        while len(stack) > to:
            out.append(f"</{stack.pop()}>")

    if title:
        out.append(f"<h1>{html.escape(title)}</h1>")
    for block in blocks:
        kind = block["kind"]
        if kind != "bullet":
            close_lists()
        if kind == "heading":
            out.append(f"<h{min(block['level'] + (1 if title else 0), 4)}>"
                       f"{_html_inline(block['text'])}</h{min(block['level'] + (1 if title else 0), 4)}>")
        elif kind == "para":
            out.append(f"<p>{_html_inline(block['text'])}</p>")
        elif kind == "bullet":
            tag = "ol" if block["ordered"] else "ul"
            depth = block["level"] + 1
            while len(stack) < depth:
                out.append(f"<{tag}>")
                stack.append(tag)
            close_lists(depth)
            out.append(f"<li>{_html_inline(block['text'])}</li>")
        elif kind == "table":
            rows = block["rows"]
            out.append("<table><tr>" + "".join(f"<th>{_html_inline(c)}</th>" for c in rows[0])
                       + "</tr>")
            for row in rows[1:]:
                out.append("<tr>" + "".join(f"<td>{_html_inline(c)}</td>" for c in row) + "</tr>")
            out.append("</table>")
        elif kind == "image":
            path = resolve_image(block["path"], near)
            out.append(f'<p><img src="{path.name}" width="420"/></p>')
            if block.get("caption"):
                out.append(f"<p><i>{html.escape(block['caption'])}</i></p>")
        elif kind == "quote":
            out.append(f"<blockquote>{_html_inline(block['text'])}</blockquote>")
        elif kind == "code":
            out.append(f"<pre>{html.escape(block['text'])}</pre>")
        elif kind == "chart":
            header = "".join(f"<th>{html.escape(n)}</th>" for n in [""] + [s[0] for s in block["series"]])
            rows = "".join(
                "<tr><td>" + html.escape(cat) + "</td>" + "".join(
                    f"<td>{s[1][i]:g}</td>" if i < len(s[1]) else "<td></td>"
                    for s in block["series"]) + "</tr>"
                for i, cat in enumerate(block["categories"]))
            out.append((f"<p><b>{html.escape(block['title'])}</b></p>" if block["title"] else "")
                       + f"<table><tr>{header}</tr>{rows}</table>")
        elif kind == "notes":
            out.append(f"<p><i>{_html_inline(block['text'])}</i></p>")
        elif kind == "pagebreak":
            out.append('<p style="page-break-after: always"></p>')
    close_lists()
    return "\n".join(out)


def build_pdf(markdown, title="", output=None, near=None):
    """Write a PDF from Markdown with PyMuPDF's HTML layout. Returns a summary."""
    try:
        import pymupdf as fitz
    except ImportError:
        import fitz
    blocks = parse_blocks(markdown)
    if not blocks and not title:
        raise ValueError("the document is empty")
    if not title and blocks and blocks[0]["kind"] == "heading" and blocks[0]["level"] == 1:
        title, blocks = blocks[0]["text"], blocks[1:]
    html_text = blocks_to_html(blocks, title, near)
    archive_dirs = {str(resolve_image(b["path"], near).parent)
                    for b in blocks if b["kind"] == "image"}
    archive = fitz.Archive()
    for directory in archive_dirs:
        archive.add(directory)
    story = fitz.Story(html=html_text, user_css=_PDF_CSS, archive=archive)
    writer = fitz.DocumentWriter(str(output))
    page_rect = fitz.paper_rect("a4")
    where = page_rect + (54, 54, -54, -54)
    pages, more = 0, 1
    while more:
        device = writer.begin_page(page_rect)
        more, _ = story.place(where)
        story.draw(device)
        writer.end_page()
        pages += 1
        if pages > 400:
            break
    writer.close()
    return {"path": str(output), "pages": pages, "title": title}


# --------------------------------------------------------------------------- #
# Reading
# --------------------------------------------------------------------------- #
_BINARY_EXTENSIONS = {".ppt", ".doc", ".xls", ".zip", ".exe", ".dll", ".png", ".jpg",
                      ".jpeg", ".gif", ".mp3", ".mp4", ".7z", ".rar", ".bin"}


def extract_text(path, max_chars=12000):
    """Text from a pptx, docx, xlsx, pdf or plain file, with its size reported."""
    target = Path(os.path.expandvars(os.path.expanduser(str(path or "").strip().strip('"'))))
    if not target.is_file():
        raise ValueError(f"file not found: {path}")
    suffix = target.suffix.lower()
    parts = []
    if suffix == ".pptx":
        from pptx import Presentation
        prs = Presentation(str(target))
        for number, slide in enumerate(prs.slides, 1):
            lines = []
            for shape in slide.shapes:
                if shape.has_text_frame and shape.text_frame.text.strip():
                    lines.append(shape.text_frame.text.strip())
                if getattr(shape, "has_table", False) and shape.has_table:
                    for row in shape.table.rows:
                        lines.append(" | ".join(cell.text.strip() for cell in row.cells))
            notes = ""
            if slide.has_notes_slide and slide.notes_slide.notes_text_frame.text.strip():
                notes = "\n  Notes: " + slide.notes_slide.notes_text_frame.text.strip()
            parts.append(f"--- Slide {number} ---\n" + "\n".join(lines) + notes)
    elif suffix == ".docx":
        from docx import Document
        doc = Document(str(target))
        for paragraph in doc.paragraphs:
            if paragraph.text.strip():
                prefix = "# " if paragraph.style.name.startswith(("Heading", "Title")) else ""
                parts.append(prefix + paragraph.text.strip())
        for table in doc.tables:
            for row in table.rows:
                parts.append(" | ".join(cell.text.strip() for cell in row.cells))
    elif suffix in (".xlsx", ".xlsm"):
        from openpyxl import load_workbook
        workbook = load_workbook(str(target), data_only=True, read_only=True)
        for sheet in workbook.worksheets:
            parts.append(f"--- Sheet {sheet.title} ---")
            for index, row in enumerate(sheet.iter_rows(values_only=True)):
                if index >= 200:
                    parts.append("... (more rows)")
                    break
                parts.append(" | ".join("" if v is None else str(v) for v in row))
    elif suffix == ".pdf":
        try:
            import pymupdf as fitz
        except ImportError:
            import fitz
        with fitz.open(str(target)) as pdf:
            for number, page in enumerate(pdf, 1):
                parts.append(f"--- Page {number} ---\n" + page.get_text().strip())
                if sum(len(p) for p in parts) > max_chars * 2:
                    break
    else:
        raw = target.read_bytes()
        if b"\x00" in raw[:4096] or suffix in _BINARY_EXTENSIONS:
            raise ValueError(
                f"{target.name} is a {suffix or 'binary'} file I cannot read as text; "
                "supported: pptx, docx, xlsx, pdf and text files")
        parts.append(raw.decode("utf-8", errors="replace"))
    text = "\n".join(parts)
    note = f"\n[Showing the first {max_chars:,} of {len(text):,} characters]" \
        if len(text) > max_chars else ""
    return f"{target.name}:\n{text[:max_chars]}{note}"
