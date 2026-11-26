"""Throttled, draft-only previews of explicit REPORT output; never tool execution."""
import re
import time
import uuid
import json

_TOKENS = re.compile(r'^\s*```[^\n]*(?:\n|$)|`[^`\n]+`|<\s*(/?)\s*(REPORT|TUTOR|think|ACTION|SAY|ASK)\b[^>]*>', re.I | re.M)


def report_prefix(text):
    """Extract only a top-level report, excluding reasoning, actions and examples."""
    fence, region, start, end_tag = False, None, None, None

    def content(raw):
        if raw.lstrip().startswith('{'):
            try:
                from report_html import blocks_from_payload
                return blocks_from_payload(json.loads(raw))[0]
            except (ValueError, TypeError):
                return ''
        return raw
    for token in _TOKENS.finditer(text):
        value = token.group(0).strip()
        if value.startswith('```'):
            fence = not fence
            continue
        if fence or value.startswith('`'):
            continue
        closing, name = token.group(1), token.group(2).lower()
        if name == 'tutor':
            name = 'report'
        if region:
            if region == 'report' and end_tag == 'action' and name == 'report':
                if closing:
                    return content(text[start:token.start()]), True
                start = token.end()
                continue
            if name == (end_tag or region) and closing:
                if region == 'report':
                    return content(text[start:token.start()]), True
                region = None
            continue
        if name == 'action' and not closing:
            from action_attrs import parse
            attrs = parse(value)
            if (str(attrs.get('type', '')).upper() != 'RESULT_WINDOW'
                    or str(attrs.get('action', 'show')).lower() not in {'show', 'open', 'active', 'replace'}):
                return '', False
            region, start, end_tag = 'report', token.end(), 'action'
            continue
        if not closing:
            region = name
            if name == 'report':
                start = token.end()
    if region == 'report':
        # A protocol close may arrive half a tag at a time.
        return content(re.sub(r'<[^>\n]*$', '', text[start:])), False
    return '', False


def preview_markdown(text):
    """Keep streaming code visible, but don't attempt half a JSON visual or equation."""
    opening = None
    for match in re.finditer(r'^\s*```([^\n]*)', text, re.M):
        if opening is None:
            opening = match
        else:
            opening = None
    if opening and opening.group(1).strip().split(' ', 1)[0].lower() in {
            'scene3d', 'model3d', 'image', 'animation', 'mermaid', 'diagram',
            'chart', 'math', 'latex', 'tex', 'questionnaire', 'explorer', 'lesson', 'walkthrough'}:
        text = text[:opening.start()]
    # Only standalone display fences count; $$ inside code is not mathematics.
    lines, in_code, math_start = text.splitlines(keepends=True), False, None
    offset = 0
    for line in lines:
        if line.strip().startswith('```'):
            in_code = not in_code
        if not in_code and line.strip().startswith('$$'):
            if line.strip() == '$$':
                math_start = offset if math_start is None else None
            elif not line.strip().endswith('$$'):
                math_start = offset
        offset += len(line)
    return (text[:math_start] if math_start is not None else text).strip()


class ReportStream:
    def __init__(self, emit, cancelled=lambda: False, clock=time.monotonic):
        self.emit, self.cancelled, self.clock = emit, cancelled, clock
        self.id = uuid.uuid4().hex
        self.chunks, self.size, self.last_at, self.last_text = [], 0, -1e9, ''
        self.closed = False

    def reset(self):
        if self.last_text:
            self._send(self.last_text, 'interrupted')
        self.chunks, self.size, self.last_text, self.last_at = [], 0, '', -1e9
        self.id = uuid.uuid4().hex

    def _send(self, content, phase):
        self.emit(('report_stream', {'stream_id': self.id, 'content': content,
                                    'title': 'VANTA Report', 'phase': phase}))

    def feed(self, delta):
        if self.closed or self.cancelled() or self.size > 1_000_000:
            return
        delta = str(delta or '')
        self.chunks.append(delta)
        self.size += len(delta)
        if self.clock() - self.last_at >= .4:
            self.flush()

    def flush(self):
        self.last_at = self.clock()
        text, _ = report_prefix(''.join(self.chunks))
        text = preview_markdown(text)
        from result_presenter import display_worthy
        if not any(line.strip() and not line.lstrip().startswith(('#', '```')) for line in text.splitlines()):
            text = ''
        if text and not display_worthy(text):
            text = ''
        from report_html import validate_scenes
        if text and validate_scenes(text):
            return  # Hold invalid geometry for the final-response repair path.
        if text and text != self.last_text and not self.cancelled():
            self._send(text, 'writing')
            self.last_text = text

    def finish(self, final_response):
        # Provider retries may have emitted abandoned drafts. Reconcile against
        # the returned response, which is authoritative, not concatenated deltas.
        self.chunks = [str(final_response or '')]
        self.flush()
        if self.last_text:
            content, complete = report_prefix(str(final_response or ''))
            from action_protocol import extract
            outside_examples = re.sub(r'```.*?(?:```|$)|`[^`\n]+`|<think>.*?</think>', '',
                                      str(final_response or ''), flags=re.S | re.I)
            from action_attrs import parse
            complete = complete and not any(
                str(parse(e.attributes).get('type', '')).upper() != 'RESULT_WINDOW'
                for e in extract(outside_examples))
            from report_html import validate_scenes
            complete = complete and not validate_scenes(content)
            phase = 'awaiting_review' if complete and not self.cancelled() else 'interrupted'
            self._send(self.last_text, phase)
        self.closed = True
