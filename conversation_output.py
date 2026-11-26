"""Pure response rendering rules shared by speech and dashboard output."""
import re

SPOKEN_WORD_LIMIT = 45

_RAW_URL = re.compile(r'(?:https?://|www\.)[^\s<>"\]]+', re.IGNORECASE)
_EMAIL = re.compile(r'(?<![\w.+-])[\w.+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}(?![\w.-])')
_QUOTED_WINDOWS_PATH = re.compile(r'(["\'])([A-Za-z]:\\[^"\'\r\n]+)\1')
_WINDOWS_PATH = re.compile(r'(?<!\w)[A-Za-z]:\\(?:[^\s<>:"|?*]+\\)*[^\s<>:"|?*]+')
_PHONE = re.compile(r'(?<!\w)(?:\+\d{1,3}[ -]?)?(?:\d[ -]?){9,12}\d(?!\w)')
_LONG_IDENTIFIER = re.compile(r'\b(?=[A-Za-z0-9_-]{32,}\b)(?=[A-Za-z0-9_-]*\d)[A-Za-z0-9_-]+\b')


def split_speech_artifacts(value):
    """Separate machine-readable details from natural speech.

    The exact value remains visible in a dedicated Details card while TTS gets
    a short human reference.  This is applied inside ``speak`` as well as at
    final-response rendering, so streaming cannot leak a URL before the final
    response has a chance to sanitize it.
    """
    text = str(value or "")
    artifacts = []

    def keep(kind, raw, replacement):
        cleaned = str(raw or "").rstrip(".,;:!?)")
        if cleaned and not any(item["value"] == cleaned for item in artifacts):
            artifacts.append({"kind": kind, "value": cleaned})
        suffix = str(raw or "")[len(cleaned):]
        return replacement + suffix

    # Markdown links keep their readable label in speech and send only the
    # destination to Details.
    def markdown(match):
        label, url = match.group(1), match.group(2)
        keep("link", url, "")
        return label

    text = re.sub(r'\[([^\]]+)\]\((https?://[^)]+)\)', markdown, text,
                  flags=re.IGNORECASE)
    text = _RAW_URL.sub(lambda m: keep("link", m.group(0),
                                      "the link shown in Details"), text)
    text = _EMAIL.sub(lambda m: keep("email", m.group(0),
                                    "the email address shown in Details"), text)
    text = _QUOTED_WINDOWS_PATH.sub(
        lambda m: keep("path", m.group(2), "the file path shown in Details"),
        text)
    text = _WINDOWS_PATH.sub(lambda m: keep("path", m.group(0),
                                           "the file path shown in Details"), text)
    text = _PHONE.sub(lambda m: keep("phone", m.group(0),
                                    "the phone number shown in Details"), text)
    text = _LONG_IDENTIFIER.sub(lambda m: keep("identifier", m.group(0),
                                              "the identifier shown in Details"), text)
    text = re.sub(r'\s+', ' ', text).strip()
    return text, artifacts


_VALID_PROTOCOL_OPEN = re.compile(
    r'<\s*(?:ACTION(?:\s|>)|SAY>|REPORT>|ASK(?:\s|>)|DONE(?:\s|/|>)|'
    r'tool_call(?:\s|>)|function=)',
    re.IGNORECASE)


def malformed_protocol_response(value):
    """Whether model scratch markup would otherwise turn into silence.

    Plain conversational prose is valid. Text beginning like a tag is valid
    only when it is one of VANTA's executable/output protocol forms. This
    catches fragments such as ``<action? no, use exact XML>`` and ``< S? >``
    without trying to enumerate every sentence a model may use around them.
    """
    text = str(value or '').lstrip()
    return bool(text.startswith('<') and not _VALID_PROTOCOL_OPEN.search(text))


#: Phrases that only appear when a model is deliberating about the protocol
#: rather than speaking it. Drawn from real transcripts: the model talks about
#: which "channel" its output belongs in, whether it may act "in final", and
#: what it ought to "emit".
_REASONING_TELLS = re.compile(
    r"\b(?:in\s+final|final\s+channel|commentary\s+channel|action\s+channel"
    r"|need\s+(?:to\s+)?(?:emit|output|use|produce)|must\s+emit"
    r"|let'?s\s+(?:do|emit|try)|exact\s+xml|need\s+xml"
    r"|already\s+emitted|no\s+receipt\s+yet)\b", re.IGNORECASE)

#: Protocol tags written with spaces between the letters -- "< S A Y >" --
#: which are the model losing the shape of the tag rather than using it.
_SPACED_TAG = re.compile(r"<\s*(?:[A-Za-z]\s+){2,}[A-Za-z]\s*>")


def is_reasoning_leak(value):
    """Whether this is a model thinking out loud instead of responding.

    Separate from malformed_protocol_response, which asks whether the text
    *starts* like a broken tag. This asks whether it reads like deliberation,
    which is how the damaging cases presented: a half-written tag followed by
    a paragraph weighing up which channel to put it in.
    """
    text = " ".join(str(value or "").split())
    if not text:
        return False
    if _SPACED_TAG.search(text):
        return True
    # The damaging case opened with something that *looks* like a valid tag --
    # "< ACTION type...>" -- so a check on how the text starts reported it as
    # fine. What marks it is deliberation with nothing executable in it: no
    # self-closing tag and no closing tag anywhere.
    complete_tag = re.search(r"/\s*>|</\s*[A-Za-z]+\s*>", text)
    if _REASONING_TELLS.search(text) and not complete_tag:
        return True
    # A broken opening tag on its own is enough, when it carries the hesitation
    # that comes with it.
    if malformed_protocol_response(text):
        return bool(re.search(r"\?|\bcannot\b|\bcan't\b", text))
    return False


def quarantine_leak(value):
    """What goes into the conversation in place of leaked reasoning.

    The leak itself must not go back, which is the whole point. Appending it
    verbatim meant every later call in the turn re-sent it, it accumulated,
    and the provider eventually refused the request as a policy violation --
    losing a turn in which a project had already been created. None of that
    text was worth keeping: a model does not need to re-read its own
    malformed output in order to produce a good one.
    """
    return ("(no executable response was produced on that step; the internal "
            "draft was discarded)")


def naturalize_user_framing(value):
    """Hide internal third-person prompt framing from user-facing output."""
    text = str(value or "")
    replacements = (
        (r"\bthe user is\b", "you are"),
        (r"\bthe user has\b", "you have"),
        (r"\bthe user wants\b", "you want"),
        (r"\bthe user asks\b", "you ask"),
        (r"\bthe user's\b", "your"),
        (r"\bthe user\b", "you"),
    )
    for pattern, replacement in replacements:
        text = re.sub(pattern, replacement, text, flags=re.IGNORECASE)
    text = re.sub(r"(^|[.!?]\s+)(you|your)\b", lambda m: m.group(1) + m.group(2).capitalize(), text, flags=re.IGNORECASE)
    return text

def _clean_markup(value):
    value=re.sub(r'<think>.*?</think>','',value or '',flags=re.DOTALL|re.IGNORECASE)
    value=re.sub(r'<ACTION\s+[^>]*/>','',value,flags=re.DOTALL|re.IGNORECASE)
    value=re.sub(r'<ACTION\s+[^>]*>.*?</ACTION>','',value,flags=re.DOTALL|re.IGNORECASE)
    # Truncated model output may never close its tool tag; remove it to EOF.
    value=re.sub(r'<ACTION\b.*$','',value,flags=re.DOTALL|re.IGNORECASE)
    value=re.sub(r'<tool_call\b.*$','',value,flags=re.DOTALL|re.IGNORECASE)
    value=re.sub(r'<function=[A-Z_]+\b.*$','',value,flags=re.DOTALL|re.IGNORECASE)
    value=re.sub(r'<(?:DONE|ASK)\b[^>]*>.*?</(?:DONE|ASK)>','',value,flags=re.DOTALL|re.IGNORECASE)
    value=re.sub(r'<DONE\s*/>','',value,flags=re.IGNORECASE)
    value=re.sub(r'```(?:json)?|```','',value,flags=re.IGNORECASE)
    return naturalize_user_framing(re.sub(r'\n{3,}','\n\n',value).strip())


def _clean_report_markup(value):
    """Remove VANTA protocol noise while preserving report Markdown.

    Reports intentionally use fenced Mermaid/LaTeX blocks.  The ordinary
    conversational cleaner removes code fences so they are not spoken aloud;
    applying it to reports silently turned diagrams into plain text before the
    result renderer saw them.
    """
    value = re.sub(r'<think>.*?</think>', '', value or '',
                   flags=re.DOTALL | re.IGNORECASE)
    value = re.sub(r'<ACTION\s+[^>]*/>', '', value,
                   flags=re.DOTALL | re.IGNORECASE)
    value = re.sub(r'<ACTION\s+[^>]*>.*?</ACTION>', '', value,
                   flags=re.DOTALL | re.IGNORECASE)
    value = re.sub(r'<(?:DONE|ASK)\b[^>]*>.*?</(?:DONE|ASK)>', '', value,
                   flags=re.DOTALL | re.IGNORECASE)
    value = re.sub(r'<DONE\s*/>', '', value, flags=re.IGNORECASE)
    return naturalize_user_framing(
        re.sub(r'\n{3,}', '\n\n', value).strip()
    )


def canonical_response_text(response):
    """Return exactly one answer for both the dashboard and speech.

    Models sometimes emit a complete prose answer and then repeat a shortened
    version inside ``<SAY>``.  The old renderer unwrapped the tag and joined
    both, making VANTA narrate the answer twice.  Plain prose is authoritative;
    a tagged question is retained only when the prose has no question itself.
    """
    source=response or ''
    tagged=[m.strip() for m in re.findall(
        r'<(?:SAY|REPORT)>(.*?)</(?:SAY|REPORT)>',source,
        flags=re.DOTALL|re.IGNORECASE,
    ) if m.strip()]
    plain=re.sub(
        r'<(?:SAY|REPORT)>.*?</(?:SAY|REPORT)>','',source,
        flags=re.DOTALL|re.IGNORECASE,
    )
    plain=_clean_markup(plain)
    if plain:
        if '?' not in plain:
            questions=[item for item in tagged if '?' in item]
            if questions:
                return f"{plain}\n\n{_clean_markup(questions[-1])}".strip()
        return plain

    unique=[]
    seen=set()
    for item in tagged:
        cleaned=_clean_markup(item)
        key=re.sub(r'\W+',' ',cleaned.lower()).strip()
        if cleaned and key not in seen:
            unique.append(cleaned)
            seen.add(key)
    return '\n\n'.join(unique)

def visible_response_text(response):
    return canonical_response_text(response)


def dashboard_response_text(response):
    """Return the detailed display channel, without speech or tool markup."""
    source = re.sub(r'<think>.*?</think>', '', response or '', flags=re.DOTALL | re.IGNORECASE)
    reports = [_clean_report_markup(match.group(2)) for match in re.finditer(
        r'<(REPORT|TUTOR)>(.*?)</\1>', source, flags=re.DOTALL | re.IGNORECASE)]
    reports = [item for item in reports if item]
    if reports:
        return '\n\n'.join(reports)
    # Plain final answers can become reading documents too. Keep their code,
    # diagram fences and formulas; the speech cleaner strips those fences.
    body = _clean_report_markup(re.sub(r'<SAY>.*?</SAY>', '', source,
                                      flags=re.DOTALL | re.IGNORECASE))
    if body:
        if '?' not in body:
            questions = [item for item in re.findall(r'<SAY>(.*?)</SAY>', source,
                          flags=re.DOTALL | re.IGNORECASE) if '?' in item]
            if questions:
                body += '\n\n' + _clean_markup(questions[-1])
        return body
    return canonical_response_text(source)


def report_text(response):
    """Only the REPORT blocks, or '' when there are none.

    dashboard_response_text falls back to the whole cleaned response when no
    REPORT is present, which is right for a conversational turn. An action turn
    needs the opposite: the report if it exists, and otherwise nothing, because
    the rest of that response is tool markup and narration.
    """
    source = re.sub(r'<think>.*?</think>', '', response or '', flags=re.DOTALL | re.IGNORECASE)
    reports = [_clean_report_markup(match.group(2)) for match in re.finditer(
        r'<(REPORT|TUTOR)>(.*?)</\1>', source, flags=re.DOTALL | re.IGNORECASE)]
    return '\n\n'.join(item for item in reports if item)


def wants_report_surface(response, presentation='inline', catalogs=()):
    """Give substantial or visual answers a reading surface.

    TUTOR is a catalog, not an executable action or a separate window. Legacy
    TUTOR envelopes denote report content; a substantial structured lesson is
    also recoverable when the model omits its envelope.
    """
    if report_text(response) or presentation == 'report':
        return True
    content = dashboard_response_text(response)
    # This is a delivery-size contract, not a list of user phrases or topics.
    # Large answers and inherently visual material need a readable surface
    # when the model forgets its REPORT envelope or mislabels them inline.
    if len(content.split()) >= 120:
        return True
    if len(re.findall(r'(?m)^\s*(?:[-*]\s+|\d+[.)]\s+)\S', content)) >= 3:
        return True
    if re.search(r'(?m)^\s*\$\$', content):
        return True
    if re.search(r'(?m)^\s*```(?:mermaid|diagram|chart|scene3d|model3d|animation|questionnaire|explorer|lesson|walkthrough)\b', content):
        return True
    if re.search(r'(?m)^\s*\|.+\|\s*\n\s*\|\s*:?-+', content):
        return True
    return bool('TUTOR' in (catalogs or ()) and len(content.split()) >= 60 and re.search(
        r'(?m)^\s*(?:#{1,6}\s|\*\*.+?\*\*|```|\|.+\||\d+[.)]\s)', content))


def explicit_spoken_text(response):
    """Return only model-authored SAY content, deduplicated in source order."""
    items = []
    seen = set()
    for match in re.findall(r'<SAY>(.*?)</SAY>', response or '', flags=re.DOTALL | re.IGNORECASE):
        cleaned = _clean_markup(match)
        key = re.sub(r'\W+', ' ', cleaned.lower()).strip()
        if cleaned and key not in seen:
            items.append(cleaned)
            seen.add(key)
    return '\n\n'.join(items)


def needs_spoken_summary(text, word_limit=SPOKEN_WORD_LIMIT):
    """Long dashboard prose should be summarized before reaching TTS."""
    return len(re.findall(r"\b[\w'-]+\b", text or '')) > word_limit


def bounded_spoken_fallback(text, word_limit=SPOKEN_WORD_LIMIT):
    """Safe local fallback: never make TTS read an entire long report."""
    clean = _clean_markup(text)
    words = clean.split()
    if len(words) <= word_limit:
        return clean
    clipped = ' '.join(words[:word_limit]).rstrip(' ,;:-')
    return clipped + '.'


def speech_safe_text(value):
    """Convert display-oriented Markdown into natural text for every TTS path."""
    text = speak_dates(str(value or ""))
    # Code blocks are useful on screen and generally useless read aloud.
    text = re.sub(r"```[A-Za-z0-9_+-]*\s*.*?```", " ", text, flags=re.DOTALL)
    text = _clean_markup(text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"&lt;ACTION.*?/&gt;", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"\[ACTION[^\]]+\]", " ", text, flags=re.IGNORECASE)

    # Keep link labels but never narrate Markdown syntax or destinations.
    text = re.sub(r"!\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"^\s{0,3}#{1,6}\s*", "", text, flags=re.MULTILINE)
    text = re.sub(r"^\s{0,3}>\s?", "", text, flags=re.MULTILINE)
    text = re.sub(r"^\s*[-+*]\s+", "", text, flags=re.MULTILINE)
    text = re.sub(r"^\s*(?:_{3,}|-{3,}|\*{3,})\s*$", "", text,
                  flags=re.MULTILINE)

    # Preserve the words inside emphasis and inline code, not their wrappers.
    text = re.sub(r"(\*\*|__)(.+?)\1", r"\2", text, flags=re.DOTALL)
    text = re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)", r"\1", text)
    text = re.sub(r"(?<!_)_([^_\n]+)_(?!_)", r"\1", text)
    text = re.sub(r"`([^`]+)`", r"\1", text)

    # A real multiplication sign should be spoken naturally; every remaining
    # asterisk is formatting debris and is removed.
    text = re.sub(r"(?<=\w)\s*\*\s*(?=\w)", " times ", text)
    text = text.replace("*", " ").replace("|", ", ")
    text = text.replace("_", " ")
    text = re.sub(r"\s+", " ", text).strip()
    return text

def action_turn_speech_text(response):
    """Return only pre-action SAY text; never tool syntax or success claims."""
    source=response or ''
    marker=re.search(r'<(?:ACTION\b|tool_call\b|function=[A-Z_]+\b)',source,re.IGNORECASE)
    prefix=source[:marker.start()] if marker else source
    items=[]; seen=set()
    for match in re.findall(r'<SAY>(.*?)</SAY>',prefix,flags=re.DOTALL|re.IGNORECASE):
        cleaned=_clean_markup(match)
        key=re.sub(r'\W+',' ',cleaned.lower()).strip()
        if cleaned and key not in seen:
            items.append(cleaned); seen.add(key)
    return '\n\n'.join(items)

def narrates_unexecuted_action(response):
    visible=visible_response_text(response)
    # Examples and quoted instructions are not claims that VANTA performed
    # them. Keep first-person promises outside quotes subject to the guard.
    visible = re.sub(r'"[^"\n]*"|(?<!\w)\'[^\'\n]+\'(?!\w)|`[^`\n]+`', '', visible)
    action_promise = re.search(
        r"\b(?:i am|i'm|i will|let me|i'll|now)\s+(?:now\s+)?"
        r"(?:fetch|retrieve|read|check|search|open|create|execute|proceed|investigate|"
        r"examine|analyze|review|research|inspect|process|triage|summarize|prioritize|follow up|look into|"
        r"fetching|retrieving|reading|checking|searching|opening|creating|executing|"
        r"proceeding|investigating|examining|analyzing|reviewing|researching|inspecting|processing|triaging|summarizing|prioritizing|"
        r"following up|looking up|looking into)\b",
        visible,re.IGNORECASE,
    )
    memory_promise = re.search(
        r"\bi(?:'ll| will)?\s+(?:remember (?:that|this)|keep (?:that|this) in mind|"
        r"save (?:that|this)|note (?:that|this)|record (?:that|this))\b",
        visible, re.IGNORECASE)
    return bool(action_promise or memory_promise)


_INVOKE_BLOCK = re.compile(
    r'<invoke\s+name=["\']([A-Za-z_][A-Za-z0-9_]*)["\']\s*>(.*?)</invoke\s*>',
    re.DOTALL | re.IGNORECASE)
_PARAM = re.compile(
    r'<parameter\s+name=["\']([A-Za-z_][A-Za-z0-9_]*)["\']\s*>(.*?)</parameter\s*>',
    re.DOTALL | re.IGNORECASE)

# Models occasionally collapse ``<ACTION type="MUSIC_PLAY" ... />`` into
# ``<MUSIC_PLAY ... />``. Only names in the code-owned action registry may be
# translated; unknown uppercase tags must never expand the command surface.
_ACTION_SHORTHAND = re.compile(
    r'<([A-Z][A-Z0-9_]{2,})\b([^<>]*?)/\s*>', re.DOTALL)

# Qwen sometimes prefixes VANTA's otherwise-correct action with its own
# container but omits both the inner angle bracket and a closing wrapper.
# Treat this as syntax, not authority: the embedded type must still exist in
# the code-owned registry.
_TOOL_CALL_ACTION = re.compile(
    r'<tool_call\b[^>]*>\s*<?ACTION\s+([^<>]*?)/\s*>'
    r'(?:\s*</tool_call\s*>)?', re.DOTALL | re.IGNORECASE)


def normalize_tool_syntax(response):
    """Rewrite a model's own function-call syntax as a VANTA ACTION tag.

    Every model has a native tool format it falls back to under pressure, and
    VANTA only recognises <ACTION type="..." />. When the model wrote

        <function_calls>
          <invoke name="ADD_CALENDAR_EVENT">
            <parameter name="summary">Exam</parameter>
          </invoke>
        </function_calls>

    nothing matched, the turn was treated as ordinary conversation, and the
    tags themselves were delivered to the dashboard and forwarded to Telegram.
    Himanshu got the markup instead of a calendar entry -- and pasting the same
    text back in worked, because by then it arrived as a fresh instruction.

    Refusing the syntax would be the wrong fix: the model is asking for a real
    action by a real name. Translating costs nothing and the dispatcher
    already validates the name against the registry, so an invented one is
    still refused on its own merits.
    """
    source = str(response or "")

    # Some models insert whitespace immediately after ``<`` while explicitly
    # trying to emit VANTA's protocol (``< ACTION type=...>``). Repair only a
    # tag that contains a quoted action type; ordinary prose and unknown tags
    # are left alone, and the code-owned registry still decides whether the
    # resulting action is legal.
    def repair_spaced_action(match):
        attrs = (match.group(1) or "").strip()
        named = re.search(r'\btype\s*=\s*["\']([A-Za-z_][A-Za-z0-9_]*)["\']',
                          attrs, re.IGNORECASE)
        if not named:
            return match.group(0)
        name = named.group(1).upper()
        try:
            from autonomy import KNOWN_ACTIONS
            if name not in KNOWN_ACTIONS:
                return match.group(0)
        except Exception:
            return match.group(0)
        attrs = re.sub(
            r'(\btype\s*=\s*["\'])[^"\']+(["\'])',
            lambda item: item.group(1) + name + item.group(2),
            attrs, count=1, flags=re.IGNORECASE)
        return f"<ACTION {attrs}>"

    source = re.sub(r'<\s+ACTION\s+([^<>]*?/\s*)>', repair_spaced_action,
                    source, flags=re.IGNORECASE)
    source = re.sub(r'<\s+ACTION\s+([^<>]*?)(?<!/)>', repair_spaced_action,
                    source, flags=re.IGNORECASE)
    source = re.sub(r'<\s+/\s*ACTION\s*>', '</ACTION>', source,
                    flags=re.IGNORECASE)

    def rewrite_tool_call_action(match):
        attrs = (match.group(1) or "").strip()
        named = re.search(r'\btype\s*=\s*["\']([A-Za-z_][A-Za-z0-9_]*)["\']',
                          attrs, re.IGNORECASE)
        if not named:
            return match.group(0)
        name = named.group(1).upper()
        try:
            from autonomy import KNOWN_ACTIONS
            allowed = name in KNOWN_ACTIONS
        except Exception:
            allowed = False
        if not allowed:
            return match.group(0)
        # Preserve all attributes; action_schema and the dispatcher remain the
        # enforcement boundary after this purely syntactic translation.
        attrs = re.sub(
            r'(\btype\s*=\s*["\'])[^"\']+(["\'])',
            lambda item: item.group(1) + name + item.group(2),
            attrs, count=1, flags=re.IGNORECASE)
        return f"<ACTION {attrs} />"

    source = _TOOL_CALL_ACTION.sub(rewrite_tool_call_action, source)

    def rewrite_shorthand(match):
        name = match.group(1).upper()
        try:
            from autonomy import KNOWN_ACTIONS
            allowed = name in KNOWN_ACTIONS
        except Exception:
            allowed = False
        if not allowed:
            return match.group(0)
        attrs = (match.group(2) or "").strip()
        suffix = f" {attrs}" if attrs else ""
        return f'<ACTION type="{name}"{suffix} />'

    # This must run even without a native <invoke> block: shorthand was the
    # malformed form that previously bypassed execution and leaked to users.
    source = _ACTION_SHORTHAND.sub(rewrite_shorthand, source)
    if "<invoke" not in source.lower():
        return source

    def rewrite(match):
        name = match.group(1).upper()
        params = _PARAM.findall(match.group(2) or "")
        attrs, body = [], None
        for key, raw in params:
            value = (raw or "").strip()
            # A multi-line value is content, not an attribute -- WRITE_FILE and
            # RUN_PYTHON carry their payload in the tag body.
            if "\n" in value and body is None:
                body = value
                continue
            attrs.append(f'{key}="{value.replace(chr(34), "&quot;")}"')
        rendered = " ".join(attrs)
        if body is not None:
            return f'<ACTION type="{name}" {rendered}>{body}</ACTION>'.replace("  ", " ")
        return f'<ACTION type="{name}" {rendered} />'.replace("  ", " ")

    converted = _INVOKE_BLOCK.sub(rewrite, source)
    # The wrapper is noise once its children are ACTION tags.
    converted = re.sub(r'</?function_calls\s*>', '', converted,
                       flags=re.IGNORECASE)
    return converted



# --------------------------------------------------------------------------- #
# What is worth saying aloud
# --------------------------------------------------------------------------- #
#: By default a spoken answer is one or two sentences. Everything else stays on
#: screen, where it can be read at the reader's pace and does not take the
#: listener's time. These are ceilings, not targets: the model is told to be
#: briefer than this, and these catch the times it is not.
MAX_SPOKEN_SENTENCES = 2
EXPANDED_SPOKEN_SENTENCES = 6
EXPANDED_WORD_LIMIT = 140

_WANTS_DETAIL = re.compile(
    r"\b(tell me more|more detail|in detail|detailed|elaborate|expand on|"
    r"walk me through|go on|keep going|break (?:it|that) down|full "
    r"(?:report|briefing|story|rundown)|the whole thing|read (?:it|them|that) "
    r"(?:out|to me)|explain (?:it|that|this|why|how)|why is that)\b",
    re.IGNORECASE)


def wants_detail(user_text):
    """Whether the person asked to hear more than the usual short answer."""
    return bool(_WANTS_DETAIL.search(str(user_text or "")))


_LIST_LINE = re.compile(r"^\s*(?:[-*•]\s+|\d{1,2}[.)]\s+)\S")
_TABLE_LINE = re.compile(r"^\s*\|.*\|\s*$|^\s*[-|: ]{6,}$")
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"'(“])")


def prose_and_dense(text):
    """Split a response into speakable prose and list/table lines.

    Bulleted and numbered lines and tables are reading material. Read aloud,
    five bullets are a monotone recital the listener cannot skim and cannot
    interrupt usefully; on screen they take a glance.
    """
    prose, dense = [], []
    for line in str(text or "").splitlines():
        if _LIST_LINE.match(line) or _TABLE_LINE.match(line):
            dense.append(line.strip())
        elif line.strip():
            prose.append(line.strip())
    return " ".join(prose), dense


def spoken_digest(text, word_limit=SPOKEN_WORD_LIMIT,
                  max_sentences=MAX_SPOKEN_SENTENCES, screen_available=False):
    """The part of a response that should be said: its first sentence or two.

    Whole sentences only, within a word budget. A pointer to omitted details
    is allowed only when the caller has arranged actual display delivery.
    Without that destination a pure list gets a bounded spoken fallback.
    """
    prose, dense = prose_and_dense(_clean_markup(text) if text else "")
    if not prose and not dense:
        return ""
    if not prose:
        if screen_available:
            return "The details are on your screen."
        return bounded_spoken_fallback(' '.join(dense), word_limit)
    sentences = [s.strip() for s in _SENTENCE_END.split(prose) if s.strip()]
    chosen, used = [], 0
    for sentence in sentences:
        count = len(sentence.split())
        if chosen and (len(chosen) >= max_sentences or used + count > word_limit):
            break
        if not chosen and count > word_limit:
            clipped = " ".join(sentence.split()[:word_limit]).rstrip(" ,;:-")
            chosen.append(clipped + ".")
            used = word_limit
            break
        chosen.append(sentence)
        used += count
    spoken = " ".join(chosen)
    if dense and screen_available and not spoken.rstrip().endswith(("screen.", "screen")):
        # A list was withheld; say so only when the prose gives no hint of it.
        if not re.search(r"\b(screen|below|listed)\b", spoken, re.IGNORECASE):
            spoken += " The rest is on your screen."
    if not screen_available:
        spoken = re.sub(
            r'\b(?:the\s+)?(?:rest|details|results|report|information)\s+(?:is|are)\s+'
            r'(?:now\s+)?(?:on|in)\s+(?:your|the)\s+(?:screen|dashboard)\b[^.!?]*[.!?]?',
            "I haven't displayed those details.", spoken, flags=re.I)
    return spoken


# Dates and times are spoken the way a person says them. An ISO timestamp read
# digit by digit ("two zero two six dash one zero...") is the "full dates" that
# made replies tedious.
_ISO_WHEN = re.compile(
    r"(?:\b(at|on|by|for|from|until|till)\s+)?"
    r"\b(\d{4})-(\d{2})-(\d{2})"
    r"(?:[T ](\d{2}):(\d{2})(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?)?\b")
_WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday",
             "Saturday", "Sunday")
_MONTHS = ("January", "February", "March", "April", "May", "June", "July",
           "August", "September", "October", "November", "December")


def _spoken_clock(hour, minute):
    suffix = "AM" if hour < 12 else "PM"
    hour12 = hour % 12 or 12
    if hour == 0 and minute == 0:
        return "midnight"
    if hour == 12 and minute == 0:
        return "noon"
    return f"{hour12} {suffix}" if minute == 0 else f"{hour12}:{minute:02d} {suffix}"


def spoken_when(match, today=None):
    import datetime
    today = today or datetime.date.today()
    try:
        day = datetime.date(int(match.group(2)), int(match.group(3)),
                            int(match.group(4)))
    except ValueError:
        return match.group(0)
    offset = (day - today).days
    # "at tomorrow" is not English; a relative day needs no preposition.
    preposition = None if offset in (-1, 0, 1) else match.group(1)
    lead = f"{preposition} " if preposition else ""
    if offset == 0:
        label = "today"
    elif offset == 1:
        label = "tomorrow"
    elif offset == -1:
        label = "yesterday"
    elif 1 < offset < 7:
        label = _WEEKDAYS[day.weekday()]
    else:
        label = f"{day.day} {_MONTHS[day.month - 1]}"
        if day.year != today.year:
            label += f" {day.year}"
    if match.group(5) is None:
        return lead + label
    clock = _spoken_clock(int(match.group(5)), int(match.group(6)))
    return f"{lead}{label} at {clock}"


def speak_dates(text, today=None):
    return _ISO_WHEN.sub(lambda m: spoken_when(m, today), str(text or ""))
