# -*- coding: utf-8 -*-
"""
report_view.py -- giving a briefing a shape, so it can be read instead of faced.

The dashboard received the model's REPORT as raw text and inserted it into a
textbox. Styling existed, but only for one research payload -- it looked for
the literal lines SUMMARY, EVIDENCE and "SOURCES  ." and tagged those. An
ordinary briefing, which is what nearly every report is, matched none of them
and arrived as an undifferentiated wall:

    TODAY
    - RL review: completed earlier today.
    - Evening: dinner at 20:00; wind down and sleep at 22:30.
    TASKS
    - SDP review: tomorrow, 13 October.
    EMAIL
    - "Get hired by Eurogrip -- Grab PPIs and 3L" from Team Unstop...

Every line the same weight, the same colour, the same size. The information was
all there and none of it was findable, and the reaction to opening it was to
close it.

This file finds the structure that is already in the text -- sections, bullets,
labelled values, quantities, links -- and labels each line so the renderer can
give it weight. It draws nothing. That split is deliberate and the same one
used for the screen overlay and the route map: the parsing is where the real
mistakes live (a date mistaken for a label, a bullet swallowing its heading),
and parsing can be tested without a display, which is the only way it gets
tested at all.

WHAT COUNTS AS A HEADING
------------------------
The model is not consistent, and should not have to be. All of these are
headings in reports it has actually produced: "TODAY", "## Tasks",
"**EMAIL**", "Email:" on its own line. A heading is short, has no sentence
punctuation, and is followed by content -- so that is what is tested, rather
than demanding one syntax and silently flattening the rest.
"""

import re

#: A bullet in any of the forms the model writes.
_BULLET = re.compile(r"^\s*(?:[-*•·]|\d{1,2}[.)])\s+(.*)$")
#: "Label: value", but not "19:30" or "https://x" -- a time and a URL both
#: contain a colon and neither is a label.
_LABELLED = re.compile(r"^\s*([A-Za-z][A-Za-z0-9 /'()&-]{1,28}):\s+(\S.*)$")
_MARKDOWN_HEADING = re.compile(r"^\s*#{1,6}\s+(.*)$")
_BOLD_ONLY = re.compile(r"^\s*\*\*(.+?)\*\*:?\s*$")
_RULE = re.compile(r"^\s*([-=_*])\1{2,}\s*$")
_URL = re.compile(r"https?://\S+")
_TABLE_SEPARATOR = re.compile(
    r"^\s*\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|?\s*$")
_FENCE = re.compile(r"^\s*```\s*([A-Za-z0-9_+-]*)\s*$")
#: A quantity worth pulling out: 30C, 14 km/h, 2 days, 9050 steps, Rs 3L.
_QUANTITY = re.compile(
    r"^\s*(?:[₹$]|Rs\.?\s?)?\d[\d,.]*\s*"
    r"(?:%|C|°C|km/h|km|m|kg|hrs?|hours?|mins?|minutes?|days?|weeks?|"
    r"steps|L|k|lpa)?\b", re.IGNORECASE)

MAX_HEADING_WORDS = 6
MAX_HEADING_CHARS = 42


def _strip_markup(text):
    """Remove the markdown the renderer does not need."""
    cleaned = str(text or "")
    cleaned = re.sub(r"\*\*(.+?)\*\*", r"\1", cleaned)
    cleaned = re.sub(r"(?<!\w)\*(.+?)\*(?!\w)", r"\1", cleaned)
    cleaned = re.sub(r"`(.+?)`", r"\1", cleaned)
    return cleaned.strip()


def _looks_like_heading(line, next_line):
    """Whether this line introduces what follows rather than saying something.

    Length and punctuation do the work. A heading is short, does not end a
    sentence, and has something under it -- "TODAY" above three bullets is a
    heading; "Chennai is clear at 30C." is not, however short.
    """
    text = _strip_markup(line)
    if not text or len(text) > MAX_HEADING_CHARS:
        return False
    if len(text.split()) > MAX_HEADING_WORDS:
        return False
    if text.endswith((".", "!", "?", ",", ";")):
        return False
    if _BULLET.match(line) or _URL.search(text):
        return False
    # Nothing under it is a one-line paragraph, not a section.
    if not str(next_line or "").strip():
        return False
    # Either shouting, or title-like, or explicitly marked.
    if text.isupper():
        return True
    if text.endswith(":"):
        return True
    return False


def parse(text):
    """Typed blocks for a report. Each is a dict with a 'kind'.

    Kinds: heading, bullet, labelled (label/value), text, link, rule, spacer.
    """
    raw_lines = str(text or "").replace("\r\n", "\n").split("\n")
    blocks = []
    index = 0
    while index < len(raw_lines):
        raw = raw_lines[index]
        line = raw.rstrip()
        following = next((candidate for candidate in raw_lines[index + 1:]
                          if candidate.strip()), "")

        if line.strip().startswith("$$"):
            stripped_math = line.strip()
            if stripped_math.endswith("$$") and len(stripped_math) > 4:
                formula = stripped_math[2:-2].strip()
                index += 1
            else:
                body = [stripped_math[2:]]
                index += 1
                while index < len(raw_lines):
                    candidate = raw_lines[index]
                    if candidate.rstrip().endswith("$$"):
                        body.append(candidate.rstrip()[:-2])
                        index += 1
                        break
                    body.append(candidate)
                    index += 1
                formula = "\n".join(body).strip()
            blocks.append({"kind": "math", "text": formula})
            continue

        # Markdown tables are one visual object, not a row of unrelated text.
        # Recognise the header/separator contract and keep all cells intact for
        # the UI renderer. Ragged rows are padded instead of discarded.
        if ("|" in line and index + 1 < len(raw_lines)
                and _TABLE_SEPARATOR.match(raw_lines[index + 1])):
            def cells(value):
                value = value.strip().strip("|")
                return [_strip_markup(cell.strip()) for cell in value.split("|")]

            headers = cells(line)
            rows = []
            index += 2
            while index < len(raw_lines):
                candidate = raw_lines[index]
                if "|" not in candidate or not candidate.strip():
                    break
                row = cells(candidate)
                row = (row + [""] * len(headers))[:len(headers)]
                rows.append(row)
                index += 1
            blocks.append({"kind": "table", "headers": headers, "rows": rows})
            continue

        # Code is useful as code. Mermaid is promoted to a diagram block so
        # the result window can draw it rather than showing its source.
        fence = _FENCE.match(line)
        if fence:
            language = fence.group(1).lower()
            body = []
            index += 1
            while index < len(raw_lines) and not _FENCE.match(raw_lines[index]):
                body.append(raw_lines[index])
                index += 1
            if index < len(raw_lines):
                index += 1
            block_kind = ("diagram" if language == "mermaid" else
                          "math" if language in {"latex", "math", "tex"} else
                          "code")
            blocks.append({"kind": block_kind, "language": language,
                           "text": "\n".join(body).strip()})
            continue

        if not line.strip():
            # Runs of blank lines collapse: the model emits them freely and
            # they are what makes a report feel longer than it is.
            if blocks and blocks[-1]["kind"] != "spacer":
                blocks.append({"kind": "spacer"})
            index += 1
            continue

        if _RULE.match(line):
            blocks.append({"kind": "rule"})
            index += 1
            continue

        heading = _MARKDOWN_HEADING.match(line) or _BOLD_ONLY.match(line)
        if heading:
            blocks.append({"kind": "heading",
                           "text": _strip_markup(heading.group(1)).rstrip(":")})
            index += 1
            continue

        if _looks_like_heading(line, following):
            blocks.append({"kind": "heading",
                           "text": _strip_markup(line).rstrip(":")})
            index += 1
            continue

        bullet = _BULLET.match(line)
        if bullet:
            body = _strip_markup(bullet.group(1))
            labelled = _LABELLED.match(body)
            if labelled:
                blocks.append({"kind": "bullet",
                               "label": labelled.group(1).strip(),
                               "text": labelled.group(2).strip(),
                               "quantity": bool(_QUANTITY.match(labelled.group(2)))})
            else:
                blocks.append({"kind": "bullet", "text": body})
            index += 1
            continue

        stripped = _strip_markup(line)
        if stripped.startswith(("http://", "https://")) and " " not in stripped:
            blocks.append({"kind": "link", "text": stripped})
            index += 1
            continue

        labelled = _LABELLED.match(stripped)
        if labelled:
            blocks.append({"kind": "labelled",
                           "label": labelled.group(1).strip(),
                           "text": labelled.group(2).strip(),
                           "quantity": bool(_QUANTITY.match(labelled.group(2)))})
            index += 1
            continue

        blocks.append({"kind": "text", "text": stripped})
        index += 1

    while blocks and blocks[-1]["kind"] == "spacer":
        blocks.pop()
    return blocks


def is_plain_prose(blocks):
    """Whether this has no structure worth drawing.

    A two-sentence answer should not be dressed up as a briefing; styling it
    into sections would be noise pretending to be organisation.
    """
    meaningful = [block for block in blocks if block["kind"] != "spacer"]
    if not meaningful:
        return True
    structured = sum(1 for block in meaningful
                     if block["kind"] in ("heading", "bullet", "labelled",
                                          "link", "rule", "table", "diagram",
                                          "code", "math"))
    return structured == 0


def outline(blocks):
    """Section names in order, for a summary line or a jump list."""
    return [block["text"] for block in blocks if block["kind"] == "heading"]


def counts(blocks):
    """How many of each kind, for deciding whether to open the panel at all."""
    tally = {}
    for block in blocks:
        tally[block["kind"]] = tally.get(block["kind"], 0) + 1
    return tally
