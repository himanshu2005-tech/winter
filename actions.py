import os
import re

import action_attrs
import time
import pyautogui
import webbrowser
import subprocess
import json
import html
import hashlib
import threading
from dataclasses import dataclass
from PIL import ImageGrab

from vision import get_capture_origin, get_capture_size, get_virtual_screen_bounds, is_protected_window
from autonomy import action_allowed as _action_allowed, confirmation_required as _policy_confirmation_required
# Dispatch lives in a table, not in a ladder. `action` registers a handler
# against one or more action names; `execute_actions` looks the name up.
import action_bus
from action_bus import action

_TURN_PRESENTATION = threading.local()


@dataclass(frozen=True)
class DirectUserDelivery:
    """A tool result that may be delivered without another model turn.

    ``surface`` is deliberately typed instead of being inferred from words in
    ``spoken``.  In particular, saying "on your dashboard" is never enough to
    make content visible: dashboard delivery must enqueue a
    ``dashboard_document`` event first.
    """

    content: str
    spoken: str = ""
    surface: str = "activity"
    title: str = "VANTA Output"
    language: str = ""


def set_direct_user_delivery(content, spoken="", *, surface="activity",
                             title="VANTA Output", language=""):
    """Record a validated, explicitly routed direct result for ``agent.py``."""
    normalized_surface = str(surface or "activity").strip().lower()
    if normalized_surface not in {"activity", "dashboard"}:
        raise ValueError(f"Unsupported direct-delivery surface: {surface}")
    delivery = DirectUserDelivery(
        content=str(content or ""),
        spoken=str(spoken or ""),
        surface=normalized_surface,
        title=str(title or "VANTA Output"),
        language=str(language or ""),
    )
    execute_actions._direct_user_delivery = delivery
    # Transitional mirrors for callers/tests that still inspect the original
    # fields.  Delivery itself is consumed through the typed record above.
    execute_actions._direct_user_result = delivery.content
    execute_actions._direct_user_spoken = delivery.spoken
    return delivery


def enqueue_direct_user_delivery(ui_queue):
    """Consume one direct result and return speech only after UI enqueue.

    A dashboard claim is safe only after its document event has reached the UI
    queue.  Ordinary short results retain the non-interrupting Activity path.
    The neutral fallbacks intentionally make no visibility claim.
    """
    delivery = getattr(execute_actions, "_direct_user_delivery", None)
    if delivery is None:
        # Compatibility for extension actions that have not moved to the typed
        # contract yet.  Crucially, their fallback does not claim a dashboard.
        content = str(getattr(execute_actions, "_direct_user_result", "") or "")
        spoken = str(getattr(execute_actions, "_direct_user_spoken", "") or "")
        if not content:
            execute_actions._direct_user_result = None
            execute_actions._direct_user_spoken = None
            return ""
        delivery = DirectUserDelivery(content=content, spoken=spoken)

    execute_actions._direct_user_delivery = None
    execute_actions._direct_user_result = None
    execute_actions._direct_user_spoken = None
    if not delivery.content.strip():
        return ""

    if delivery.surface == "dashboard":
        ui_queue.put(("dashboard_document", {
            "title": delivery.title,
            "language": delivery.language,
            "content": delivery.content,
        }))
        return (delivery.spoken
                or "I found the requested information and opened it on the dashboard.")

    ui_queue.put(("info", delivery.content))
    return delivery.spoken or "I found the requested information."


def set_turn_presentation(value):
    """Bind the router's semantic delivery choice to this execution thread."""
    choice = str(value or "inline").strip().lower()
    _TURN_PRESENTATION.value = choice if choice in {"inline", "report"} else "inline"


def _turn_presentation():
    # Direct handler tests and maintenance calls predate routing and remain
    # usable. Every live agent turn sets this explicitly before execution.
    return getattr(_TURN_PRESENTATION, "value", "report")


_TURN_EXECUTION_VISIBILITY = threading.local()


def set_turn_execution_visibility(value):
    """Bind foreground/background execution for the current agent thread."""
    choice = str(value or "foreground").strip().lower()
    _TURN_EXECUTION_VISIBILITY.value = (
        choice if choice in {"foreground", "background"} else "foreground")


def _turn_runs_in_background():
    return getattr(_TURN_EXECUTION_VISIBILITY, "value", "foreground") == "background"


class _ExecutionUIQueue:
    """Suppress transient progress from a background turn, not final results.

    Handlers historically write directly to the queue as well as through
    ActionContext.  A queue view is therefore the single boundary that keeps
    every existing and future action honest without action-name conditions.
    Approval prompts are emitted before this view is installed and remain
    visible.
    """
    _TRANSIENT = frozenset({"action", "info"})

    def __init__(self, queue):
        self._queue = queue

    def put(self, item, *args, **kwargs):
        try:
            kind = item[0]
        except Exception:
            kind = None
        if kind in self._TRANSIENT:
            return None
        return self._queue.put(item, *args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._queue, name)

# Desktop control is live. It was previously disabled because input landed in
# the wrong application; control is now targeted by accessible name and every
# text entry can assert which window it expects (see desktop.py). Set
# WINTER_FOREGROUND_OBSERVER_MODE=1 to fall back to read-only observation.
FOREGROUND_OBSERVER_MODE = os.getenv("WINTER_FOREGROUND_OBSERVER_MODE", "0").strip().lower() in {
    "1", "true", "yes", "on"
}
_SCREEN_MUTATING_ACTIONS = {
    "BEGIN_VISIBLE_SESSION", "END_VISIBLE_SESSION", "OPEN_URL",
    "CLICK", "CLICK_ID", "DOUBLE_CLICK", "DOUBLE_CLICK_ID", "DOUBLECLICK",
    "DOUBLECLICK_ID", "RIGHT_CLICK", "RIGHT_CLICK_ID", "RIGHTCLICK",
    "RIGHTCLICK_ID", "TYPE", "PRESS", "HOTKEY", "MOUSE_MOVE", "MOUSE_DRAG", "SCROLL",
    "CLICK_TEXT", "MOVE_MOUSE", "DRAG", "FOCUS_WINDOW", "WINDOW_STATE",
    "CLOSE_WINDOW", "LAUNCH_APP",
}


# Actions whose payload lives between the tags rather than in an attribute.
# The parser uses this to lift bodies out before scanning, so anything quoted
# inside one is never mistaken for an action to run.
BLOCK_BODY_ACTIONS = (
    "RUN_PYTHON", "RUN_BLENDER_SCRIPT", "RUNTIME_APPLY", "RUN_SANDBOX", "WRITE_CODE",
    "BOARD_WRITE", "BOARD_MATH", "ADD_PANEL", "UPDATE_PANEL",
    "ADD_DIRECTIVE", "CREATE_ACTION", "CREATE_WORKFLOW", "DASHBOARD_SHOW_TEXT",
    "RESULT_WINDOW",
    "WIDGET",
    "ADD_TASKS",
    "WRITE_PROJECT_FILE", "EDIT_PROJECT_FILE", "BOARD_EDIT_PAGE", "DRAW_SCENE",
    "SHOW_ON_SCREEN",
    # A plan is a list of lines, and a list of lines in an XML attribute is a
    # list of lines with the newlines taken out.
    "PLAN_PURSUIT", "START_PURSUIT",
    # Documents are written as Markdown between the tags.
    "CREATE_PRESENTATION", "CREATE_DOCUMENT", "CREATE_SPREADSHEET", "CREATE_PDF",
    "NOTE_ADD",
    # Mail body text is content, not an XML attribute.  Keeping it protected
    # also prevents a quoted ACTION example inside an email from executing.
    "SEND_EMAIL",
    "REPLY_EMAIL",
)


def _tag_body(response_text, action_type):
    """The text between <ACTION ...> and </ACTION> for one action type.

    Several teaching actions carry their payload as a block rather than an
    attribute, because LaTeX and lesson prose are full of quotes and newlines
    that do not survive an XML attribute.
    """
    from action_protocol import extract
    for envelope in extract(response_text):
        if action_attrs.parse(envelope.attributes).get('type', '').upper() == action_type.upper():
            return envelope.body
    return ''


# Markdown-ish prefixes so a whole lesson can arrive in one BOARD_WRITE and
# still land with a typographic hierarchy. Without these the model has to emit
# one tag per line, which makes a lesson twenty round-trips instead of one.
_BOARD_PREFIXES = (
    ("### ", "subheading"), ("## ", "heading"), ("# ", "title"),
    ("> ", "quote"), ("! ", "warning"), ("? ", "answer"),
    ("* ", "point"), ("- ", "point"),
)


# Inline mathematics. A model writing a lesson puts short symbols in prose
# constantly ("$QK^T$ is every query against every key"), and leaving the
# delimiters in litters the board with dollar signs. The board canvas lays out
# text, not mixed text-and-image runs, so inline maths is unwrapped to its
# source rather than typeset -- "QK^T" reads fine, "$QK^T$" does not. Anything
# that genuinely needs setting goes on its own line as $$...$$ or through
# BOARD_MATH, which is what the catalogue tells the model to do.
# A dollar followed by a digit is money, not mathematics. Without the
# digit guards, "costs $5 and $9" read as one delimited expression and
# came out as "costs 5 and 9" -- which a lesson on economics would hit
# in its first paragraph.
_INLINE_MATH = re.compile(
    r"(?<!\$)\$(?![\d\s$])([^$\n]{1,120}?)(?<![\s$])\$(?![\d$])")


def _strip_inline_math(text):
    return _INLINE_MATH.sub(lambda match: match.group(1), text)


def _board_lines(ui_queue, text, forced_style="", color=""):
    """Send a block of lesson text to the board, line by line, with styles."""
    written = 0
    in_code = False
    previous_style = ""
    paragraph = []

    def flush():
        if not paragraph:
            return 0
        ui_queue.put(("board_write", {"text": " ".join(paragraph),
                                      "style": "body", "color": color}))
        paragraph.clear()
        return 1

    for raw_line in str(text).split("\n"):
        line = raw_line.rstrip()
        stripped = line.strip()
        if stripped.startswith("```"):
            written += flush()
            in_code = not in_code
            continue
        if not stripped:
            written += flush()
            previous_style = ""
            continue
        if in_code:
            ui_queue.put(("board_write", {"text": line, "style": "code",
                                          "color": color}))
            written += 1
            continue
        # $$ ... $$ on its own line is a formula, not prose.
        if stripped.startswith("$$") and stripped.endswith("$$") and len(stripped) > 4:
            written += flush()
            ui_queue.put(("board_math", {"latex": stripped[2:-2].strip()}))
            written += 1
            previous_style = ""
            continue

        indented = line.startswith(("  ", "\t"))
        style = forced_style
        if not style:
            for prefix, mapped in _BOARD_PREFIXES:
                if stripped.startswith(prefix):
                    style = "subpoint" if (mapped == "point" and indented) else mapped
                    stripped = stripped[len(prefix):]
                    break
            else:
                # An indented line under a styled one continues it, rather
                # than starting a new paragraph. Restricting this to bullets
                # meant a wrapped warning or definition dropped back to plain
                # body text halfway through its own sentence.
                style = (previous_style if indented and previous_style
                         and previous_style != "body" else "body")

        # Consecutive plain lines are one paragraph, not several. Sending each
        # source line separately gave every hard-wrapped line the vertical gap
        # of a new paragraph, so prose came out looking like a list.
        if style == "body" and not forced_style:
            paragraph.append(_strip_inline_math(stripped))
            previous_style = style
            continue
        written += flush()
        ui_queue.put(("board_write", {"text": _strip_inline_math(stripped),
                                      "style": style, "color": color,
                                      "continuation": style == previous_style
                                      and indented}))
        written += 1
        previous_style = style
    written += flush()
    return written

def _observer_mode_block(action_type, attrs):
    if not FOREGROUND_OBSERVER_MODE:
        return None
    normalized = (action_type or "").upper()
    if normalized in _SCREEN_MUTATING_ACTIONS:
        return (
            f"[Foreground Observer Mode] {normalized} was not executed. "
            "VANTA can analyze the window already on screen, but it will not open, "
            "focus, click, type into, or close desktop applications."
        )
    if normalized == "RUN_COMMAND":
        command = (attrs.get("cmd", "") or "").lower()
        if re.search(r"\b(start-process|chrome(?:\.exe)?|msedge(?:\.exe)?|explorer(?:\.exe)?|cursor(?:\.exe)?|code(?:\.exe)?|blender(?:\.exe)?)\b", command):
            return (
                "[Foreground Observer Mode] Desktop/application launch command was blocked. "
                "Open the application yourself, then ask VANTA to analyze what is already visible."
            )
    return None

def _requires_confirmation(attrs, action_label, origin="user"):
    # A background event has no human approval, irrespective of what the model
    # puts in an XML attribute.
    allowed, policy_error = _action_allowed(action_label.split(" ", 1)[0], origin)
    if not allowed:
        return False, policy_error
    confirmed = attrs.get('confirmed', '').strip().lower() == 'true'
    if confirmed:
        return True, None
    msg = (f"[Confirmation Required] {action_label} was not executed. "
           f"If you are confident this is correct, reissue the identical "
           f"ACTION tag with confirmed=\"true\" added. If there is real "
           f"ambiguity about intent, use <ASK> instead.")
    return False, msg

# ------------------------------------------------------------------ #
# EXECUTED-ACTION LEDGER
#
# The executor re-reads the whole conversation (including its own earlier
# ACTION tags) every turn, so a side-effecting action it already completed
# is easy to re-emit. Without a ledger that means a second calendar event,
# a second email, a second alarm. The ledger is scoped to one task and
# records the receipt of every side-effecting action that succeeded, so a
# repeat is answered with the original receipt instead of running again.
# ------------------------------------------------------------------ #
SIDE_EFFECT_ACTIONS = {
    "SEND_EMAIL", "REPLY_EMAIL", "SEND_WHATSAPP", "SEND_TELEGRAM",
    "SEND_TELEGRAM_FILE",
    "ADD_CALENDAR_EVENT", "DELETE_CALENDAR_EVENT", "ADD_TASK", "ADD_TASKS",
    "SET_ALARM", "ADD_ALARM", "DELETE_ALARM",
    "WRITE_FILE", "SAVE_FILE", "DELETE_FILE", "MOVE_FILE", "COPY_FILE",
    "RUN_COMMAND", "RUN_PYTHON", "RUN_BLENDER_SCRIPT", "RUNTIME_APPLY", "RUNTIME_RESUME",
    "TRACK_COMMITMENT", "COMPLETE_COMMITMENT",
    "ADD_LIFE_GOAL", "SET_LIFE_PREFERENCE",
    "LEARN_KNOWLEDGE", "KEEP_DATASET_PREVIEW",
    "REMEMBER_FACT", "CORRECT_FACT", "FORGET_FACT",
    "REMEMBER_FACE", "FORGET_FACE",
    "COMPLETE_TASK", "UPDATE_TASK",
    # Desktop actions change the world but are cheap to repeat deliberately;
    # only the destructive or app-launching ones are replay-guarded.
    "CLOSE_WINDOW", "LAUNCH_APP", "LOCK_SCREEN", "CLIPBOARD_WRITE",
    "WATCH_CREATE", "WATCH_PAUSE", "WATCH_RESUME", "WATCH_CANCEL", "FLIGHT_WATCH_CREATE",
    "SET_TIMER", "SEND_TELEGRAM", "SPOTIFY_QUEUE",
    "KILL_PROCESS", "ARCHIVE_EMAIL", "MARK_EMAIL_READ", "UPDATE_CALENDAR_EVENT",
    "SHUTDOWN", "RESTART",
    "SHUTDOWN", "RESTART",
    "WRITE_CODE",
    "IDE_OPEN_PROJECT", "IDE_OPEN_FILE", "IDE_SHOW_DIFF",
    "CODING_TASK_START", "CODING_TASK_CANCEL",
    "DRIVE_UPLOAD", "DRIVE_CREATE_FOLDER", "DRIVE_RENAME", "DRIVE_MOVE",
    "DRIVE_COPY", "DRIVE_TRASH", "DRIVE_SHARE", "DRIVE_RESTORE",
    "PEOPLE_CREATE", "PEOPLE_UPDATE", "PEOPLE_DELETE", "OPEN_BRAIN_MAP",
    "SET_STANDBY", "RESUME_STANDBY",
    "CREATE_PRESENTATION", "CREATE_DOCUMENT", "CREATE_SPREADSHEET", "CREATE_PDF",
    "OPEN_PATH", "DOWNLOAD_FILE", "ZIP_PATH", "UNZIP_FILE", "NOTE_ADD", "NOTE_DELETE",
    "PDF_MERGE", "PDF_EXTRACT",
}

# Attributes that do not change what the action actually does.
_LEDGER_IGNORED_ATTRS = {"type", "confirmed", "speech"}

_task_ledger = {}
_task_attempts = set()
_task_request = ""
_task_report_payload = None


def reset_task_ledger(request_text=""):
    """Called by agent.py at the start of each task."""
    global _task_request, _task_report_payload
    _task_ledger.clear()
    _task_attempts.clear()
    _task_request = str(request_text or "")
    _task_report_payload = None


def last_task_report():
    """The substantive report already rendered during the current task.

    Completion narration is a different channel.  Keeping this tiny piece of
    task state prevents a final sentence such as "the report is ready" from
    replacing the document that RESULT_WINDOW has already put on screen.
    """
    return dict(_task_report_payload) if isinstance(_task_report_payload, dict) else None


def _remember_task_report(payload):
    global _task_report_payload
    if not isinstance(payload, dict):
        return
    content = str(payload.get('content') or payload.get('text') or '').strip()
    blocks = payload.get('blocks')
    if not content and not isinstance(blocks, list):
        return
    _task_report_payload = dict(payload)


def _report_render_receipt(title, content):
    """Compact observable evidence for the critic, without logging the report.

    A line count alone cannot prove that a requested table, timeline or source
    list exists, which caused the critic to demand the same 130-line report
    repeatedly.  Report only structural metadata, domains and a content hash;
    the potentially sensitive prose remains in the local result surface.
    """
    import hashlib
    from urllib.parse import urlparse

    text = str(content or '')
    headings = [
        match.group(1).strip() for match in
        re.finditer(r'^#{1,6}\s+(.+?)\s*$', text, re.MULTILINE)
    ][:6]
    domains = []
    for url in re.findall(r'https?://[^\s)>]+', text):
        domain = (urlparse(url.rstrip('.,;:')).netloc or '').lower()
        if domain.startswith('www.'):
            domain = domain[4:]
        if domain and domain not in domains:
            domains.append(domain)
    tables = len(re.findall(r'^\s*\|?.+\|.+\n\s*\|?\s*:?-{2,}',
                            text, re.MULTILINE))
    diagrams = len(re.findall(r'```\s*(?:mermaid|diagram|flow|flowchart)\b',
                              text, re.IGNORECASE))
    formulae = len(re.findall(r'\$\$|```\s*(?:latex|math|tex)\b',
                              text, re.IGNORECASE))
    digest = hashlib.sha256(text.encode('utf-8')).hexdigest()[:12]
    structure = (f"headings={headings or ['none']}; tables={tables}; "
                 f"diagrams={diagrams}; formulae={formulae}; "
                 f"source_domains={domains[:6] or ['none']}")
    return (f"[Success] Result window rendered title={title!r}; "
            f"lines={len(text.splitlines())}; {structure}; sha256={digest}.")


# The executor owns action announcements.  Keeping this callback here avoids
# importing agent.py (and its UI/audio startup) from the action layer, while
# still giving every action -- built-in, extension, and future action -- the
# same behaviour without editing hundreds of handlers.
_action_speaker = None


def set_action_speaker(callback):
    """Install the runtime speech callback, or ``None`` to disable it."""
    global _action_speaker
    _action_speaker = callback


_ACTION_SPEECH_VERBS = {
    "ADD": "Adding", "APPLY": "Applying", "BROWSE": "Browsing",
    "CAPTURE": "Capturing", "CHECK": "Checking", "CLICK": "Clicking",
    "CLOSE": "Closing", "COPY": "Copying", "CREATE": "Creating",
    "DELETE": "Deleting", "DISCOVER": "Finding", "DOWNLOAD": "Downloading",
    "DRAW": "Drawing", "EDIT": "Editing", "EXPORT": "Exporting",
    "FILL": "Filling", "FIND": "Finding", "FOCUS": "Focusing",
    "GET": "Checking", "GOTO": "Opening", "INSPECT": "Inspecting",
    "LAUNCH": "Opening", "LIST": "Checking", "LOAD": "Loading",
    "LOCATE": "Finding", "MOVE": "Moving", "OPEN": "Opening",
    "PAUSE": "Pausing", "PLAY": "Playing", "PRESS": "Pressing",
    "READ": "Reading", "REAUTHORIZE": "Reconnecting", "REMOVE": "Removing",
    "RENDER": "Rendering", "RESTART": "Restarting", "RESUME": "Resuming",
    "RUN": "Running", "SAVE": "Saving", "SEARCH": "Searching",
    "SELECT": "Selecting", "SEND": "Sending", "SET": "Setting",
    "SHOW": "Showing", "START": "Starting", "STOP": "Stopping",
    "SYNC": "Syncing", "TAKE": "Taking", "TOGGLE": "Toggling",
    "TYPE": "Typing", "UPDATE": "Updating", "UPLOAD": "Uploading",
    "VERIFY": "Verifying", "VIEW": "Opening", "WAIT": "Waiting",
    "WATCH": "Checking", "WRITE": "Writing",
}

_ACTION_SPEECH_NAMESPACES = {
    "ACTION", "ACTIONS", "BROWSER", "CAMERA", "CONTROL", "IDE", "MUSIC",
    "PROJECT", "RUNTIME", "SCREEN", "SYSTEM", "WINDOW", "WINDOWS",
}


def normalise_action_speech(value, fallback="Working on it"):
    """Return a safe two-or-three-word action announcement.

    The model supplies the phrase, but malformed prose must not turn an action
    announcement into another full answer.  Markup and punctuation are
    discarded, one-word phrases gain ``now``, and longer phrases are capped.
    Missing speech stays backwards compatible with old actions through one
    neutral three-word fallback.
    """
    clean = re.sub(r"<[^>]+>", " ", str(value or ""))
    words = re.findall(r"[A-Za-z0-9]+(?:['’-][A-Za-z0-9]+)?", clean)
    if not words:
        return str(fallback or "").strip()
    words = words[:3]
    if len(words) == 1:
        words.append("now")
    return " ".join(words)


def _derived_action_speech(action_type, attrs=None):
    """Describe an action when a model omitted its optional spoken phrase.

    This is deliberately capability-agnostic: it derives the wording from the
    action name and, when present, the model's own short ``purpose``.  New
    actions therefore receive useful speech without adding another per-action
    table.  Paths, commands, URLs and payloads are never used as narration.
    """
    attrs = attrs or {}
    purpose = str(attrs.get("purpose") or "").strip()
    if (purpose and len(purpose) <= 90 and "\n" not in purpose
            and not re.search(r"(?:https?://|[A-Za-z]:\\|[/\\]{2}|@)", purpose)):
        phrase = normalise_action_speech(purpose, fallback="")
        if phrase:
            first = phrase.split()[0]
            gerund = _ACTION_SPEECH_VERBS.get(first.upper())
            if gerund:
                phrase = " ".join([gerund] + phrase.split()[1:])
            return phrase

    tokens = [part for part in re.split(r"[^A-Za-z0-9]+",
                                        str(action_type or "").upper()) if part]
    if not tokens:
        return "Starting action"

    verb_index = next((i for i, token in enumerate(tokens)
                       if token in _ACTION_SPEECH_VERBS), None)
    if verb_index is None:
        objects = [t for t in tokens if t not in _ACTION_SPEECH_NAMESPACES]
        subject = (objects or tokens)[0].lower()
        return normalise_action_speech(f"Checking {subject}")

    verb = _ACTION_SPEECH_VERBS[tokens[verb_index]]
    objects = [token.lower() for i, token in enumerate(tokens)
               if i != verb_index and token not in _ACTION_SPEECH_NAMESPACES]
    if not objects:
        # Namespace is still more informative than a universal filler.
        objects = [token.lower() for i, token in enumerate(tokens)
                   if i != verb_index][:1] or ["action"]
    return normalise_action_speech(" ".join([verb] + objects[:2]))


def action_speech(attrs=None, action_type=""):
    """Return model-authored action speech, or a relevant safe fallback."""
    attrs = attrs or {}
    requested = normalise_action_speech(attrs.get("speech", ""), fallback="")
    if requested:
        return requested
    return _derived_action_speech(action_type or attrs.get("type", ""), attrs)


def announce_action(attrs, action_type=""):
    """Speak once, synchronously, immediately before an action is dispatched."""
    phrase = action_speech(attrs, action_type=action_type)
    action_background = str((attrs or {}).get("background", "")).strip().lower()
    if (_turn_runs_in_background()
            or action_background in {"true", "yes", "1", "on"}):
        return phrase
    callback = _action_speaker
    if callback is not None:
        try:
            callback(phrase)
        except Exception as exc:
            # Audio must never make the requested capability unavailable.
            print(f"[Action Speech] Could not announce {phrase!r}: {exc}")
    return phrase


def _volume_requested(text):
    """True only when this turn explicitly concerns machine loudness."""
    return bool(re.search(
        r"\b(?:volume|sound|audio|loud|louder|quiet|quieter|soft|softer|mute|unmute)\b",
        str(text or ""), re.IGNORECASE))


def _ledger_key(action_type, attrs, body=""):
    material = sorted(
        (k, v) for k, v in attrs.items() if k not in _LEDGER_IGNORED_ATTRS
    )
    if body:
        # Block actions often carry their actual operation in the body. Two
        # incremental Blender revisions with the same project attributes are
        # not duplicates merely because their Python differs only there.
        material.append(("__body_sha256", hashlib.sha256(
            str(body).encode("utf-8")).hexdigest()))
    return f"{action_type}|{material}"


def _classify_output(output):
    """Whether an action's own receipt reports failure.

    Only the documented prefixes count. Matching 'error' anywhere in the body
    would misread a search result or a log file that merely contains the word.
    """
    text = (output or "").lstrip()
    if not text:
        return False

    # A browser receipt carries its verdict in a flag, not a prefix. Nothing
    # downstream read it: `[Browser Receipt] {"ok": false, "filled": [],
    # "failed": [...]}` does not start with any documented failure prefix, so
    # every failed browser step was recorded as a success.
    #
    # That is how a BROWSER_FILL_FORM contorted into fields="url=..." -- which
    # filled nothing on a blank page -- marked a portal workflow satisfied,
    # after which every attempt to navigate was refused as a duplicate of an
    # outcome that had already "succeeded". The flag was added precisely so a
    # form where every field failed would stop reading as a login; the half
    # that reads the flag was missing.
    if text.startswith("[Browser Receipt]"):
        import json as _json
        body = text[len("[Browser Receipt]"):].strip()
        try:
            receipt = _json.loads(body)
            if not isinstance(receipt, dict):
                return False
            return receipt.get('ok') is True and receipt.get('verified') is not False
        except (ValueError, AttributeError):
            return False       # unreadable evidence cannot complete an outcome

    lowered = text.casefold()
    natural_failure = (
        lowered.startswith(("could not ", "failed to ", "unable to "))
        or "nothing is playing" in lowered
        or "needs approval" in lowered
        or "did not start" in lowered
    )
    return not natural_failure and not lowered.startswith(tuple(prefix.casefold() for prefix in (
        "[Error]", "[Action Error]", "[Authorization Policy]", "[Autonomy Policy]",
        "[Mode Policy]", "[Confirmation Required]", "[Approval Required]",
        "[Foreground Observer Mode]", "[Interrupted]", "Error:",
        "[Unsupported Action]", "[Visible Control Error]", "[Blocked]",
        "[Unverified]", "[Partial]", "[Pending]", "[Browser Error]",
        "[Not Run]",
        "[Calendar Policy]", "[Dataset Error]", "[Unavailable]", "[Failed]", "[Invalid]",
    )))


# Python can do everything a shell can. RUN_COMMAND required approval and
# RUN_PYTHON did not, so the gate had a door beside it -- and the model found
# that door on its own: told twice that its shell command was waiting for
# approval, it rewrote the same filesystem check as RUN_PYTHON to get the answer
# without one. That is not misbehaviour to punish, it is a hole to close.
#
# Gating all Python would be the safe and lazy answer, but most of it is
# arithmetic on data already in hand, and Himanshu asked to be interrupted only
# for things that cannot be undone. So the test is what the code reaches for.
_PYTHON_ESCAPES = (
    "subprocess", "os.system", "os.popen", "os.exec", "os.spawn",
    "pty.spawn", "shutil.rmtree", "shutil.move", "shutil.copy",
    "os.remove", "os.unlink", "os.rmdir", "os.rename", "os.replace",
    "os.chmod", "os.chown", "winreg", "ctypes", "win32api", "win32com",
    "pyautogui", "keyboard.", "socket.", "requests.post", "requests.put",
    "requests.delete", "urllib.request.urlopen", "smtplib", "ftplib",
    "paramiko", "pip install", "__import__", "eval(", "exec(",
)


def python_needs_approval(code):
    """Whether this Python reaches outside its own process.

    Returns the reason, or "" when the code only computes. Named reasons rather
    than a bare bool because a refusal Himanshu cannot understand is a refusal
    he will approve blindly.
    """
    body = str(code or "")
    lowered = body.lower()
    for token in _PYTHON_ESCAPES:
        if token.lower() in lowered:
            return f"it uses {token}"
    # Writing files is the other irreversible thing Python does easily.
    if "open(" in lowered and any(mode in lowered for mode in
                                  ("'w'", '"w"', "'a'", '"a"', "'wb'", '"wb"',
                                   "'ab'", '"ab"', "'x'", '"x"')):
        return "it opens a file for writing"
    return ""


def _queue_for_approval(action_type, attrs, ui_queue, origin, turn_id="",
                        outcome_id=""):
    """Route an irreversible action to the real human approval queue.

    WRITE_FILE and the alarm actions may be parsed after the main loop. Without
    this bridge they could bypass the same policy as their registered
    equivalents. SEND_EMAIL now uses the registered path so body, attachments,
    approval and provider receipt stay together.
    """
    import uuid, json as _json
    runtime = getattr(execute_actions, "_outcome_runtime", None)
    if not outcome_id and runtime is not None:
        outcome_id = runtime.outcome_for_action(action_type)
    if not turn_id:
        turn_id = str(getattr(execute_actions, "_turn_id", "") or "")
    try:
        from memory.state_db import execute_write
        from memory.neo4j_store import store as _approval_store
        action_id = str(uuid.uuid4())
        payload_json = _json.dumps(attrs, sort_keys=True)
        from execution_reliability import fingerprint
        integrity_hash = fingerprint(action_type, attrs, turn_id, outcome_id)
        existing = _approval_store.query(
            "MATCH (a:Approval {status:'pending',action_type:$type,payload:$payload}) "
            "WHERE a.expires_at IS NULL OR a.expires_at > toString(datetime()) "
            "RETURN a.id AS id ORDER BY a.created_at DESC LIMIT 1",
            type=action_type, payload=payload_json)
        if existing:
            action_id = existing[0]["id"]
        else:
            execute_write(
                """INSERT INTO approvals (id, action_type, payload, inference_source, status, expires_at)
                   VALUES (?, ?, ?, ?, 'pending', datetime('now', '+1 hour'))""",
                (action_id, action_type, payload_json, origin)
            )
            _approval_store.query(
                "MATCH (a:Approval {id:$id}) SET a.turn_id=$turn_id, "
                "a.outcome_id=$outcome_id, a.integrity_hash=$integrity_hash",
                id=action_id, turn_id=str(turn_id or ""),
                outcome_id=str(outcome_id or ""),
                integrity_hash=integrity_hash)
            execute_write(
                """INSERT INTO action_log (id, action_type, payload, inference_source, outcome, verified_flag)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (str(uuid.uuid4()), action_type, payload_json, origin, 'pending_approval', False)
            )
    except Exception as exc:
        # Never fall through to executing an irreversible action because the
        # approval store was unavailable.
        print(f"[Approvals] Could not queue {action_type}: {exc}")
        return f"[Error] {action_type} was not executed: the approval queue is unavailable."
    # A prompt with buttons, not just a line of text. "Check dashboard or say
    # 'approve that'" pointed at a control that did not exist, so the only way
    # through was knowing an exact phrase -- and one unanswered approval cost
    # eleven model calls and 71,508 prompt tokens of retrying.
    try:
        from approval_prompt import describe
        detail = describe(action_type, attrs)
    except Exception:
        detail = action_type
    ui_queue.put(("approval_request", {
        "id": action_id, "action_type": action_type, "detail": detail,
    }))
    # A prompt on a screen he is not looking at is no better than no prompt.
    # Telegram has no buttons here, so the reply words are spelled out -- and
    # "approve" from Telegram now actually works, which it did not before.
    try:
        import turn_context
        if turn_context.is_remote():
            from providers.telegram_bot import send_telegram_message
            send_telegram_message(
                f"Approval needed: {detail}\n\n"
                f"Reply 'approve' to run it, or 'reject' to cancel.")
    except Exception as exc:
        print(f"[Approvals] Could not reach Telegram: {exc}")
    # No buttons to point at. Approval is a word he says or types, which
    # reaches VANTA from his phone and from across the room, unlike a cursor.
    ui_queue.put(("info", f"VANTA needs approval: {detail}\n"
                          f"Say or type 'approve' to run it, or 'reject'."))
    # Said plainly to the model too, because it retried this twice and then
    # tried to reach the same result through a different action entirely. It
    # is also told how to ask, so the request reaches Himanshu as a sentence
    # rather than only as a panel he may not be looking at.
    return (f"[Approval Required] {action_type} is queued and waiting on "
            f"Himanshu. Do NOT retry it, and do NOT attempt the same thing "
            f"through another action. Ask him, in one short line, to approve "
            f"it -- then stop.")


# Handled after the main dispatch loop rather than inside it. The loop has no
# branch for these, so it reached its "the handler never wrote a receipt" check
# and reported failure -- for actions that then ran perfectly a few lines later.
# SET_ALARM was the visible case: the alarm was set and VANTA told Himanshu it
# had failed twice, which is worse than either outcome on its own, because now
# he distrusts a receipt that was right.
# ------------------------------------------------------------------ #
# PIPELINE CONTROLS
#
# The checks that run around every action, as data rather than as tests
# written into the loop. Each of these used to be a literal comparison in
# execute_actions, so extending one meant editing the pipeline; now it means
# adding an entry, which is what someone adding an action expects to do.
# ------------------------------------------------------------------ #

#: Names that once existed and no longer do, with what to use instead. The
#: *_ID variants targeted a numbered element map that is never populated, so
#: they could never hit anything; name-based clicking replaced them.
RETIRED_ACTIONS = {
    name: (
        "[Error] {action_type} no longer exists: there are no numbered "
        "screen IDs. Use CLICK name=\"...\" for a native control, "
        "CLICK_SCREEN_TEXT text=\"...\" for anything with a visible label, "
        "or CLICK x= y= with coordinates from a current screenshot."
    )
    for name in ("CLICK_ID", "DOUBLE_CLICK_ID", "RIGHT_CLICK_ID",
                 "DOUBLECLICK_ID", "RIGHTCLICK_ID")
}

#: Handled by a pass that runs after the main loop, which has the tag body as
#: well as the attributes. Letting the loop see them too meant one action
#: queued an approval here and was written there, so the receipt announced it
#: was waiting while the file already existed.
DEFERRED_ACTIONS = frozenset({"SAVE_FILE", "WRITE_FILE"})

#: One attempt per task, whatever the arguments. Starting media is not
#: idempotent even when the query changes: a stale verification signal must
#: not launch several tracks over one another.
ONCE_PER_TASK_ACTIONS = {
    "MUSIC_PLAY": (
        "[Blocked] {action_type} was already attempted in this task and was "
        "not run again. Inspect MUSIC_NOW_PLAYING or report the existing "
        "result; do not start another track."
    ),
    "YOUTUBE_PLAY": (
        "[Blocked] {action_type} was already attempted in this task and was "
        "not run again. Inspect the current YouTube tab or report the "
        "existing playback receipt instead of starting another video."
    ),
}

#: Looking at a page is not doing anything to it.
#:
#: Driving a browser costs two steps per interaction, because every click has
#: to be followed by a snapshot to see what it did. The gate-pass application
#: spent five of its sixteen steps looking, ran out one step from the end, and
#: reported "the step budget ended before I could verify the submission" --
#: having filled and submitted the form correctly.
#:
#: So these give their step back, the way capability discovery already does.
#: Bounded, because a turn that only ever looks is a turn in a loop.
OBSERVATION_ONLY_ACTIONS = frozenset({
    "BROWSER_SNAPSHOT", "BROWSER_READ", "BROWSER_TABS",
})


#: Actions that must not be followed by the settle pause: WAIT has already
#: waited exactly as long as it was asked to, and OPEN_URL hands off to the
#: browser and is done.
NO_SETTLE_DELAY = frozenset({"WAIT", "OPEN_URL"})

# Only raw desktop interactions need a shared settling boundary. API/file
# operations already return their result; managed browsers own their waits.
DESKTOP_SETTLE_ACTIONS = frozenset({
    'CLICK', 'DOUBLE_CLICK', 'RIGHT_CLICK', 'CLICK_TEXT', 'CLICK_SCREEN_TEXT',
    'TYPE', 'PRESS', 'HOTKEY', 'DRAG', 'SCROLL', 'FOCUS_WINDOW', 'WINDOW_STATE',
    'CLOSE_WINDOW', 'LAUNCH_APP', 'SET_FIELD', 'TOGGLE_FIELD', 'SELECT_OPTION',
    'FILL_FORM',
})

#: How long to let the desktop settle after an action that touched it.
SETTLE_DELAY_SECONDS = 0.5


POST_LOOP_ACTIONS = frozenset({
    "SET_ALARM", "ADD_ALARM", "DELETE_ALARM", "LIST_ALARMS",
})


def parse_alarm_tags(response_text):
    """Every alarm the reply asks for, parsed by attribute not by position.

    Public and separate so the parsing can be checked by parsing something.
    The test for this used to search execute_actions for the regex literal,
    which broke whenever the pattern changed -- including when the change was
    to accept more spellings -- and never established that a tag parses at all.

    Both SET_ALARM and ADD_ALARM. ADD_ALARM was authorized, given retrieval
    vocabulary and a confirmation rule, and never implemented anywhere.

    The pattern is attribute-order independent because the earlier one
    demanded exactly time= then label= then />, so the recurrence= the
    catalogue openly advertises made the tag match nothing -- and the receipt
    said "SET_ALARM produced no result. Check that its required attributes
    were supplied and spelled correctly", sending the model to check spelling
    that was already right.
    """
    found = []
    for tag in re.findall(
            r'<ACTION\s+([^>]*?type="(?:SET_ALARM|ADD_ALARM)"[^>]*?)/>',
            str(response_text or ""), re.IGNORECASE):
        attrs = action_attrs.parse(tag)
        if attrs.get('time') and attrs.get('label'):
            found.append(attrs)
    return found


def _calendar_create_count(response_text):
    """How many calendar events one reply is asking to create.

    Counted from the reply rather than tracked across the loop, so the
    handler can ask the question itself instead of depending on a variable
    that happened to be in scope.
    """
    return len(re.findall(r'\btype=["\']ADD_CALENDAR_EVENT["\']',
                          str(response_text or ""), re.IGNORECASE))


def _record(action_type, output):
    """Record a post-loop action against the same contract as the main loop."""
    execute_actions._last_output = output
    ok = _classify_output(output)
    execute_actions._results.append({"type": action_type, "ok": ok, "output": output})
    return ok


# Read-only actions whose result is itself the requested deliverable. Their
# receipts used to go only to the model and the hidden Activity page, while the
# model confidently said "displayed on your screen". User-triggered results
# are surfaced in the scrollable Details card; background checks stay quiet.
VISIBLE_RESULT_ACTIONS = frozenset({
    "TRENDING_NEWS", "KNOWLEDGE_SEARCH", "SEARCH_WEB", "SEARCH_RECALL",
    "FIND_FILES", "LIST_DIRECTORY", "GITHUB_SEARCH", "FLIGHT_SEARCH",
    "FIND_API", "DRIVE_LIST", "DRIVE_SEARCH", "LIST_TASKS",
    "LIST_CALENDAR_EVENTS", "LIST_COMMITMENTS", "WATCH_LIST",
    "WATCH_HISTORY", "WATCH_STATUS", "WATCH_CHECK", "LIST_DOCUMENTS",
    "READ_EMAILS", "LIST_ALARMS", "LIST_PROCEDURES", "LIST_KNOWN_FACES", "WHO_IS_THIS",
    "STANDBY_STATUS", "CODING_TASK_STATUS", "GITHUB_READ_REPO",
    "GET_WEATHER", "GET_AIR_QUALITY", "INDIA_POST_LOOKUP", "SYSTEM_STATUS",
})
# DISCOVER_ACTIONS was in that set and should never have been. It is VANTA
# looking up its own tool list -- machinery, not a deliverable. Nobody asks
# "discover actions", so its twelve-row capability dump was never the thing
# that was wanted, and it arrived as a 680x720 card over whatever was being
# worked on. What a person wants to know about a step like this is that it is
# happening and what it is doing, which is one line in the notch.


def _surface_action_result(ui_queue, action_type, output, origin):
    """Keep action receipts as evidence, never desktop interruptions.

    Visibility is a turn-level semantic decision made before tools run. This
    keeps web, files, calendar and future capabilities consistent without an
    action-name or phrase allow-list. Explicit RESULT_WINDOW and final REPORT
    delivery remain the intentional ways to open the rich surface.
    """
    return False

    # Legacy presenter retained below temporarily for compatibility archaeology;
    # it is intentionally unreachable now that presentation is turn-owned.
    if origin != "user" or action_type not in VISIBLE_RESULT_ACTIONS:
        return False
    text = str(output or "").strip()
    if not text or not _classify_output(text):
        return False
    if len(text) > 12000:
        text = text[:12000] + "\n\n…additional output omitted."
    from result_presenter import present
    presented = present(action_type, text)
    # Search receipts arrive before the model has compared or synthesised
    # anything. Opening a source-only window at that point replaces the answer
    # with links and makes every research step interrupt the desktop.
    if action_type in {"SEARCH_WEB", "KNOWLEDGE_SEARCH", "SEARCH_RECALL"} \
            and not presented.get("synthesized"):
        return False
    ui_queue.put(("artifacts", {
        "title": presented["title"],
        "status": presented["status"],
        "links": presented["links"],
        "summary": presented.get("summary", ""),
        "quality": presented.get("quality", ""),
        "sources": presented.get("sources", []),
        "items": [{"kind": "result", "value": presented["text"]}],
    }))
    return True


def _announce_background_work(text):
    """Speak one short pre-flight line without coupling actions to the UI.

    ``agent.py`` installs itself under the stable ``agent`` module name even
    when launched as ``__main__``. Looking up the already-running module avoids
    importing a second core (and therefore a second microphone listener).
    """
    import sys
    core = sys.modules.get("agent")
    speaker = getattr(core, "speak", None) if core is not None else None
    if callable(speaker):
        speaker(str(text).strip(), False, block=True)


last_command_output = ""

# ------------------------------------------------------------------ #
# COORDINATE TRANSLATION
#
# The model only ever sees IMAGE-RELATIVE pixel coordinates. Every
# CLICK/DOUBLE_CLICK/etc needs translating to TRUE screen coordinates
# (which can be negative on multi-monitor setups) before SetCursorPos.
# ------------------------------------------------------------------ #

from vision import (
    get_send_scale,
    get_capture_size,
    get_capture_origin,
    get_virtual_screen_bounds,
    is_protected_window,
)

def _clamp_to_capture(x, y):
    scale = get_send_scale()
    if scale != 1.0:
        x = x / scale
        y = y / scale
    cw, ch = get_capture_size()
    x = max(0, min(int(x), cw - 1))
    y = max(0, min(int(y), ch - 1))
    return x, y

def _to_true_screen_coords(x, y):
    ox, oy = get_capture_origin()
    abs_x, abs_y = x + ox, y + oy
    vx, vy, vw, vh = get_virtual_screen_bounds()
    abs_x = max(vx, min(abs_x, vx + vw - 1))
    abs_y = max(vy, min(abs_y, vy + vh - 1))
    return abs_x, abs_y


def _clamp_to_screen(x, y):
    return _clamp_to_capture(x, y)


# ------------------------------------------------------------------ #
# SELF-PROTECTION — HARD BLOCK
# ------------------------------------------------------------------ #

def _resolve_window_title_at(abs_x, abs_y):
    import win32gui
    try:
        hwnd = win32gui.WindowFromPoint((abs_x, abs_y))
        if hwnd:
            return win32gui.GetWindowText(hwnd)
    except Exception:
        pass
    return ""

def _resolve_focused_window_title():
    import win32gui
    try:
        hwnd = win32gui.GetForegroundWindow()
        if hwnd:
            return win32gui.GetWindowText(hwnd)
    except Exception:
        pass
    return ""

def _guard_click_target(abs_x, abs_y):
    title = _resolve_window_title_at(abs_x, abs_y)
    if is_protected_window(title):
        reason = (f"Blocked a click targeting a protected window: '{title}'. "
                   f"This is VANTA's own source code / editor, and clicking or "
                   f"typing into it is never allowed.")
        print(f"[SAFETY] {reason}")
        execute_actions._blocked_reason = reason
        return False
    return True

def _guard_type_target():
    title = _resolve_focused_window_title()
    if is_protected_window(title):
        reason = (f"Blocked a TYPE action: the currently focused window is '{title}', "
                   f"which is VANTA's own source code / editor. Typing there is never "
                   f"allowed. Click the ACTUAL intended app (e.g. Notepad) first, and "
                   f"confirm it via a fresh screenshot before retrying TYPE.")
        print(f"[SAFETY] {reason}")
        execute_actions._blocked_reason = reason
        return False
    return True

def _move_mouse_smoothly(target_x, target_y, duration=0.4):
    import win32api, time
    start_x, start_y = win32api.GetCursorPos()
    steps = int(duration / 0.015)

    if steps <= 0:
        win32api.SetCursorPos((target_x, target_y))
        return

    for i in range(1, steps + 1):
        t = i / steps
        eased = 1 - (1 - t)**3  # ease-out cubic
        curr_x = int(start_x + (target_x - start_x) * eased)
        curr_y = int(start_y + (target_y - start_y) * eased)
        win32api.SetCursorPos((curr_x, curr_y))
        time.sleep(0.015)

    win32api.SetCursorPos((target_x, target_y))
    time.sleep(0.1)


def _invoke_accessible_target(x, y, intended_label):
    """Invoke a named accessibility control without guessing nearby pixels.

    UIA InvokePattern is preferable to mouse input: it targets the control
    itself and remains correct if borders, DPI scaling, or browser chrome move
    by a few pixels.  Unnamed OmniParser regions fall back to one guarded click.
    """
    if not intended_label:
        return None, False
    try:
        import uiautomation as auto

        abs_x, abs_y = _to_true_screen_coords(x, y)
        control = auto.ControlFromPoint(abs_x, abs_y)
        intended = intended_label.strip().lower()
        for _ in range(5):
            if control is None:
                break
            actual = (control.Name or "").strip()
            actual_low = actual.lower()
            labels_match = actual_low and (
                intended in actual_low or actual_low in intended
            )
            if labels_match:
                try:
                    pattern = control.GetInvokePattern()
                    if pattern is not None:
                        pattern.Invoke()
                        print(f"[Action] Invoked UIA target '{actual}'")
                        return actual, True
                except Exception:
                    pass
            try:
                control = control.GetParentControl()
            except Exception:
                break
    except Exception as e:
        print(f"[Action] UIA invoke unavailable: {e}")
    return None, False


def _click_target_once(x, y, intended_label, ui_queue, base_dir):
    """Use semantic invocation when possible, otherwise one guarded click.

    A lack of visible screen change is not proof that a click missed (focus,
    selection, and canvas state often change without enough pixels moving), so
    this function never sprays offset retries around the intended target.
    """
    actual_name, invoked = _invoke_accessible_target(x, y, intended_label)
    if invoked:
        return actual_name, True
    actual_name = _win32_click(x, y, ui_queue)
    return actual_name, actual_name is not None

def _win32_click(x, y, ui_queue):
    """(x, y) are IMAGE-RELATIVE. Translates to true screen coords,
    then hard-checks the resolved window before touching the OS.
    Returns the UIA-resolved name of whatever ended up under the
    cursor after the click (or None if blocked/unavailable), so the
    caller can compare it against what the model intended to click."""
    import win32api, win32con

    abs_x, abs_y = _to_true_screen_coords(x, y)

    if not _guard_click_target(abs_x, abs_y):
        return None

    _move_mouse_smoothly(abs_x, abs_y)

    needs_double_click = False
    try:
        import uiautomation as auto
        import win32gui, win32process
        import psutil

        hwnd = win32gui.WindowFromPoint((abs_x, abs_y))
        if hwnd:
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            proc = psutil.Process(pid)
            if proc.name().lower() == "explorer.exe":
                elem = auto.ControlFromPoint(abs_x, abs_y)
                if elem and elem.ControlType in (auto.ControlType.ListItemControl, auto.ControlType.DataItemControl, auto.ControlType.TreeItemControl):
                    needs_double_click = True
    except Exception:
        pass

    if needs_double_click:
        print(f"[Action] Auto-upgrading click at ({abs_x}, {abs_y}) to DOUBLE CLICK for File Explorer item")
        win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
        time.sleep(0.06)
        win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
        time.sleep(0.08)
        win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
        time.sleep(0.06)
        win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
        time.sleep(0.1)
        verified_name = _log_click_verification(abs_x, abs_y)
        return verified_name

    win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
    time.sleep(0.08)
    win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
    time.sleep(0.1)
    verified_name = _log_click_verification(abs_x, abs_y)

    time.sleep(0.4)
    return verified_name

def _log_click_verification(abs_x, abs_y):
    """Resolves and returns the name of whatever UIA element is under
    the cursor right after a click — the ground-truth check that lets
    the caller detect a near-miss click. Falls back to OCR text under
    the cursor when UIA reports nothing useful (common on webpages
    where a clicked "button" is really just styled text with a thin
    or absent accessibility node)."""
    try:
        import uiautomation as auto
        elem = auto.ControlFromPoint(abs_x, abs_y)
        if elem is not None:
            name = elem.Name or ""
            if name:
                print(f"[Verify] Element under cursor after click: '{name}' ({elem.ControlTypeName})")
                return name
    except Exception as e:
        print(f"[Verify] UIA check failed: {e}")

    print("[Verify] No named UIA element found under cursor after click.")
    return "(no element found)"

def _win32_double_click(x, y, ui_queue):
    import win32api, win32con

    abs_x, abs_y = _to_true_screen_coords(x, y)

    if not _guard_click_target(abs_x, abs_y):
        return

    _move_mouse_smoothly(abs_x, abs_y)

    win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
    time.sleep(0.06)
    win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
    time.sleep(0.08)
    win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
    time.sleep(0.06)
    win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
    time.sleep(0.1)
    _log_click_verification(abs_x, abs_y)
    time.sleep(0.4)

def _win32_right_click(x, y, ui_queue):
    import win32api, win32con

    abs_x, abs_y = _to_true_screen_coords(x, y)

    if not _guard_click_target(abs_x, abs_y):
        return

    _move_mouse_smoothly(abs_x, abs_y)

    win32api.mouse_event(win32con.MOUSEEVENTF_RIGHTDOWN, 0, 0, 0, 0)
    time.sleep(0.08)
    win32api.mouse_event(win32con.MOUSEEVENTF_RIGHTUP, 0, 0, 0, 0)
    time.sleep(0.1)

def _win32_move_rel(dx, dy):
    import win32api

    vx, vy, vw, vh = get_virtual_screen_bounds()
    cur_x, cur_y = win32api.GetCursorPos()
    new_x = max(vx, min(cur_x + dx, vx + vw - 1))
    new_y = max(vy, min(cur_y + dy, vy + vh - 1))
    win32api.SetCursorPos((new_x, new_y))
    return new_x, new_y

def _win32_drag_rel(dx, dy, ui_queue):
    import win32api, win32con

    ui_queue.put(("hide_overlay", ""))
    time.sleep(0.05)

    vx, vy, vw, vh = get_virtual_screen_bounds()
    start_x, start_y = win32api.GetCursorPos()
    end_x = max(vx, min(start_x + dx, vx + vw - 1))
    end_y = max(vy, min(start_y + dy, vy + vh - 1))

    win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
    time.sleep(0.05)

    steps = max(4, int(max(abs(dx), abs(dy)) / 20))
    for i in range(1, steps + 1):
        ix = int(start_x + (end_x - start_x) * i / steps)
        iy = int(start_y + (end_y - start_y) * i / steps)
        win32api.SetCursorPos((ix, iy))
        time.sleep(0.01)

    time.sleep(0.05)
    win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
    time.sleep(0.1)

    ui_queue.put(("show_overlay", ""))


# ------------------------------------------------------------------ #
# DETERMINISTIC BROWSER NAVIGATION
#
# webbrowser.open() fires an OS-level "open this URL" request and has
# NO idea which window/tab actually receives it, or whether it landed
# in an existing browser window vs a brand new one. That made
# OPEN_URL the single least reliable action in the whole pipeline —
# everything downstream (finding the address bar's ID box, confirming
# the page loaded) depended on the model visually re-discovering
# things that had no guaranteed location yet.
#
# This replaces it with a direct, scripted sequence: find an existing
# browser window if one is open (reuse it with a new tab), or launch
# one if not, then drive the address bar with Ctrl+L — a shortcut
# that focuses the address bar unconditionally, with no ID box or
# click required at all. This removes the single least reliable step
# (visually finding the address bar) from the entire navigation path.
# ------------------------------------------------------------------ #

_BROWSER_CLASS_NAMES = ("chrome_widgetwin_1", "msvirtualgroupparent")  # Chrome, Edge top-level class

def _find_existing_browser_hwnd():
    """Find an open Chrome/Edge HWND using ctypes EnumWindows (works from any thread)."""
    import ctypes, ctypes.wintypes
    import win32gui

    found_hwnd = [0]

    EnumWindowsProc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.wintypes.HWND, ctypes.wintypes.LPARAM)

    def _enum_cb(hwnd, _):
        if found_hwnd[0]:
            return True
        if not ctypes.windll.user32.IsWindowVisible(hwnd):
            return True
        buf = ctypes.create_unicode_buffer(256)
        ctypes.windll.user32.GetClassNameW(hwnd, buf, 256)
        cls = buf.value.lower()
        if any(b in cls for b in ("chrome_widget", "msvirtualgroup")):
            title_buf = ctypes.create_unicode_buffer(256)
            ctypes.windll.user32.GetWindowTextW(hwnd, title_buf, 256)
            if title_buf.value.strip():
                found_hwnd[0] = hwnd
        return True

    ctypes.windll.user32.EnumWindows(EnumWindowsProc(_enum_cb), 0)
    return found_hwnd[0]

#: The last page OPEN_URL handed to his own browser, so a later read of the
#: managed browser can notice that it is looking somewhere else.
_last_opened_url = {"url": "", "at": 0.0}


def _host_of(url):
    from urllib.parse import urlparse
    try:
        return (urlparse(str(url)).hostname or "").lower().removeprefix("www.")
    except ValueError:
        return ""


def cross_browser_note(snapshot_text, now=None):
    """A warning when the managed browser is not on the page OPEN_URL opened.

    OPEN_URL shows a page in HIS browser. BROWSER_READ reads VANTA's own,
    separate one. Asked to check a portal, the model opened it in his and then
    read its own -- which was on a blank start page -- and reported that there
    were no jobs while they were on his screen. Nothing connected the two, so
    this does: if a page was opened for him recently and the browser being read
    is somewhere else, say so, at the moment it matters.
    """
    import re as _re
    import time as _time
    now = now if now is not None else _time.time()
    opened = _last_opened_url["url"]
    if not opened or now - _last_opened_url["at"] > 900:
        return ""
    match = _re.search(r'"url":\s*"([^"]*)"', str(snapshot_text or ""))
    current = match.group(1) if match else ""
    if current and _host_of(current) == _host_of(opened):
        return ""
    where = current or "a different page"
    return (f"[Note] This is VANTA's own browser, which is on {where}. The page "
            f"{opened} was opened earlier in HIS browser with OPEN_URL, and "
            f"nothing in his browser can be read from here. To read or use "
            f"that page, call BROWSER_GOTO url=\"{opened}\" -- his signed-in "
            f"sessions are already copied across. Do not report that a page "
            f"is empty when you have only looked at the wrong browser.\n")


def _navigate_browser(url, ui_queue):
    """Open a URL in HIS default browser. VANTA cannot read what is there."""
    import webbrowser
    import time
    print(f"[Action] Opening URL: {url}")
    webbrowser.open(url)
    time.sleep(3.0)  # Wait for page load



def _email_digest_for_model(raw):
    """Bounded mail context, with owner-configurable snippet disclosure.

    Sender, subject and attachment names are always useful operational
    metadata. Snippets can contain private message text, so installations keep
    them out of the task-model context by default. Himanshu's private VANTA
    instance opts in through VANTA_EMAIL_MODEL_CONTENT so grounded summaries
    and reply drafts can use the same short Gmail evidence as inbox triage.
    Attachment bytes and full bodies are never included here.
    """
    import identity
    include_snippets = identity.env_flag("EMAIL_MODEL_CONTENT", False)
    text = str(raw or "")
    if not text.strip() or text.startswith("No emails found"):
        return "Model-visible digest: 0 messages matched."

    entries, current = [], {}
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("[") and "]" in line:
            if current:
                entries.append(current)
            current = {
                "unread": "(unread)" in line,
                "message_id": line[1:line.index("]")].strip()[:120],
            }
        elif line.startswith("From:"):
            current["from"] = line[5:].strip()[:70]
        elif line.startswith("Reply-To:"):
            current["reply_to"] = line[9:].strip()[:100]
        elif line.startswith("Thread-ID:"):
            current["thread_id"] = line[10:].strip()[:120]
        elif line.startswith("Subject:"):
            current["subject"] = line[8:].strip()[:90]
        elif line.startswith("Attachments:"):
            current["attachments"] = line[12:].strip()[:240]
        elif line.startswith("Snippet:"):
            current["snippet"] = line[8:].strip()[:500]
    if current:
        entries.append(current)

    if not entries:
        return "Model-visible digest: could not read the result format."

    unread = sum(1 for item in entries if item.get("unread"))
    lines = [f"Model-visible digest: {len(entries)} message(s), "
             f"{unread} unread. Each item includes bounded Gmail metadata; "
             f"attachment bytes are not in your context."]
    for item in entries:
        mark = "unread" if item.get("unread") else "read"
        line = (f"  - ({mark}) {item.get('from', 'unknown')} "
                f"| message_id={item.get('message_id', 'unknown')} "
                f"| thread_id={item.get('thread_id', 'unknown')} "
                f"| reply_to: {item.get('reply_to', '(use From)')} "
                f"| {item.get('subject', 'no subject')} "
                f"| attachments: {item.get('attachments', 'None')}")
        if include_snippets:
            line += f" | snippet: {item.get('snippet', '(none)')}"
        lines.append(line)
    if include_snippets:
        lines.append("Use this evidence to summarize or draft. Do not invent "
                     "anything beyond the bounded Gmail fields above.")
    else:
        lines.append("You cannot retrieve the bodies or snippets from this "
                     "handle. Summarize only the metadata above.")
    return "\n".join(lines)

# ====================================================================
# ACTION HANDLERS
# ====================================================================
# Extracted mechanically from the if/elif ladder that used to
# live inside execute_actions. Each one is now a registration
# on the action bus: dispatch is a dictionary lookup, and a new
# capability is this decorator plus a function rather than
# another rung in a 3,500-line ladder.
#
# The bodies are the originals, comments and all, because every
# one of those comments records a failure this code exists to
# prevent. What changed around them: the names come from the
# context instead of the enclosing function, preconditions that
# used to sit between rungs are declared as controls, and an
# inner ladder became a table of closures.

@action('BEGIN_VISIBLE_SESSION', family='BEGIN_VISIBLE_SESSION')
def _run_begin_visible_session(ctx):
    """Handles BEGIN_VISIBLE_SESSION."""
    action_type = ctx.action_type
    attrs = ctx.attrs
    ui_queue = ctx.ui_queue
    base_dir = ctx.base_dir
    origin = ctx.origin
    turn_id = ctx.turn_id
    interrupt_event = ctx.interrupt_event
    element_map = ctx.element_map
    response_text = ctx.response_text
    # Superseded by native desktop control; kept so an older
    # prompt or transcript still does the sensible thing.
    import desktop
    target = attrs.get('target', '') or attrs.get('app', '')
    result = desktop.launch(target) if target else desktop.list_windows()
    if target and attrs.get('maximize', 'true').strip().lower() != 'false':
        desktop.window_state(target, 'maximize')
    execute_actions._last_output = result
    ui_queue.put(("info", result))


@action('END_VISIBLE_SESSION', family='END_VISIBLE_SESSION')
def _run_end_visible_session(ctx):
    """Handles END_VISIBLE_SESSION."""
    action_type = ctx.action_type
    attrs = ctx.attrs
    ui_queue = ctx.ui_queue
    base_dir = ctx.base_dir
    origin = ctx.origin
    turn_id = ctx.turn_id
    interrupt_event = ctx.interrupt_event
    element_map = ctx.element_map
    response_text = ctx.response_text
    result = "[Success] Nothing to release; desktop control is always available."
    execute_actions._last_output = result
    ui_queue.put(("info", result))


@action('PROFILE_DATASET', family='PROFILE_DATASET')
def _run_profile_dataset(ctx):
    """Handles PROFILE_DATASET."""
    action_type = ctx.action_type
    attrs = ctx.attrs
    ui_queue = ctx.ui_queue
    base_dir = ctx.base_dir
    origin = ctx.origin
    turn_id = ctx.turn_id
    interrupt_event = ctx.interrupt_event
    element_map = ctx.element_map
    response_text = ctx.response_text
    import dataset_tools
    path = attrs.get('path', '')
    result = dataset_tools.profile_csv(path) if path else "[Error] This needs path=\"...\" pointing at a real file. Use FIND_FILES if you do not know where it is."
    execute_actions._last_output = result
    ui_queue.put(("info", result[:1800]))


@action('AUDIT_DATASET', family='AUDIT_DATASET')
def _run_audit_dataset(ctx):
    """Handles AUDIT_DATASET."""
    action_type = ctx.action_type
    attrs = ctx.attrs
    ui_queue = ctx.ui_queue
    base_dir = ctx.base_dir
    origin = ctx.origin
    turn_id = ctx.turn_id
    interrupt_event = ctx.interrupt_event
    element_map = ctx.element_map
    response_text = ctx.response_text
    import dataset_tools
    path = attrs.get('path', '')
    result = (dataset_tools.audit(path, attrs.get('target', '') or None)
              if path else "[Error] This needs path=\"...\" pointing at a real file. Use FIND_FILES if you do not know where it is.")
    execute_actions._last_output = result
    if _classify_output(result):
        set_direct_user_delivery(
            result,
            "I've audited the dataset, sir. The findings are on your dashboard.",
            surface="dashboard", title="Dataset audit")
    ui_queue.put(("info", result[:2400]))


@action('SPLIT_DATASET', family='SPLIT_DATASET')
def _run_split_dataset(ctx):
    """Handles SPLIT_DATASET."""
    action_type = ctx.action_type
    attrs = ctx.attrs
    ui_queue = ctx.ui_queue
    base_dir = ctx.base_dir
    origin = ctx.origin
    turn_id = ctx.turn_id
    interrupt_event = ctx.interrupt_event
    element_map = ctx.element_map
    response_text = ctx.response_text
    import dataset_tools

    def _f(key, default):
        try:
            return float(attrs.get(key, default))
        except (TypeError, ValueError):
            return default
    path = attrs.get('path', '')
    result = (dataset_tools.split_dataset(
        path, target=attrs.get('target', '') or None,
        test_size=_f('test_size', 0.2),
        validation_size=_f('validation_size', 0.0),
        stratify=attrs.get('stratify', 'true').strip().lower() != 'false',
        seed=int(_f('seed', 42)),
        output_dir=attrs.get('output_dir', '') or None)
        if path else "[Error] This needs path=\"...\" pointing at a real file. Use FIND_FILES if you do not know where it is.")
    execute_actions._last_output = result
    ui_queue.put(("info", result[:1800]))


@action('CREATE_DATASET_CLEANING_PREVIEW', family='CREATE_DATASET_CLEANING_PREVIEW')
def _run_create_dataset_cleaning_preview(ctx):
    """Handles CREATE_DATASET_CLEANING_PREVIEW."""
    action_type = ctx.action_type
    attrs = ctx.attrs
    ui_queue = ctx.ui_queue
    base_dir = ctx.base_dir
    origin = ctx.origin
    turn_id = ctx.turn_id
    interrupt_event = ctx.interrupt_event
    element_map = ctx.element_map
    response_text = ctx.response_text
    import dataset_tools
    path = attrs.get('path', '')
    output_dir = attrs.get('output_dir', '') or None
    result = (dataset_tools.create_cleaning_preview(
        path, output_dir,
        target=attrs.get('target', '') or None,
        impute=attrs.get('impute', 'median'),
        drop_outliers=attrs.get('drop_outliers', '').strip().lower() == 'true',
        drop_high_missing=attrs.get('drop_high_missing', 'true').strip().lower() != 'false',
    )[1] if path else "[Error] This needs path=\"...\" pointing at a real file. Use FIND_FILES if you do not know where it is.")
    execute_actions._last_output = result
    ui_queue.put(("info", result[:2400]))


@action('KEEP_DATASET_PREVIEW', family='KEEP_DATASET_PREVIEW')
def _run_keep_dataset_preview(ctx):
    """Handles KEEP_DATASET_PREVIEW."""
    action_type = ctx.action_type
    attrs = ctx.attrs
    ui_queue = ctx.ui_queue
    base_dir = ctx.base_dir
    origin = ctx.origin
    turn_id = ctx.turn_id
    interrupt_event = ctx.interrupt_event
    element_map = ctx.element_map
    response_text = ctx.response_text
    preview = attrs.get('preview_path', '')
    destination = attrs.get('destination_path', '')
    confirmed, refusal = _requires_confirmation(attrs, "KEEP_DATASET_PREVIEW", origin)
    if not confirmed:
        execute_actions._last_output = refusal
        ui_queue.put(("info", refusal))
    elif preview and destination:
        import dataset_tools
        result = dataset_tools.keep_preview(preview, destination)
        execute_actions._last_output = result
        ui_queue.put(("info", result))
    else:
        execute_actions._last_output = "[Dataset Error] preview_path and destination_path are required."


@action('RUN_COMMAND', family='RUN_COMMAND')
def _run_run_command(ctx):
    """Handles RUN_COMMAND."""
    action_type = ctx.action_type
    attrs = ctx.attrs
    ui_queue = ctx.ui_queue
    base_dir = ctx.base_dir
    origin = ctx.origin
    turn_id = ctx.turn_id
    interrupt_event = ctx.interrupt_event
    element_map = ctx.element_map
    response_text = ctx.response_text
    cmd = attrs.get('cmd', '')
    if cmd:
        label = attrs.get('_label', '') or cmd
        msg = f"Running: {label}"
        print(f"[Action] {msg}")
        ui_queue.put(("action", msg))
        ui_queue.put(("info", f"Running command:\n{label}"))
        # 15s was too short for the work this is actually used
        # for: `python -m venv` measured 20.2s on this machine and
        # `pip install numpy` 15.3s, so setting up an environment
        # timed out on its two most basic steps. Callers may ask
        # for longer; the ceiling keeps a hung command from
        # blocking the turn forever.
        try:
            timeout_s = float(attrs.get('timeout', '') or 120)
        except (TypeError, ValueError):
            timeout_s = 120.0
        timeout_s = max(1.0, min(timeout_s, 900.0))
        process = None
        try:
            import system_commands
            shell = str(attrs.get('shell', 'powershell') or 'powershell').strip().lower()
            argv, command_cwd = system_commands.prepare(
                cmd, base_dir, attrs.get('cwd', ''), shell)
            # ``subprocess.run`` cannot observe VANTA's live abort event.  A
            # long build therefore continued after the task, speech and UI had
            # all said they were cancelled.  Keep the child in a process group
            # and poll ``communicate`` so the same execution brake used by the
            # rest of the action pipeline can stop terminal work as well.
            creationflags = (getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                             | getattr(subprocess, "CREATE_NO_WINDOW", 0))
            process = subprocess.Popen(
                argv, cwd=command_cwd,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                encoding='utf-8', errors='replace',
                creationflags=creationflags,
            )
            deadline = time.monotonic() + timeout_s
            while True:
                try:
                    out, err = process.communicate(timeout=0.12)
                    break
                except subprocess.TimeoutExpired:
                    if interrupt_event is not None and interrupt_event.is_set():
                        _terminate_started_process(process)
                        out, err = process.communicate()
                        execute_actions._last_output = (
                            "[Interrupted] Command and its child processes were stopped."
                        )
                        ui_queue.put(("info", f"> {label}\n\n[Interrupted] Stopped"))
                        return
                    if time.monotonic() >= deadline:
                        _terminate_started_process(process)
                        out, err = process.communicate()
                        execute_actions._last_output = (
                            f"[Error] Command timed out after {timeout_s:.0f}s and "
                            "was stopped. For a long job, raise timeout=\"...\" or "
                            "start it in the background and use WAIT_FOR on the file "
                            "it produces."
                        )
                        ui_queue.put(("info", f"> {label}\n\n[Error] Timed out and stopped"))
                        return
            out = (out or "").strip()
            err = (err or "").strip()
            combined = f"{out}\n{err}".strip()
            # The exit code was collected and thrown away, so a
            # failed install reached the model as ordinary output
            # and read like success. It is the only reliable
            # signal PowerShell gives us.
            if process.returncode != 0:
                combined = (f"[Error] Command failed with exit code "
                            f"{process.returncode}.\n{combined}").strip()
            elif not combined:
                combined = '[Success] Command completed with exit_code=0; no output.'
            output_path = ''
            if len(combined) > 1500:
                try:
                    output_path = system_commands.preserve_output(
                        base_dir, out, err, process.returncode, shell, command_cwd)
                except OSError:
                    pass
                combined = system_commands.preview(combined, output_path)
            execute_actions._last_command_result = {
                'exit_code': process.returncode, 'shell': shell, 'cwd': command_cwd,
                'output_path': output_path, 'stdout_chars': len(out), 'stderr_chars': len(err)}
            ui_queue.put(("info", f"> {label}\n\n{combined}"))
            execute_actions._last_output = combined
            print(f"[Command Output]\n{combined}")
        except Exception as ce:
            if process is not None and process.poll() is None:
                _terminate_started_process(process)
            execute_actions._last_output = f"[Error] {ce}"
            ui_queue.put(("info", f"> {label}\n\n[Error] {ce}"))


@action('WINDOWS_RADIO', family='windows_system')
def _run_windows_radio(ctx):
    import windows_radios
    kind = str(ctx.attrs.get('kind', 'all')).strip().lower()
    state = str(ctx.attrs.get('state', 'status')).strip().lower()
    try:
        script = windows_radios.command(kind, state)
    except ValueError as exc:
        execute_actions._last_output = '[Error] ' + str(exc)
        return
    original = ctx.attrs
    try:
        ctx.attrs = {'cmd': script, 'timeout': '45', '_label': 'Windows radio control'}
        _run_run_command(ctx)
        execute_actions._last_output = windows_radios.receipt(execute_actions._last_output, kind, state)
        if execute_actions._last_output.startswith('[Success]'):
            # Turning Wi-Fi off removes the cloud path. Delivery must not
            # depend on another LLM round trip after the local readback.
            message = execute_actions._last_output.removeprefix('[Success] ')
            set_direct_user_delivery(message, message)
            if kind == 'wifi' and state == 'off':
                execute_actions._offline_completion = message
                execute_actions._stop_batch = True
    finally:
        ctx.attrs = original


def _terminate_started_process(process):
    """Stop only the process tree VANTA started for a command or Python action."""
    if process is None or process.poll() is not None:
        return
    try:
        if os.name == "nt":
            # PowerShell commonly starts a compiler, package manager or Python
            # child. Terminating only the shell leaves that real workload
            # alive, so target this known PID and its descendants.
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                capture_output=True, text=True, timeout=5,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        else:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
    except Exception:
        try:
            process.kill()
        except Exception:
            pass


@action('SEARCH_VAULT', family='SEARCH_VAULT')
def _run_search_vault(ctx):
    """Handles SEARCH_VAULT."""
    action_type = ctx.action_type
    attrs = ctx.attrs
    ui_queue = ctx.ui_queue
    base_dir = ctx.base_dir
    origin = ctx.origin
    turn_id = ctx.turn_id
    interrupt_event = ctx.interrupt_event
    element_map = ctx.element_map
    response_text = ctx.response_text
    query = attrs.get('query', '')
    if query:
        msg = f"Searching memory for: '{query}'"
        print(f"[Action] {msg}")
        ui_queue.put(("action", msg))
        ui_queue.put(("info", f"Searching memory for:\n{query}"))

        try:
            from memory.orchestrator import get_context_for_turn
            import session_context
            bundle = get_context_for_turn(query, mode="deep")
            results = bundle.text_block

            handle_ref = session_context.add_handle(f"SEARCH_VAULT result (query: {query})", results, slot="search_result")
            # The handle is kept for the focus slots, but the model
            # gets the results. Handing it a reference it has no
            # action to dereference left it searching its own memory
            # and being told nothing came back -- and unlike mail,
            # there is nothing private here: this is the same graph
            # the orchestrator already injects into every prompt.
            execute_actions._last_output = (
                f"[Success] {handle_ref}\n{results}")
            print(f"[Command Output]\n{handle_ref}")
            ui_queue.put(("info", f"> SEARCH_VAULT\n\n{handle_ref}"))
        except Exception as e:
            execute_actions._last_output = f"[Error] Memory search failed: {e}"
            print(f"[Error] Memory search failed: {e}")
            ui_queue.put(("info", f"> SEARCH_VAULT\n\n[Error] {e}"))


@action('SHOW_ON_SCREEN', 'TICK_ON_SCREEN', 'CLEAR_SCREEN', family='SHOW_ON_SCREEN')
def _run_show_on_screen(ctx):
    """Handles SHOW_ON_SCREEN, TICK_ON_SCREEN, CLEAR_SCREEN."""
    action_type = ctx.action_type
    attrs = ctx.attrs
    ui_queue = ctx.ui_queue
    base_dir = ctx.base_dir
    origin = ctx.origin
    turn_id = ctx.turn_id
    interrupt_event = ctx.interrupt_event
    element_map = ctx.element_map
    response_text = ctx.response_text
    # A card that stays visible over his work and can be changed
    # mid-task. The blackboard takes the whole screen and Control
    # Center panels need a window opened, so neither could be a
    # checklist that sits beside what he is doing.
    import hud_state
    # Variant selection, as a table. Each closure is the
    # body that used to be a rung of an inner ladder.
    card = locals().get('card')
    items = locals().get('items')
    line = locals().get('line')
    part = locals().get('part')
    res = locals().get('res')
    title = locals().get('title')
    def _v_0_show_on_screen():
        nonlocal card, items, line, part, res, title
        items = [line.strip(" -•	") for line in
                 _tag_body(response_text, 'SHOW_ON_SCREEN').splitlines()
                 if line.strip(" -•	")]
        if not items:
            items = [part.strip() for part in
                     str(attrs.get('items', '')).split('|')
                     if part.strip()]
        title = attrs.get('title', '') or 'Now'
        if not items:
            res = ("[Error] SHOW_ON_SCREEN needs the lines between "
                   "its tags, one per line, or items=\"a|b|c\".")
        else:
            card = hud_state.show(base_dir, title, items,
                                  card_id=attrs.get('id', ''))
            ui_queue.put(("hud_refresh", ""))
            res = (f"[Success] {len(card['items'])} item(s) on "
                   f"screen under {card['title']!r}. He can see "
                   f"them now.")
    def _v_1_tick_on_screen():
        nonlocal card, items, line, part, res, title
        res = hud_state.check(
            base_dir, attrs.get('item', ''),
            done=str(attrs.get('done', 'true')).lower() != 'false',
            card_id=attrs.get('id', ''))
        ui_queue.put(("hud_refresh", ""))
    def _v_default():
        nonlocal card, items, line, part, res, title
        res = hud_state.hide(base_dir, attrs.get('id', ''))
        ui_queue.put(("hud_refresh", ""))
    _variants = {
        'SHOW_ON_SCREEN': _v_0_show_on_screen,
        'TICK_ON_SCREEN': _v_1_tick_on_screen,
    }
    _variants.get(action_type, _v_default)()
    execute_actions._last_output = res
    print(f"[Action] {action_type}: {res.splitlines()[0][:120]}")
    ui_queue.put(("info", f"> {action_type}\n\n{res}"))


@action('QUIET_FOR', family='QUIET_FOR')
def _run_quiet_for(ctx):
    """Handles QUIET_FOR."""
    action_type = ctx.action_type
    attrs = ctx.attrs
    ui_queue = ctx.ui_queue
    base_dir = ctx.base_dir
    origin = ctx.origin
    turn_id = ctx.turn_id
    interrupt_event = ctx.interrupt_event
    element_map = ctx.element_map
    response_text = ctx.response_text
    # "I am napping, stay quiet" needs to be state, not a sentence
    # in one reply. Without this the promise held until the next
    # proactive event, which read him an email while he slept.
    import quiet
    ends = quiet.begin(attrs.get('minutes', ''),
                       attrs.get('reason', ''))
    if ends is None:
        res = "[Error] QUIET_FOR needs minutes=\"<number>\"."
    else:
        import datetime as _dt
        when = _dt.datetime.fromtimestamp(ends).strftime('%H:%M')
        res = (f"[Success] Quiet until {when}. Alarms and timers "
               f"still fire, and anything he asks is still answered.")
    execute_actions._last_output = res
    print(f"[Action] QUIET_FOR: {res}")
    ui_queue.put(("info", f"> QUIET_FOR\n\n{res}"))


@action('SET_STANDBY', family='standby',
        requires=(action_bus.user_only(),),
        receipt="active state, requested duration and wake controls")
def _run_set_standby(ctx):
    """Enter the lifecycle state selected by the model."""
    import runtime_power
    import screen_rest
    import threading
    import time

    raw = ctx.attr('minutes', '')
    try:
        minutes = float(raw) if str(raw).strip() else 0.0
    except (TypeError, ValueError):
        ctx.report('[Error] SET_STANDBY minutes must be a number.')
        return
    if minutes < 0 or minutes > 60 * 24 * 7:
        ctx.report('[Error] Standby duration must be between 0 and 10080 minutes.')
        return
    seconds = minutes * 60.0 if minutes else None
    monitor = str(ctx.attr('monitor', '')).strip().lower() in {
        '1', 'true', 'yes', 'on', 'monitor', 'security'
    }
    changed = runtime_power.enter(seconds, monitor=monitor)
    if changed:
        screen_rest.begin(ctx.ui_queue, 'standby mode')
        ctx.ui_queue.put(('standby', ''))
    token = runtime_power.generation()

    if seconds:
        def expire():
            while runtime_power.is_standby():
                remaining = runtime_power.remaining_seconds()
                if remaining is None:
                    return
                if remaining > 0:
                    time.sleep(min(remaining, 1.0))
                    continue
                if runtime_power.expire_if_due(token):
                    screen_rest.end(ctx.ui_queue, 'standby timer complete')
                    ctx.ui_queue.put(('show_overlay', ''))
                    print('[Standby] Timed standby completed.')
                return
        threading.Thread(target=expire, daemon=True,
                         name='vanta-standby-action-timer').start()
    duration = (f'{minutes:g} minute(s)' if minutes else
                 'until Himanshu wakes VANTA by voice or input')
    kind = 'monitor' if monitor else 'ordinary'
    transition = 'entered' if changed else 'already active; settings updated'
    ctx.report(f'[Success] Standby {transition} for {duration}; '
               f'standby_type={kind}; state_generation={token}; '
               f'changed={str(changed).lower()}.')


@action('RESUME_STANDBY', family='standby',
        requires=(action_bus.user_only(),), receipt="observed active state")
def _run_resume_standby(ctx):
    import runtime_power
    import screen_rest
    changed = runtime_power.resume()
    screen_rest.end(ctx.ui_queue, 'left standby mode')
    ctx.ui_queue.put(('show_overlay', ''))
    ctx.report(f'[Success] Standby active=0; changed={int(changed)}.')


@action('STANDBY_STATUS', family='standby', receipt="observed active state")
def _run_standby_status(ctx):
    import runtime_power
    remaining = runtime_power.remaining_seconds()
    detail = (f'; remaining_seconds={remaining:.0f}'
              if remaining is not None else '')
    ctx.report(f'[Success] Standby active={int(runtime_power.is_standby())}; '
               f'monitor={int(runtime_power.monitor_enabled())}{detail}.')


@action('CAMERA_AWARENESS_STATUS', family='standby',
        receipt="fresh structured presence and study ergonomics state")
def _run_camera_awareness_status(ctx):
    import camera_awareness
    state = camera_awareness.snapshot()
    age = (time.time() - state.observed_at) if state.observed_at else None
    age_text = f'{age:.0f}' if age is not None else 'unknown'
    ctx.report(
        f'[Success] observed_age_seconds={age_text}; present={state.present}; '
        f'himanshu_present={state.himanshu_present}; '
        f'recognized_people={list(state.recognized_people)}; '
        f'unknown_faces={state.unknown_face_count}; '
        f'attentive={state.attentive}; distance={state.distance}; '
        f'posture={state.posture}; study_present_seconds='
        f'{state.study_present_seconds:.0f}; study_breaks={state.study_breaks}; '
        f'model_assessment={state.model_assessment or "none"}; '
        f'raw_frames_retained=0.')


@action('LEARN_PROCEDURE', family='learned_procedures',
        requires=(action_bus.user_only(),),
        receipt="graph procedure id, trigger and proposed action sequence")
def _run_learn_procedure(ctx):
    import learned_procedures
    result = learned_procedures.learn(
        ctx.attr('trigger'), ctx.attr('instruction'), ctx.attr('actions'),
        evidence=ctx.attr('evidence') or ctx.response_text)
    ctx.report(result)
    ctx.say(f'> LEARN_PROCEDURE\n\n{result}')


@action('LIST_PROCEDURES', family='learned_procedures',
        receipt="active learned procedures")
def _run_list_procedures(ctx):
    import learned_procedures
    ctx.report(learned_procedures.describe())


@action('REMOVE_PROCEDURE', family='learned_procedures',
        requires=(action_bus.user_only(),),
        receipt="retired procedure id and inactive graph state")
def _run_remove_procedure(ctx):
    import learned_procedures
    ctx.report(learned_procedures.retire(
        ctx.attr('id') or ctx.attr('text')))


@action('LIST_CAMERA_CAPTURES', family='camera_library',
        requires=(action_bus.user_only(),), receipt="capture ids and timestamps")
def _run_list_camera_captures(ctx):
    import camera_library
    camera_library.purge_expired(ctx.base_dir)
    ctx.report(camera_library.describe(ctx.attr('limit', 20)))


@action('REMEMBER_FACT', family='REMEMBER_FACT')
def _run_remember_fact(ctx):
    """Handles REMEMBER_FACT."""
    action_type = ctx.action_type
    attrs = ctx.attrs
    ui_queue = ctx.ui_queue
    base_dir = ctx.base_dir
    origin = ctx.origin
    turn_id = ctx.turn_id
    interrupt_event = ctx.interrupt_event
    element_map = ctx.element_map
    response_text = ctx.response_text
    # Structured memory. LEARN_KNOWLEDGE stores prose that can only
    # be found by similarity; this writes a fact the graph can be
    # queried and reasoned over.
    subject = attrs.get('subject', '') or 'Himanshu'
    predicate = attrs.get('predicate', '')
    obj = attrs.get('object', '')
    if not predicate or not obj:
        execute_actions._last_output = "Error: REMEMBER_FACT needs predicate and object."
    else:
        from memory.graph_store import add_edge
        confidence = float(attrs.get('confidence', '0.9') or 0.9)
        res = add_edge(
            subject, predicate, obj,
            confidence=max(0.0, min(1.0, confidence)),
            source_id=f"remembered:{turn_id or 'turn'}",
            sensitivity=attrs.get('sensitivity', 'private'),
            fact_class='explicit_user_fact',
            evidence_type='user_stated',
            authority=int(attrs.get('authority', '100') or 100),
            autonomy_eligible=attrs.get('autonomy_eligible', 'false').lower() == 'true',
            autonomy_scope=attrs.get('autonomy_scope', 'advisory'),
            confirmation_required=attrs.get('confirmation_required', 'false').lower() == 'true',
            claim_key=attrs.get('claim_key', '') or None,
            evidence_quote=attrs.get('evidence_quote', '') or None,
        )
        execute_actions._last_output = str(res)
        print(f"[Agent] REMEMBER_FACT: {res}")
        ui_queue.put(("info", f"> REMEMBER_FACT\n\n{res}"))


@action('CORRECT_FACT', family='CORRECT_FACT')
def _run_correct_fact(ctx):
    """Handles CORRECT_FACT."""
    action_type = ctx.action_type
    attrs = ctx.attrs
    ui_queue = ctx.ui_queue
    base_dir = ctx.base_dir
    origin = ctx.origin
    turn_id = ctx.turn_id
    interrupt_event = ctx.interrupt_event
    element_map = ctx.element_map
    response_text = ctx.response_text
    # Replaces what VANTA believes. Without this there was no way
    # to fix something it had learned wrong.
    subject = attrs.get('subject', '') or 'Himanshu'
    predicate = attrs.get('predicate', '')
    new_object = attrs.get('object', '') or attrs.get('new_object', '')
    old_object = attrs.get('old_object', '')
    if not predicate:
        execute_actions._last_output = "Error: CORRECT_FACT needs a predicate."
    else:
        from memory.neo4j_store import store as _graph
        if new_object:
            correction = _graph.correct_fact(
                subject, predicate, new_object, old_object or None,
                reason=attrs.get('reason', 'corrected by Himanshu'),
                source_id=f"corrected:{turn_id or 'turn'}",
            )
            parts = [f"Retired {correction['retired']} outdated fact(s).", str(correction['result'])]
        else:
            execute_actions._last_output = "Error: CORRECT_FACT needs a replacement object; use FORGET_FACT to retire a claim."
            return
        res = " ".join(parts)
        execute_actions._last_output = res
        print(f"[Agent] CORRECT_FACT: {res}")
        ui_queue.put(("info", f"> CORRECT_FACT\n\n{res}"))


@action('FORGET_FACT', family='FORGET_FACT')
def _run_forget_fact(ctx):
    """Handles FORGET_FACT."""
    action_type = ctx.action_type
    attrs = ctx.attrs
    ui_queue = ctx.ui_queue
    base_dir = ctx.base_dir
    origin = ctx.origin
    turn_id = ctx.turn_id
    interrupt_event = ctx.interrupt_event
    element_map = ctx.element_map
    response_text = ctx.response_text
    subject = attrs.get('subject', '') or 'Himanshu'
    predicate = attrs.get('predicate', '')
    obj = attrs.get('object', '')
    if not predicate and not obj:
        execute_actions._last_output = (
            "Error: FORGET_FACT needs a predicate or an object, to avoid "
            "retiring everything known about the subject."
        )
    else:
        from memory.neo4j_store import store as _graph
        retired = _graph.invalidate_fact(
            subject, predicate or None, obj or None,
            reason=attrs.get('reason', 'asked to forget'),
        )
        res = (
            "Nothing matched; nothing was forgotten." if not retired else
            "Forgot: " + "; ".join(f"{r['subject']} {r['predicate']} {r['object']}" for r in retired)
        )
        execute_actions._last_output = res
        print(f"[Agent] FORGET_FACT: {res}")
        ui_queue.put(("info", f"> FORGET_FACT\n\n{res}"))


@action('RECALL_FACTS', family='RECALL_FACTS')
def _run_recall_facts(ctx):
    """Handles RECALL_FACTS."""
    action_type = ctx.action_type
    attrs = ctx.attrs
    ui_queue = ctx.ui_queue
    base_dir = ctx.base_dir
    origin = ctx.origin
    turn_id = ctx.turn_id
    interrupt_event = ctx.interrupt_event
    element_map = ctx.element_map
    response_text = ctx.response_text
    entity = attrs.get('entity', '') or attrs.get('subject', '') or 'Himanshu'
    predicate = attrs.get('predicate', '')
    from memory.neo4j_store import store as _graph
    from memory.orchestrator import _render_fact, _filter_facts
    limit = int(attrs.get('limit', '25') or 25)
    query = attrs.get('query', '').strip()
    include_history = attrs.get('history', 'false').strip().lower() == 'true'
    if include_history:
        rows = _graph.fact_history(entity, predicate or None, limit=limit)
    elif query:
        rows = _graph.search_facts(query, limit=limit,
                                   entity_keys=[entity],
                                   predicates=[predicate] if predicate else None)
    else:
        rows = _graph.facts_for(entity, predicate or None, limit=limit)
    rows = _filter_facts(rows, query or f"{entity} {predicate}", origin)
    if rows:
        label = "Fact history" if include_history else "Current facts"
        rendered = []
        for row in rows:
            line = _render_fact(row)
            if include_history and row.get('valid_to'):
                line += f" [retired {row['valid_to']}]"
            rendered.append(line)
        res = f"{label} about {entity}:\n" + "\n".join(rendered)
    else:
        res = f"No current facts recorded about '{entity}'."
    execute_actions._last_output = res
    print(f"[Agent] RECALL_FACTS: {len(rows)} fact(s)")
    ui_queue.put(("info", f"> RECALL_FACTS\n\n{res}"))


@action('CLICK', 'DOUBLE_CLICK', 'RIGHT_CLICK', 'CLICK_TEXT', 'MOVE_MOUSE', 'DRAG', 'SCROLL', 'TYPE', 'PRESS', 'HOTKEY', 'LIST_CONTROLS', 'LIST_WINDOWS', 'FOCUS_WINDOW', 'WINDOW_STATE', 'CLOSE_WINDOW', 'LAUNCH_APP', 'MEDIA', 'VOLUME', 'SET_BRIGHTNESS', 'LOCK_SCREEN', 'CLIPBOARD_READ', 'CLIPBOARD_WRITE', 'LIST_PROCESSES', 'SET_FIELD', 'TOGGLE_FIELD', 'SELECT_OPTION', 'READ_FORM', 'FILL_FORM', 'INSPECT_POINT', 'LOCATE_TEXT', 'CLICK_SCREEN_TEXT', family='CLICK')
def _run_click(ctx):
    """Handles CLICK, DOUBLE_CLICK, RIGHT_CLICK, CLICK_TEXT...."""
    action_type = ctx.action_type
    attrs = ctx.attrs
    ui_queue = ctx.ui_queue
    base_dir = ctx.base_dir
    origin = ctx.origin
    turn_id = ctx.turn_id
    interrupt_event = ctx.interrupt_event
    element_map = ctx.element_map
    response_text = ctx.response_text
    import desktop
    expect = attrs.get('expect_window', '')

    def _num(key, default=0):
        try:
            return int(float(attrs.get(key, default)))
        except (TypeError, ValueError):
            return int(default)

    # Variant selection, as a table. Each closure is the
    # body that used to be a rung of an inner ladder.
    chunk = locals().get('chunk')
    clicked = locals().get('clicked')
    detail = locals().get('detail')
    key = locals().get('key')
    kind = locals().get('kind')
    pairs = locals().get('pairs')
    point = locals().get('point')
    res = locals().get('res')
    system_tools = locals().get('system_tools')
    value = locals().get('value')
    def _v_0_click():
        nonlocal chunk, clicked, detail, key, kind, pairs, point, res, system_tools, value
        if attrs.get('name'):
            res = desktop.click_control(
                attrs.get('name'), attrs.get('control_type', ''), expect)
        elif attrs.get('x') and attrs.get('y'):
            res = desktop.click(
                _num('x'), _num('y'),
                button='right' if action_type == 'RIGHT_CLICK' else 'left',
                clicks=2 if action_type == 'DOUBLE_CLICK' else 1,
                expect_window=expect, space=attrs.get('space', 'image'))
        else:
            res = ("[Error] Give name=\"...\" (preferred) or x/y coordinates. "
                   "Use LIST_CONTROLS to see what is clickable.")
    def _v_1_click_text():
        nonlocal chunk, clicked, detail, key, kind, pairs, point, res, system_tools, value
        res = desktop.click_control(
            attrs.get('name', '') or attrs.get('text', ''),
            attrs.get('control_type', ''), expect)
    def _v_2_move_mouse():
        nonlocal chunk, clicked, detail, key, kind, pairs, point, res, system_tools, value
        res = desktop.move_mouse(_num('x'), _num('y'),
                                 attrs.get('space', 'image'))
    def _v_3_inspect_point():
        nonlocal chunk, clicked, detail, key, kind, pairs, point, res, system_tools, value
        res = desktop.inspect_point(_num('x'), _num('y'),
                                    attrs.get('space', 'image'))
    def _find_text_on_screen():
        """Where the text is, shared by locating it and clicking it.

        These two actions had one body that then asked which of them was
        running -- the last place in this file where a handler selected
        behaviour by action name. They share a lookup, not a decision, so the
        lookup is the shared part and each action does its own thing with the
        result.
        """
        nonlocal point, detail
        import system_tools
        point, detail = system_tools.locate_text(
            attrs.get('text', '') or attrs.get('query', ''),
            attrs.get('region', ''))
        return point

    def _v_4_locate_text():
        nonlocal detail, point, res
        if _find_text_on_screen() is None:
            res = detail
            return
        res = detail + ". Click it with CLICK_SCREEN_TEXT, or "
        res += f'CLICK x="{point[0]}" y="{point[1]}" space="screen".'

    def _v_4_click_screen_text():
        nonlocal clicked, detail, point, res
        if _find_text_on_screen() is None:
            res = detail
            return
        # OCR measured this on real pixels, so it is already in
        # screen space and must not be transformed again.
        clicked = desktop.click(point[0], point[1],
                                expect_window=expect, space="screen")
        res = f"{detail}. {clicked}"
    def _v_5_drag():
        nonlocal chunk, clicked, detail, key, kind, pairs, point, res, system_tools, value
        res = desktop.drag(_num('x1'), _num('y1'), _num('x2'), _num('y2'),
                           expect_window=expect,
                           space=attrs.get('space', 'image'),
                           button=attrs.get('button', 'left'),
                           modifiers=attrs.get('modifiers', ''),
                           duration=attrs.get('duration', 0.4),
                           interrupt_event=interrupt_event)
    def _v_6_scroll():
        nonlocal chunk, clicked, detail, key, kind, pairs, point, res, system_tools, value
        res = desktop.scroll(
            _num('amount', -500),
            _num('x') if attrs.get('x') else None,
            _num('y') if attrs.get('y') else None,
            space=attrs.get('space', 'image'))
    def _v_7_type():
        nonlocal chunk, clicked, detail, key, kind, pairs, point, res, system_tools, value
        res = desktop.type_text(attrs.get('text', ''), expect, attrs.get('into', ''))
    def _v_8_press():
        nonlocal chunk, clicked, detail, key, kind, pairs, point, res, system_tools, value
        res = desktop.press_key(attrs.get('key', ''), _num('times', 1), expect)
    def _v_9_hotkey():
        nonlocal chunk, clicked, detail, key, kind, pairs, point, res, system_tools, value
        res = desktop.hotkey(attrs.get('keys', '') or attrs.get('combo', ''), expect)
    def _v_10_set_field():
        nonlocal chunk, clicked, detail, key, kind, pairs, point, res, system_tools, value
        res = desktop.set_value(attrs.get('name', ''), attrs.get('value', ''), expect)
    def _v_11_toggle_field():
        nonlocal chunk, clicked, detail, key, kind, pairs, point, res, system_tools, value
        res = desktop.toggle(
            attrs.get('name', ''),
            attrs.get('checked', 'true').strip().lower() not in {'false', '0', 'no'},
            expect)
    def _v_12_select_option():
        nonlocal chunk, clicked, detail, key, kind, pairs, point, res, system_tools, value
        res = desktop.select_option(attrs.get('name', ''), attrs.get('value', ''), expect)
    def _v_13_read_form():
        nonlocal chunk, clicked, detail, key, kind, pairs, point, res, system_tools, value
        res = desktop.read_form(_num('limit', 40))
    def _v_14_fill_form():
        nonlocal chunk, clicked, detail, key, kind, pairs, point, res, system_tools, value
        # fields="Name=Himanshu | Email=a@b.com | Country:select=India | Agree:check=true"
        pairs = []
        for chunk in str(attrs.get('fields', '')).split('|'):
            chunk = chunk.strip()
            if not chunk or '=' not in chunk:
                continue
            key, value = chunk.split('=', 1)
            kind = 'fill'
            if ':' in key:
                key, kind = key.rsplit(':', 1)
            pairs.append((key.strip(), value.strip(), kind.strip().lower()))
        res = (desktop.fill_form(pairs, expect) if pairs else
               '[Error] FILL_FORM needs fields="Label=value | Other=value".')
    def _v_15_list_controls():
        nonlocal chunk, clicked, detail, key, kind, pairs, point, res, system_tools, value
        res = desktop.list_controls(_num('limit', 40), attrs.get('control_type', ''))
    def _v_16_list_windows():
        nonlocal chunk, clicked, detail, key, kind, pairs, point, res, system_tools, value
        res = desktop.list_windows(_num('limit', 25))
    def _v_17_focus_window():
        nonlocal chunk, clicked, detail, key, kind, pairs, point, res, system_tools, value
        res = desktop.focus_window(attrs.get('title', ''))
    def _v_18_window_state():
        nonlocal chunk, clicked, detail, key, kind, pairs, point, res, system_tools, value
        res = desktop.window_state(attrs.get('title', ''),
                                   attrs.get('state', 'minimize'))
    def _v_19_close_window():
        nonlocal chunk, clicked, detail, key, kind, pairs, point, res, system_tools, value
        res = desktop.close_window(attrs.get('title', ''))
    def _v_20_launch_app():
        nonlocal chunk, clicked, detail, key, kind, pairs, point, res, system_tools, value
        res = desktop.launch(attrs.get('target', '') or attrs.get('name', ''))
    def _v_21_media():
        nonlocal chunk, clicked, detail, key, kind, pairs, point, res, system_tools, value
        res = desktop.media_key(attrs.get('action', 'playpause'))
    def _v_22_volume():
        nonlocal chunk, clicked, detail, key, kind, pairs, point, res, system_tools, value
        if not _volume_requested(_task_request):
            res = ("[Blocked] Volume was not changed because the current "
                   "request did not ask for a loudness change.")
        else:
            res = desktop.volume(attrs.get('direction', 'up'), _num('steps', 5))
    def _v_23_set_brightness():
        nonlocal chunk, clicked, detail, key, kind, pairs, point, res, system_tools, value
        res = desktop.set_brightness(attrs.get('percent', '50'))
    def _v_24_lock_screen():
        nonlocal chunk, clicked, detail, key, kind, pairs, point, res, system_tools, value
        res = desktop.lock_screen()
    def _v_25_clipboard_read():
        nonlocal chunk, clicked, detail, key, kind, pairs, point, res, system_tools, value
        res = desktop.clipboard_read()
    def _v_26_clipboard_write():
        nonlocal chunk, clicked, detail, key, kind, pairs, point, res, system_tools, value
        res = desktop.clipboard_write(attrs.get('text', ''))
    def _v_default():
        nonlocal chunk, clicked, detail, key, kind, pairs, point, res, system_tools, value
        res = desktop.list_processes(_num('limit', 20), attrs.get('filter', ''))
    _variants = {
        'CLICK': _v_0_click,
        'DOUBLE_CLICK': _v_0_click,
        'RIGHT_CLICK': _v_0_click,
        'CLICK_TEXT': _v_1_click_text,
        'MOVE_MOUSE': _v_2_move_mouse,
        'INSPECT_POINT': _v_3_inspect_point,
        'LOCATE_TEXT': _v_4_locate_text,
        'CLICK_SCREEN_TEXT': _v_4_click_screen_text,
        'DRAG': _v_5_drag,
        'SCROLL': _v_6_scroll,
        'TYPE': _v_7_type,
        'PRESS': _v_8_press,
        'HOTKEY': _v_9_hotkey,
        'SET_FIELD': _v_10_set_field,
        'TOGGLE_FIELD': _v_11_toggle_field,
        'SELECT_OPTION': _v_12_select_option,
        'READ_FORM': _v_13_read_form,
        'FILL_FORM': _v_14_fill_form,
        'LIST_CONTROLS': _v_15_list_controls,
        'LIST_WINDOWS': _v_16_list_windows,
        'FOCUS_WINDOW': _v_17_focus_window,
        'WINDOW_STATE': _v_18_window_state,
        'CLOSE_WINDOW': _v_19_close_window,
        'LAUNCH_APP': _v_20_launch_app,
        'MEDIA': _v_21_media,
        'VOLUME': _v_22_volume,
        'SET_BRIGHTNESS': _v_23_set_brightness,
        'LOCK_SCREEN': _v_24_lock_screen,
        'CLIPBOARD_READ': _v_25_clipboard_read,
        'CLIPBOARD_WRITE': _v_26_clipboard_write,
    }
    _variants.get(action_type, _v_default)()

    execute_actions._last_output = res
    print(f"[Agent] {action_type}: {res.splitlines()[0][:120] if res else ''}")
    ui_queue.put(("info", f"> {action_type}\n\n{res}"))


@action('ADD_ROUTINE', 'LIST_ROUTINES', 'DELETE_ROUTINE', 'PAUSE_ROUTINE', 'RESUME_ROUTINE', family='ADD_ROUTINE')
def _run_add_routine(ctx):
    """Handles ADD_ROUTINE, LIST_ROUTINES, DELETE_ROUTINE, PAUSE_ROUTINE...."""
    action_type = ctx.action_type
    attrs = ctx.attrs
    ui_queue = ctx.ui_queue
    base_dir = ctx.base_dir
    origin = ctx.origin
    turn_id = ctx.turn_id
    interrupt_event = ctx.interrupt_event
    element_map = ctx.element_map
    response_text = ctx.response_text
    import routines
    # Variant selection, as a table. Each closure is the
    # body that used to be a rung of an inner ladder.
    res = locals().get('res')
    def _v_0_list_routines():
        nonlocal res
        res = routines.listing(base_dir)
    def _v_1_add_routine():
        nonlocal res
        res = routines.add(base_dir, attrs.get('name', ''),
                           attrs.get('prompt', '') or attrs.get('do', ''),
                           attrs.get('schedule', ''),
                           attrs.get('expires_on', ''))
    def _v_2_delete_routine():
        nonlocal res
        res = routines.remove(base_dir, attrs.get('id', '') or attrs.get('name', ''))
    def _v_default():
        nonlocal res
        res = routines.set_enabled(base_dir,
                                   attrs.get('id', '') or attrs.get('name', ''),
                                   action_type == 'RESUME_ROUTINE')
    _variants = {
        'LIST_ROUTINES': _v_0_list_routines,
        'ADD_ROUTINE': _v_1_add_routine,
        'DELETE_ROUTINE': _v_2_delete_routine,
    }
    _variants.get(action_type, _v_default)()
    ctx.report(res)
    print(f"[Agent] {action_type}: {res.splitlines()[0][:110]}")
    ui_queue.put(("info", f"> {action_type}\n\n{res}"))


@action('GET_WEATHER', family='GET_WEATHER')
def _run_get_weather(ctx):
    """Handles GET_WEATHER."""
    action_type = ctx.action_type
    attrs = ctx.attrs
    ui_queue = ctx.ui_queue
    base_dir = ctx.base_dir
    origin = ctx.origin
    turn_id = ctx.turn_id
    interrupt_event = ctx.interrupt_event
    element_map = ctx.element_map
    response_text = ctx.response_text
    from providers import weather_api
    try:
        horizon = int(attrs.get('days', '0') or 0)
    except ValueError:
        horizon = 0
    res = weather_api.get_weather(
        attrs.get('location', '') or attrs.get('place', ''),
        days=horizon, date=attrs.get('date', ''))
    execute_actions._last_output = res
    print(f"[Agent] GET_WEATHER: {res.splitlines()[0][:110]}")
    ui_queue.put(("info", f"> GET_WEATHER\n\n{res}"))


@action('GET_AIR_QUALITY', family='GET_AIR_QUALITY')
def _run_get_air_quality(ctx):
    from providers.air_quality_api import get_air_quality
    res = get_air_quality(ctx.attrs.get("city", ""), ctx.attrs.get("lat", ""),
                          ctx.attrs.get("lon", ""))
    execute_actions._last_output = res
    print(f"[Agent] GET_AIR_QUALITY: {res.splitlines()[0][:110]}")
    ctx.ui_queue.put(("info", f"> GET_AIR_QUALITY\n\n{res}"))


@action('INDIA_POST_LOOKUP', family='INDIA_POST_LOOKUP')
def _run_india_post_lookup(ctx):
    from providers.india_post_api import lookup
    res = lookup(ctx.attrs.get("pin", ""), ctx.attrs.get("office", ""))
    execute_actions._last_output = res
    print(f"[Agent] INDIA_POST_LOOKUP: {res.splitlines()[0][:110]}")
    ctx.ui_queue.put(("info", f"> INDIA_POST_LOOKUP\n\n{res}"))


@action('SET_TIMER', 'SYSTEM_STATUS', 'KILL_PROCESS', 'TAKE_SCREENSHOT', 'READ_SCREEN_TEXT', family='SET_TIMER')
def _run_set_timer(ctx):
    """Handles SET_TIMER, SYSTEM_STATUS, KILL_PROCESS, TAKE_SCREENSHOT...."""
    action_type = ctx.action_type
    attrs = ctx.attrs
    ui_queue = ctx.ui_queue
    base_dir = ctx.base_dir
    origin = ctx.origin
    turn_id = ctx.turn_id
    interrupt_event = ctx.interrupt_event
    element_map = ctx.element_map
    response_text = ctx.response_text
    import system_tools
    # Variant selection, as a table. Each closure is the
    # body that used to be a rung of an inner ladder.
    res = locals().get('res')
    def _v_0_set_timer():
        nonlocal res
        duration_text = attrs.get('duration', '') or attrs.get('in', '')
        res = system_tools.set_timer(
            base_dir,
            duration_text,
            attrs.get('label', ''))
        if str(res).startswith('[Success]') and str(
                attrs.get('display', '')).strip().lower() in {
                    'true', 'yes', '1', 'on'}:
            seconds = system_tools.parse_duration(duration_text)
            if seconds:
                ui_queue.put(('show_widget', {
                    'id': 'timer', 'kind': 'timer',
                    'title': attrs.get('widget_title') or attrs.get('label') or 'Timer',
                    'duration_seconds': seconds,
                    'value': attrs.get('label') or 'Remaining',
                }))
    def _v_1_system_status():
        nonlocal res
        res = system_tools.system_status()
        from performance_metrics import summary as timing_summary
        timings = timing_summary()
        if timings:
            res += '\nRecent execution timings (bounded sample):\n' + '\n'.join(
                f"{row['kind']} {row['name']}: {row['mean_ms']:.0f} ms average, "
                f"{row['p95_ms']:.0f} ms p95, {row['count']} samples"
                for row in timings[-8:])
    def _v_2_kill_process():
        nonlocal res
        res = system_tools.kill_process(
            attrs.get('name', '') or attrs.get('pid', ''),
            confirmed=attrs.get('confirmed', '').strip().lower() == 'true')
    def _v_3_take_screenshot():
        nonlocal res
        res = system_tools.take_screenshot(
            base_dir, attrs.get('path', ''), attrs.get('region', ''))
        if str(res).startswith("[Success]"):
            match = re.search(
                r"Screenshot saved to (.+?) \((\d+)x(\d+)\)\.?$", str(res))
            if match:
                import artifact_context
                artifact_context.record(
                    match.group(1), kind="screenshot",
                    source_action="TAKE_SCREENSHOT",
                    metadata={"width": int(match.group(2)),
                              "height": int(match.group(3))})
    def _v_default():
        nonlocal res
        res = system_tools.read_screen_text(attrs.get('region', ''))
        # Raw OCR is evidence for the active reasoning turn, not a user-facing
        # document.  The model must synthesize what matters before anything is
        # displayed or spoken.
    _variants = {
        'SET_TIMER': _v_0_set_timer,
        'SYSTEM_STATUS': _v_1_system_status,
        'KILL_PROCESS': _v_2_kill_process,
        'TAKE_SCREENSHOT': _v_3_take_screenshot,
    }
    _variants.get(action_type, _v_default)()
    ctx.report(res)
    print(f"[Agent] {action_type}: {res.splitlines()[0][:110]}")
    ui_queue.put(("info", f"> {action_type}\n\n{res[:2000]}"))


@action('MUSIC_PLAY', 'MUSIC_PAUSE', 'MUSIC_NEXT', 'MUSIC_PREVIOUS', 'MUSIC_SEARCH', 'MUSIC_NOW_PLAYING', family='MUSIC_PLAY')
def _run_music_play(ctx):
    """Handles MUSIC_PLAY, MUSIC_PAUSE, MUSIC_NEXT, MUSIC_PREVIOUS...."""
    action_type = ctx.action_type
    attrs = ctx.attrs
    ui_queue = ctx.ui_queue
    base_dir = ctx.base_dir
    origin = ctx.origin
    turn_id = ctx.turn_id
    interrupt_event = ctx.interrupt_event
    element_map = ctx.element_map
    response_text = ctx.response_text
    from providers import ytmusic_api
    # Variant selection, as a table. Each closure is the
    # body that used to be a rung of an inner ladder.
    res = locals().get('res')
    def _v_0_music_play():
        nonlocal res
        res = ytmusic_api.play(attrs.get('query', '') or attrs.get('song', ''))
    def _v_1_music_pause():
        nonlocal res
        res = ytmusic_api.pause()
    def _v_2_music_next():
        nonlocal res
        res = ytmusic_api.skip('next')
    def _v_3_music_previous():
        nonlocal res
        res = ytmusic_api.skip('previous')
    def _v_4_music_search():
        nonlocal res
        res = ytmusic_api.search(attrs.get('query', ''),
                                 limit=int(attrs.get('limit', '5') or 5))
    def _v_default():
        nonlocal res
        res = ytmusic_api.now_playing()
    _variants = {
        'MUSIC_PLAY': _v_0_music_play,
        'MUSIC_PAUSE': _v_1_music_pause,
        'MUSIC_NEXT': _v_2_music_next,
        'MUSIC_PREVIOUS': _v_3_music_previous,
        'MUSIC_SEARCH': _v_4_music_search,
    }
    _variants.get(action_type, _v_default)()
    execute_actions._last_output = res
    print(f"[Agent] {action_type}: {res.splitlines()[0][:110]}")
    ui_queue.put(("info", f"> {action_type}\n\n{res}"))


@action('SPOTIFY_NOW_PLAYING', 'SPOTIFY_SEARCH', 'SPOTIFY_VOLUME', 'SPOTIFY_QUEUE', family='SPOTIFY_NOW_PLAYING')
def _run_spotify_now_playing(ctx):
    """Handles SPOTIFY_NOW_PLAYING, SPOTIFY_SEARCH, SPOTIFY_VOLUME, SPOTIFY_QUEUE."""
    action_type = ctx.action_type
    attrs = ctx.attrs
    ui_queue = ctx.ui_queue
    base_dir = ctx.base_dir
    origin = ctx.origin
    turn_id = ctx.turn_id
    interrupt_event = ctx.interrupt_event
    element_map = ctx.element_map
    response_text = ctx.response_text
    from providers import spotify_api
    # Variant selection, as a table. Each closure is the
    # body that used to be a rung of an inner ladder.
    res = locals().get('res')
    def _v_0_spotify_now_playing():
        nonlocal res
        res = spotify_api.spotify_now_playing(base_dir)
    def _v_1_spotify_search():
        nonlocal res
        res = spotify_api.spotify_search(
            base_dir, attrs.get('query', ''),
            kind=attrs.get('kind', 'track'),
            limit=int(attrs.get('limit', '5') or 5))
    def _v_2_spotify_volume():
        nonlocal res
        res = spotify_api.spotify_volume(base_dir, attrs.get('percent', ''))
    def _v_default():
        nonlocal res
        res = spotify_api.spotify_queue(base_dir, attrs.get('uri', ''))
    _variants = {
        'SPOTIFY_NOW_PLAYING': _v_0_spotify_now_playing,
        'SPOTIFY_SEARCH': _v_1_spotify_search,
        'SPOTIFY_VOLUME': _v_2_spotify_volume,
    }
    _variants.get(action_type, _v_default)()
    execute_actions._last_output = res
    print(f"[Agent] {action_type}: {res.splitlines()[0][:110]}")
    ui_queue.put(("info", f"> {action_type}\n\n{res}"))


@action('SEND_TELEGRAM', family='SEND_TELEGRAM')
def _run_send_telegram(ctx):
    """Handles SEND_TELEGRAM."""
    action_type = ctx.action_type
    attrs = ctx.attrs
    ui_queue = ctx.ui_queue
    base_dir = ctx.base_dir
    origin = ctx.origin
    turn_id = ctx.turn_id
    interrupt_event = ctx.interrupt_event
    element_map = ctx.element_map
    response_text = ctx.response_text
    # Registered and confirmation-gated for a long time, but there
    # was no handler at all: the action silently did nothing.
    from providers.telegram_bot import send_telegram_message
    text = attrs.get('text', '') or attrs.get('message', '')
    if not text:
        res = "[Error] SEND_TELEGRAM needs text."
    else:
        accepted = send_telegram_message(text)
        res = (f"[Success] Telegram accepted the message for the paired "
               f"owner; characters={len(text)}."
               if accepted else
               "[Error] Telegram did not accept the message.")
    execute_actions._last_output = res
    print(f"[Agent] SEND_TELEGRAM: {res[:110]}")
    ui_queue.put(("info", f"> SEND_TELEGRAM\n\n{res}"))


@action('SEND_TELEGRAM_FILE', family='telegram_files')
def _run_send_telegram_file(ctx):
    """Send one exact local file to the paired Telegram owner."""
    from providers.telegram_bot import send_telegram_document
    result = send_telegram_document(
        ctx.attr('path') or ctx.attr('file_path'), ctx.attr('caption'))
    ctx.report(result)
    ctx.say(f"> SEND_TELEGRAM_FILE\n\n{result}")


@action('LIST_TELEGRAM_FILES', family='telegram_files')
def _run_list_telegram_files(ctx):
    """List files previously received and saved from Telegram."""
    from providers.telegram_bot import list_received_files
    try:
        limit = int(ctx.attr('limit') or 10)
    except (TypeError, ValueError):
        limit = 10
    result = list_received_files(ctx.base_dir, limit=limit)
    ctx.report(result)
    ctx.say(f"> LIST_TELEGRAM_FILES\n\n{result}")


@action('FIND_FILES', family='FIND_FILES')
def _run_find_files(ctx):
    """Handles FIND_FILES."""
    action_type = ctx.action_type
    attrs = ctx.attrs
    ui_queue = ctx.ui_queue
    base_dir = ctx.base_dir
    origin = ctx.origin
    turn_id = ctx.turn_id
    interrupt_event = ctx.interrupt_event
    element_map = ctx.element_map
    response_text = ctx.response_text
    import documents
    res = documents.find_files(
        attrs.get('pattern', '') or attrs.get('name', '') or attrs.get('query', ''),
        root=attrs.get('root', '') or attrs.get('path', ''),
        limit=int(attrs.get('limit', '25') or 25))
    execute_actions._last_output = res
    print(f"[Agent] FIND_FILES: {res.splitlines()[0][:110]}")
    ui_queue.put(("info", f"> FIND_FILES\n\n{res}"))


@action('LIST_DIRECTORY', family='LIST_DIRECTORY')
def _run_list_directory(ctx):
    """Handles LIST_DIRECTORY."""
    action_type = ctx.action_type
    attrs = ctx.attrs
    ui_queue = ctx.ui_queue
    base_dir = ctx.base_dir
    origin = ctx.origin
    turn_id = ctx.turn_id
    interrupt_event = ctx.interrupt_event
    element_map = ctx.element_map
    response_text = ctx.response_text
    raw_path = os.path.expandvars(os.path.expanduser(
        attrs.get('path', '').strip()))
    # On Windows, "D:" means the process's remembered directory
    # on that drive, not its root. Users invariably mean D:\ here.
    if re.fullmatch(r'[A-Za-z]:', raw_path):
        raw_path += os.sep
    folder = os.path.abspath(raw_path) if raw_path else ''
    try:
        limit = max(1, min(int(attrs.get('limit', '200') or 200), 500))
    except (TypeError, ValueError):
        limit = 200
    include_hidden = str(attrs.get('include_hidden', 'false')).lower() == 'true'
    if not folder or not os.path.isdir(folder):
        res = f"[Error] Directory does not exist: {folder or raw_path!r}."
    else:
        try:
            entries = []
            with os.scandir(folder) as scan:
                for entry in scan:
                    if not include_hidden and entry.name.startswith('.'):
                        continue
                    kind = "Folder" if entry.is_dir(follow_symlinks=False) else "File"
                    entries.append((kind != "Folder", entry.name.casefold(), kind, entry.name))
            entries.sort()
            shown = entries[:limit]
            folders = sum(1 for row in entries if row[2] == "Folder")
            files = len(entries) - folders
            lines = [f"Contents of {folder}",
                     f"{folders} folder(s), {files} file(s)", ""]
            lines.extend(f"[{kind}] {name}" for _, _, kind, name in shown)
            if len(entries) > limit:
                lines.append(f"...and {len(entries) - limit} more item(s).")
            res = "\n".join(lines)
        except OSError as exc:
            res = f"[Error] Could not list {folder}: {exc}"
    execute_actions._last_output = res
    print(f"[Agent] LIST_DIRECTORY: {res.splitlines()[0][:110]}")
    ui_queue.put(("info", f"> LIST_DIRECTORY\n\n{res}"))


@action('SEARCH_DOCS', family='SEARCH_DOCS')
def _run_search_docs(ctx):
    """Handles SEARCH_DOCS."""
    action_type = ctx.action_type
    attrs = ctx.attrs
    ui_queue = ctx.ui_queue
    base_dir = ctx.base_dir
    origin = ctx.origin
    turn_id = ctx.turn_id
    interrupt_event = ctx.interrupt_event
    element_map = ctx.element_map
    response_text = ctx.response_text
    import documents
    res = documents.search_documents(
        attrs.get('query', '') or attrs.get('q', ''),
        limit=int(attrs.get('limit', '6') or 6))
    execute_actions._last_output = res
    print(f"[Agent] SEARCH_DOCS: {res.splitlines()[0][:110]}")
    ui_queue.put(("info", f"> SEARCH_DOCS\n\n{res}"))


@action('INDEX_FOLDER', family='INDEX_FOLDER')
def _run_index_folder(ctx):
    """Handles INDEX_FOLDER."""
    action_type = ctx.action_type
    attrs = ctx.attrs
    ui_queue = ctx.ui_queue
    base_dir = ctx.base_dir
    origin = ctx.origin
    turn_id = ctx.turn_id
    interrupt_event = ctx.interrupt_event
    element_map = ctx.element_map
    response_text = ctx.response_text
    import documents
    res = documents.index_folder(attrs.get('path', ''),
                                 recursive=attrs.get('recursive', 'true').strip().lower() != 'false')
    execute_actions._last_output = res
    print(f"[Agent] INDEX_FOLDER: {res}")
    ui_queue.put(("info", f"> INDEX_FOLDER\n\n{res}"))


@action('INGEST_PATH', family='INGEST_PATH')
def _run_ingest_path(ctx):
    """Handles INGEST_PATH."""
    action_type = ctx.action_type
    attrs = ctx.attrs
    ui_queue = ctx.ui_queue
    base_dir = ctx.base_dir
    origin = ctx.origin
    turn_id = ctx.turn_id
    interrupt_event = ctx.interrupt_event
    element_map = ctx.element_map
    response_text = ctx.response_text
    import documents
    res = documents.ingest_path(
        attrs.get('path', ''),
        recursive=attrs.get('recursive', 'true').strip().lower() != 'false')
    execute_actions._last_output = res
    print(f"[Agent] INGEST_PATH: {res.splitlines()[0][:160]}")
    ui_queue.put(("info", f"> INGEST_PATH\n\n{res}"))


@action('READ_DOCUMENT', 'LIST_DOCUMENTS', family='READ_DOCUMENT')
def _run_read_document(ctx):
    """Handles READ_DOCUMENT, LIST_DOCUMENTS."""
    action_type = ctx.action_type
    attrs = ctx.attrs
    ui_queue = ctx.ui_queue
    base_dir = ctx.base_dir
    origin = ctx.origin
    turn_id = ctx.turn_id
    interrupt_event = ctx.interrupt_event
    element_map = ctx.element_map
    response_text = ctx.response_text
    import documents
    # Variant selection, as a table. Each closure is the
    # body that used to be a rung of an inner ladder.
    _opt = locals().get('_opt')
    res = locals().get('res')
    def _v_0_list_documents():
        nonlocal _opt, res
        res = documents.list_documents(base_dir)
    def _v_default():
        nonlocal _opt, res
        def _opt(key):
            value = attrs.get(key, '')
            try:
                return int(value) if value else None
            except (TypeError, ValueError):
                return None
        res = documents.read_document(
            base_dir,
            attrs.get('name', '') or attrs.get('file', '') or attrs.get('path', ''),
            first=_opt('first') or _opt('page') or _opt('slide'),
            last=_opt('last') or _opt('page') or _opt('slide'),
            max_chars=_opt('max_chars') or documents.MAX_CHARS_DEFAULT,
            outline=attrs.get('outline', '').strip().lower() == 'true',
        )
        # Studying happens on the dashboard, not in the speech channel.
        if _classify_output(res):
            source_name = (attrs.get('name', '') or attrs.get('file', '')
                           or attrs.get('path', '') or "Document")
            set_direct_user_delivery(
                res, "I've put the document on your dashboard, sir.",
                surface="dashboard",
                title=os.path.basename(str(source_name)) or "Document")
    _variants = {
        'LIST_DOCUMENTS': _v_0_list_documents,
    }
    _variants.get(action_type, _v_default)()
    execute_actions._last_output = res
    print(f"[Agent] {action_type}: {res.splitlines()[0][:110]}")
    ui_queue.put(("info", f"> {action_type}\n\n{res}"))


@action('BOARD_WRITE', 'BOARD_DIAGRAM', 'BOARD_MATH', 'BOARD_PLOT', 'BOARD_CHART', 'BOARD_TABLE', 'BOARD_RULE', 'BOARD_SECTION', 'BOARD_CLEAR', 'BOARD_HIDE', 'BOARD_GOTO', 'BOARD_PAGES', 'BOARD_EDIT_PAGE', 'BOARD_ANIMATE', 'BOARD_PLOT3D', 'BOARD_MATRIX', 'BOARD_FIELD', 'BOARD_CONTOUR', 'BOARD_CAPTURE', family='BOARD_WRITE')
def _run_board_write(ctx):
    """Handles BOARD_WRITE, BOARD_DIAGRAM, BOARD_MATH, BOARD_PLOT...."""
    action_type = ctx.action_type
    attrs = ctx.attrs
    ui_queue = ctx.ui_queue
    base_dir = ctx.base_dir
    origin = ctx.origin
    turn_id = ctx.turn_id
    interrupt_event = ctx.interrupt_event
    element_map = ctx.element_map
    response_text = ctx.response_text
    # Variant selection, as a table. Each closure is the
    # body that used to be a rung of an inner ladder.
    blackboard = locals().get('blackboard')
    body = locals().get('body')
    expr = locals().get('expr')
    key = locals().get('key')
    kind = locals().get('kind')
    latex = locals().get('latex')
    needs = locals().get('needs')
    res = locals().get('res')
    spec = locals().get('spec')
    state = locals().get('state')
    style = locals().get('style')
    text = locals().get('text')
    title = locals().get('title')
    where = locals().get('where')
    written = locals().get('written')
    def _v_0_board_clear():
        nonlocal blackboard, body, expr, key, kind, latex, needs, res, spec, state, style, text, title, where, written
        ui_queue.put(("board_clear", ""))
        res = "[Success] Board wiped. It stays open for the next point."
    def _v_1_board_hide():
        nonlocal blackboard, body, expr, key, kind, latex, needs, res, spec, state, style, text, title, where, written
        ui_queue.put(("board_hide", ""))
        res = "[Success] Board closed."
    def _v_2_board_rule():
        nonlocal blackboard, body, expr, key, kind, latex, needs, res, spec, state, style, text, title, where, written
        ui_queue.put(("board_rule", {}))
        res = "[Success] Drew a rule."
    def _v_3_board_section():
        nonlocal blackboard, body, expr, key, kind, latex, needs, res, spec, state, style, text, title, where, written
        title = attrs.get('title', '') or attrs.get('text', '')
        # Turning the page is also when the previous one is
        # finished, which is the moment to put it in the graph.
        ui_queue.put(("board_ingest", attrs.get('topic', '')))
        ui_queue.put(("board_section", {"title": title}))
        res = f"[Success] New page: {title or 'untitled'}."
    def _v_4_board_goto():
        nonlocal blackboard, body, expr, key, kind, latex, needs, res, spec, state, style, text, title, where, written
        ui_queue.put(("board_goto", {"page": attrs.get('page', '1')}))
        res = f"[Success] Showing page {attrs.get('page', '1')}."
    def _v_5_board_pages():
        nonlocal blackboard, body, expr, key, kind, latex, needs, res, spec, state, style, text, title, where, written
        # This used to answer "it is printed to the activity log",
        # which the model cannot read -- so the one action for
        # checking what had already been taught taught it nothing,
        # and the lesson repeated pages. The board now mirrors its
        # own state, so the outline comes back here.
        ui_queue.put(("board_outline", ""))
        import blackboard
        state = blackboard.board_state()
        if not state.get("count"):
            res = "[Success] The board is empty; nothing taught yet."
        else:
            res = (f"[Success] {state['count']} page(s) taught so "
                   f"far, showing page {state['page']}:\n"
                   f"{state['outline']}")
    def _v_6_board_edit_page():
        nonlocal blackboard, body, expr, key, kind, latex, needs, res, spec, state, style, text, title, where, written
        body = _tag_body(response_text, 'BOARD_EDIT_PAGE')
        ui_queue.put(("board_edit_page", {
            "page": attrs.get('page', 'current'),
            "mode": attrs.get('mode', 'append'),
        }))
        if body:
            _board_lines(ui_queue, body, attrs.get('style', ''),
                         attrs.get('color', ''))
        res = (f"[Success] Edited page {attrs.get('page', 'the current one')} "
               "and brought it back up.")
    def _v_7_board_capture():
        nonlocal blackboard, body, expr, key, kind, latex, needs, res, spec, state, style, text, title, where, written
        ui_queue.put(("board_capture", {
            "region": attrs.get('region', ''),
            "window": attrs.get('window', ''),
            "caption": attrs.get('caption', ''),
        }))
        where = (attrs.get('window') or attrs.get('region')
                 or "the whole screen")
        res = (f"[Success] Put {where} on the board. The board hides "
               "itself for the grab, so it photographs what is "
               "underneath rather than itself.")
    def _v_8_board_plot3d():
        nonlocal blackboard, body, expr, key, kind, latex, needs, res, spec, state, style, text, title, where, written
        spec = {key: attrs.get(key, '') for key in (
            'kind', 'expr', 'x', 'y', 'z', 'values', 'xmin', 'xmax',
            'ymin', 'ymax', 'tmin', 'tmax', 'xlabel', 'ylabel',
            'zlabel', 'title', 'cmap', 'elev', 'azim', 'rotate',
            'frames', 'contours')}
        kind = (spec.get('kind') or 'surface').lower()
        needs = {'parametric': 'x', 'curve': 'x', 'line': 'x',
                 'scatter': 'values', 'points': 'values'}.get(kind, 'expr')
        if not spec.get(needs):
            res = (f'[Error] A {kind} 3D plot needs {needs}="...". '
                   'See the teaching catalogue.')
        else:
            ui_queue.put(("board_plot3d", spec))
            res = f"[Success] Drew a 3D {kind} on the board."
    def _v_9_board_matrix():
        nonlocal blackboard, body, expr, key, kind, latex, needs, res, spec, state, style, text, title, where, written
        if not attrs.get('values'):
            res = ('[Error] BOARD_MATRIX needs values="1,2|3,4", '
                   'pipes between rows and commas between cells.')
        else:
            ui_queue.put(("board_matrix", {key: attrs.get(key, '') for key in (
                'values', 'rows', 'columns', 'title', 'cmap', 'annotate')}))
            res = "[Success] Drew that matrix on the board."
    def _v_10_board_field():
        nonlocal blackboard, body, expr, key, kind, latex, needs, res, spec, state, style, text, title, where, written
        if not (attrs.get('fx') or attrs.get('fy')):
            res = ('[Error] BOARD_FIELD needs fx="..." and fy="...", '
                   'the two components of the vector at (x, y).')
        else:
            ui_queue.put(("board_field", {key: attrs.get(key, '') for key in (
                'fx', 'fy', 'potential', 'xmin', 'xmax', 'ymin', 'ymax',
                'density', 'scale', 'xlabel', 'ylabel', 'title', 'cmap',
                'arrow_cmap')}))
            res = "[Success] Drew that vector field on the board."
    def _v_11_board_contour():
        nonlocal blackboard, body, expr, key, kind, latex, needs, res, spec, state, style, text, title, where, written
        if not attrs.get('expr'):
            res = '[Error] BOARD_CONTOUR needs expr="x**2 + y**2".'
        else:
            ui_queue.put(("board_contour", {key: attrs.get(key, '') for key in (
                'expr', 'path', 'xmin', 'xmax', 'ymin', 'ymax',
                'title', 'cmap')}))
            res = "[Success] Drew those level sets on the board."
    def _v_12_board_animate():
        nonlocal blackboard, body, expr, key, kind, latex, needs, res, spec, state, style, text, title, where, written
        spec = {key: attrs.get(key, '') for key in (
            'kind', 'expr', 'labels', 'xmin', 'xmax', 'ymax', 'speed',
            'steps', 'tokens', 'dwell', 'caption', 'height')}
        kind = (spec.get('kind') or 'wave').lower()
        needs = {'flow': 'steps', 'pipeline': 'steps',
                 'attention': 'tokens'}.get(kind, 'expr')
        if not spec.get(needs):
            res = (f'[Error] A {kind} animation needs {needs}="...". '
                   'See the teaching catalogue.')
        else:
            ui_queue.put(("board_animate", spec))
            res = f"[Success] Animating {kind} on the board."
    def _v_13_board_math():
        nonlocal blackboard, body, expr, key, kind, latex, needs, res, spec, state, style, text, title, where, written
        latex = _tag_body(response_text, 'BOARD_MATH') or attrs.get('latex', '')
        if not latex.strip():
            res = ('[Error] BOARD_MATH needs LaTeX, either in the tag '
                   'body or latex="...". Example: \\frac{a}{b}')
        else:
            ui_queue.put(("board_math", {
                "latex": latex, "size": attrs.get('size', 30),
                "caption": attrs.get('caption', ''),
                "align": attrs.get('align', 'center'),
                "color": attrs.get('color', ''),
            }))
            res = "[Success] Set that formula on the board."
    def _v_14_board_plot():
        nonlocal blackboard, body, expr, key, kind, latex, needs, res, spec, state, style, text, title, where, written
        expr = attrs.get('expr', '') or attrs.get('y', '')
        if not expr.strip():
            res = ('[Error] BOARD_PLOT needs expr="sin(x)". Separate '
                   'several curves with a semicolon.')
        else:
            ui_queue.put(("board_plot", {
                "expr": expr, "xmin": attrs.get('xmin', -10),
                "xmax": attrs.get('xmax', 10),
                "ymin": attrs.get('ymin'), "ymax": attrs.get('ymax'),
                "labels": attrs.get('labels', ''),
                "xlabel": attrs.get('xlabel', 'x'),
                "ylabel": attrs.get('ylabel', 'y'),
                "title": attrs.get('title', ''),
            }))
            res = f"[Success] Plotted {expr} on the board."
    def _v_15_board_chart():
        nonlocal blackboard, body, expr, key, kind, latex, needs, res, spec, state, style, text, title, where, written
        if not attrs.get('labels') or not attrs.get('values'):
            res = ('[Error] BOARD_CHART needs labels="a,b,c" and a '
                   'matching values="1,2,3".')
        else:
            ui_queue.put(("board_chart", {
                "kind": attrs.get('kind', 'bar'),
                "labels": attrs.get('labels', ''),
                "values": attrs.get('values', ''),
                "title": attrs.get('title', ''),
            }))
            res = "[Success] Drew that chart on the board."
    def _v_16_board_table():
        nonlocal blackboard, body, expr, key, kind, latex, needs, res, spec, state, style, text, title, where, written
        if not attrs.get('rows'):
            res = ('[Error] BOARD_TABLE needs rows="a,b|c,d", with '
                   'pipes between rows and commas between cells.')
        else:
            ui_queue.put(("board_table", {
                "headers": attrs.get('headers', ''),
                "rows": attrs.get('rows', ''),
                "color": attrs.get('color', ''),
                "title": attrs.get('title', ''),
            }))
            res = "[Success] Drew that table on the board."
    def _v_17_board_diagram():
        nonlocal blackboard, body, expr, key, kind, latex, needs, res, spec, state, style, text, title, where, written
        spec = {key: attrs.get(key, '') for key in (
            'layers', 'labels', 'steps', 'items', 'nodes', 'events',
            'left', 'right', 'left_items', 'right_items', 'overlap',
            'forces', 'body', 'headers', 'rows', 'title', 'direction')}
        spec['kind'] = attrs.get('kind', 'flow')
        spec['color'] = attrs.get('color', '')
        needs = {
            'flow': 'steps', 'flowchart': 'steps', 'steps': 'steps',
            'boxes': 'steps', 'cycle': 'steps', 'loop': 'steps',
            'tree': 'nodes', 'hierarchy': 'nodes',
            'timeline': 'events', 'forces': 'forces',
            'freebody': 'forces', 'table': 'rows',
        }.get(spec['kind'])
        if needs and not (spec.get(needs) or spec.get('items')):
            res = (f'[Error] A {spec["kind"]} diagram needs {needs}="...". '
                   'See the teaching catalogue for the format.')
        else:
            ui_queue.put(("board_diagram", spec))
            res = f"[Success] Drew a {spec['kind']} diagram on the board."
    def _v_default():
        nonlocal blackboard, body, expr, key, kind, latex, needs, res, spec, state, style, text, title, where, written
        text = _tag_body(response_text, 'BOARD_WRITE') or attrs.get('text', '')
        if not text:
            res = "[Error] BOARD_WRITE needs text, either in the tag body or text=\"...\"."
        else:
            style = (attrs.get('style', '') or '').lower()
            written = _board_lines(ui_queue, text, style,
                                   attrs.get('color', ''))
            res = (f"[Success] Wrote {written} line(s) to the board. "
                   "It scrolls automatically; keep teaching.")
    _variants = {
        'BOARD_CLEAR': _v_0_board_clear,
        'BOARD_HIDE': _v_1_board_hide,
        'BOARD_RULE': _v_2_board_rule,
        'BOARD_SECTION': _v_3_board_section,
        'BOARD_GOTO': _v_4_board_goto,
        'BOARD_PAGES': _v_5_board_pages,
        'BOARD_EDIT_PAGE': _v_6_board_edit_page,
        'BOARD_CAPTURE': _v_7_board_capture,
        'BOARD_PLOT3D': _v_8_board_plot3d,
        'BOARD_MATRIX': _v_9_board_matrix,
        'BOARD_FIELD': _v_10_board_field,
        'BOARD_CONTOUR': _v_11_board_contour,
        'BOARD_ANIMATE': _v_12_board_animate,
        'BOARD_MATH': _v_13_board_math,
        'BOARD_PLOT': _v_14_board_plot,
        'BOARD_CHART': _v_15_board_chart,
        'BOARD_TABLE': _v_16_board_table,
        'BOARD_DIAGRAM': _v_17_board_diagram,
    }
    _variants.get(action_type, _v_default)()
    ctx.report(res)
    print(f"[Agent] {action_type}: {res}")


@action('CONTROL_CENTER', 'SHOW_CODE_PANEL', 'DASHBOARD_SHOW_TEXT', 'ADD_PANEL', 'UPDATE_PANEL', 'REMOVE_PANEL', 'LIST_PANELS', 'SET_PANEL_VALUE', family='CONTROL_CENTER')
def _run_control_center(ctx):
    """Handles CONTROL_CENTER, ADD_PANEL, UPDATE_PANEL, REMOVE_PANEL...."""
    action_type = ctx.action_type
    attrs = ctx.attrs
    ui_queue = ctx.ui_queue
    base_dir = ctx.base_dir
    origin = ctx.origin
    turn_id = ctx.turn_id
    interrupt_event = ctx.interrupt_event
    element_map = ctx.element_map
    response_text = ctx.response_text
    import panels as panel_store
    base = os.path.dirname(os.path.abspath(__file__))
    try:
        # Variant selection, as a table. Each closure is the
        # body that used to be a rung of an inner ladder.
        action_word = locals().get('action_word')
        body = locals().get('body')
        key = locals().get('key')
        operation = locals().get('operation')
        page = locals().get('page')
        panel = locals().get('panel')
        removed = locals().get('removed')
        res = locals().get('res')
        spec = locals().get('spec')
        def _v_0_control_center():
            nonlocal action_word, body, key, operation, page, panel, removed, res, spec
            page = (attrs.get('page', '') or '').strip().title()
            if page == 'Graph':
                page = 'Brain'
            operation = (attrs.get('action', '') or
                         ('page' if page else 'open')).lower()
            if operation in ('close', 'hide'):
                ui_queue.put(("hide_dashboard", ""))
                res = "[Success] Control Center closed."
            elif page:
                ui_queue.put(("dashboard_page", page))
                res = f"[Success] Control Center open on {page}."
            else:
                ui_queue.put(("show_dashboard", ""))
                res = "[Success] Control Center open."
        def _v_1_list_panels():
            nonlocal action_word, body, key, operation, page, panel, removed, res, spec
            res = panel_store.describe(base)
        def _v_0a_show_code_panel():
            nonlocal res
            ui_queue.put(("dashboard_page", "Code"))
            res = "[Success] Control Center open on Code."
        def _v_1a_dashboard_show_text():
            nonlocal body, res
            body = _tag_body(response_text, 'DASHBOARD_SHOW_TEXT')
            if not body:
                body = str(attrs.get('content', '') or '')
            if not body.strip():
                res = "[Error] DASHBOARD_SHOW_TEXT needs content to display."
                return
            ui_queue.put(("dashboard_document", {
                "title": attrs.get('title', '') or "VANTA Output",
                "language": attrs.get('language', '') or attrs.get('type_hint', ''),
                "content": body,
            }))
            res = (f"[Success] Dashboard is open on Output with "
                   f"{len(body.splitlines())} line(s) visibly rendered.")
        def _v_2_remove_panel():
            nonlocal action_word, body, key, operation, page, panel, removed, res, spec
            removed = panel_store.remove(base, attrs.get('id', '') or
                                         attrs.get('title', ''))
            ui_queue.put(("panels_changed", ""))
            res = f"[Success] Removed the panel '{removed['title']}'."
        def _v_3_set_panel_value():
            nonlocal action_word, body, key, operation, page, panel, removed, res, spec
            panel = panel_store.set_value(
                base, attrs.get('id', '') or attrs.get('panel', ''),
                attrs.get('label', ''), attrs.get('value'),
                attrs.get('detail'), attrs.get('ratio'))
            ui_queue.put(("panels_changed", ""))
            res = f"[Success] Updated '{attrs.get('label')}' on {panel['title']}."
        def _v_default():
            nonlocal action_word, body, key, operation, page, panel, removed, res, spec
            body = _tag_body(response_text, action_type)
            spec = {}
            if body:
                spec = json.loads(body)
            spec.setdefault('title', attrs.get('title', ''))
            for key in ('note', 'accent', 'id'):
                if attrs.get(key):
                    spec.setdefault(key, attrs.get(key))
            if attrs.get('pinned'):
                spec.setdefault('pinned',
                                str(attrs['pinned']).lower() == 'true')
            if action_type == 'UPDATE_PANEL':
                panel = panel_store.update(
                    base, spec.get('id') or attrs.get('id', ''), spec)
                action_word = "Updated"
            else:
                panel, action_word = panel_store.add(base, spec)
            ui_queue.put(("panels_changed", ""))
            res = (f"[Success] {action_word} the panel "
                   f"'{panel['title']}' [{panel['id']}] with "
                   f"{len(panel['items'])} items. It is on the "
                   "Workspace page of the Control Center.")
        _variants = {
            'CONTROL_CENTER': _v_0_control_center,
            'SHOW_CODE_PANEL': _v_0a_show_code_panel,
            'DASHBOARD_SHOW_TEXT': _v_1a_dashboard_show_text,
            'LIST_PANELS': _v_1_list_panels,
            'REMOVE_PANEL': _v_2_remove_panel,
            'SET_PANEL_VALUE': _v_3_set_panel_value,
        }
        _variants.get(action_type, _v_default)()
    except json.JSONDecodeError as exc:
        res = (f"[Error] The panel body must be valid JSON: {exc}. "
               'Example: {"title":"X","items":[{"kind":"stat",'
               '"label":"Done","value":"3"}]}')
    except ValueError as exc:
        res = f"[Error] {exc}"
    except Exception as exc:
        res = f"[Error] Panel operation failed: {exc}"
    ctx.report(res)
    print(f"[Agent] {action_type}: {res.splitlines()[0][:140]}")
    ui_queue.put(("info", f"> {action_type}\n\n{res}"))


@action('RESULT_WINDOW', family='RESULT_WINDOW')
def _run_result_window(ctx):
    """Show, hide or replace VANTA's durable rich result surface."""
    operation = str(ctx.attrs.get('action') or 'show').strip().lower()
    if operation in {'hide', 'minimize', 'inactive'}:
        ctx.ui_queue.put(('hide_result', ''))
        ctx.report('[Success] Result window is hidden; its current content is preserved.')
        return
    if operation in {'close', 'clear'}:
        ctx.ui_queue.put(('close_result', ''))
        ctx.report('[Success] Result window was closed and its current content cleared.')
        return
    if operation == 'toggle':
        ctx.ui_queue.put(('toggle_result', ''))
        ctx.report('[Success] Result window visibility was toggled once.')
        return
    if operation not in {'show', 'open', 'active', 'replace'}:
        ctx.report('[Error] RESULT_WINDOW action must be show, hide, close, or toggle.')
        return
    body = _tag_body(ctx.response_text, 'RESULT_WINDOW')
    content = body or str(ctx.attrs.get('content') or '').strip()
    title = str(ctx.attrs.get('title') or 'VANTA Result').strip()
    if not content:
        ctx.ui_queue.put(('show_info', ''))
        ctx.report('[Success] Existing result window content was brought forward once.')
        return
    # Some models wrap a REPORT document inside the RESULT_WINDOW action.  The
    # action is already the delivery envelope; passing the nested protocol tags
    # to the renderer made them visible as escaped text and also confused the
    # final delivery path into treating it as a second report.
    try:
        from conversation_output import report_text as _nested_report_text
        nested = _nested_report_text(content)
        if nested:
            content = nested
    except Exception:
        pass
    try:
        payload = json.loads(content)
    except json.JSONDecodeError:
        payload = {'title': title, 'content': content}
    from report_html import blocks_from_payload, validate_scenes
    scene_errors = validate_scenes(blocks_from_payload(payload)[0])
    if scene_errors:
        ctx.report('[Error] Report validation failed; nothing was displayed. '
                   + ' | '.join(scene_errors))
        return
    if isinstance(payload, dict) and isinstance(payload.get('blocks'), list):
        payload.setdefault('title', title)
        ctx.ui_queue.put(('rich_result', payload))
        _remember_task_report(payload)
        count = len(payload['blocks'])
        serialised = json.dumps(payload.get('blocks') or [], ensure_ascii=False)
        ctx.report(_report_render_receipt(payload.get('title') or title, serialised)
                   + f' {count} structured block(s).')
    else:
        if not isinstance(payload, dict):
            payload = {'title': title, 'content': str(payload)}
        payload.setdefault('title', title)
        ctx.ui_queue.put(('show_result', payload))
        _remember_task_report(payload)
        ctx.report(_report_render_receipt(payload.get('title') or title,
                                          payload.get('content') or content))


@action('WIDGET', family='WIDGET', receipt="widget id and render event")
def _run_widget(ctx):
    """Render model-selected information through a safe visual grammar."""
    operation = str(ctx.attr('action') or 'show').strip().lower()
    widget_id = str(ctx.attr('id') or '').strip()
    if operation in {'hide', 'close', 'remove'}:
        ctx.ui_queue.put(('hide_widget', widget_id))
        ctx.report(f'[Success] Widget {widget_id or "surface"} was closed.')
        return
    if operation not in {'show', 'open', 'update', 'replace'}:
        ctx.report('[Error] WIDGET action must be show, update, hide, or close.')
        return
    body = _tag_body(ctx.response_text, 'WIDGET')
    try:
        payload = json.loads(body) if body else {}
        if not isinstance(payload, dict):
            raise ValueError('body is not a JSON object')
        for field in ('id', 'kind', 'title'):
            if ctx.attr(field) and field not in payload:
                payload[field] = ctx.attr(field)
        import widget_spec
        spec = widget_spec.normalize(payload)
    except (ValueError, json.JSONDecodeError) as exc:
        ctx.report(f'[Error] Invalid widget definition: {exc}')
        return
    ctx.ui_queue.put(('show_widget', spec))
    ctx.report(f'[Success] Rendered {spec["kind"]} widget id={spec["id"]}.')


@action('RESEARCH_START', 'RESEARCH_STOP', 'RESEARCH_STATUS', family='RESEARCH_START')
def _run_research_start(ctx):
    """Handles RESEARCH_START, RESEARCH_STOP, RESEARCH_STATUS."""
    action_type = ctx.action_type
    attrs = ctx.attrs
    ui_queue = ctx.ui_queue
    base_dir = ctx.base_dir
    origin = ctx.origin
    turn_id = ctx.turn_id
    interrupt_event = ctx.interrupt_event
    element_map = ctx.element_map
    response_text = ctx.response_text
    import research
    base = os.path.dirname(os.path.abspath(__file__))
    worker = research.get_researcher(base, ui_queue=ui_queue)
    # Variant selection, as a table. Each closure is the
    # body that used to be a rung of an inner ladder.
    res = locals().get('res')
    topic = locals().get('topic')
    def _v_0_research_status():
        nonlocal res, topic
        res = "[Success] " + worker.status_line()
    def _v_1_research_stop():
        nonlocal res, topic
        res = worker.stop()
    def _v_default():
        nonlocal res, topic
        topic = (attrs.get('topic', '') or attrs.get('subject', '')).strip()
        res = worker.start(topic)
    _variants = {
        'RESEARCH_STATUS': _v_0_research_status,
        'RESEARCH_STOP': _v_1_research_stop,
    }
    _variants.get(action_type, _v_default)()
    execute_actions._last_output = res
    print(f"[Agent] {action_type}: {res}")
    ui_queue.put(("info", f"> {action_type}\n\n{res}"))


@action('LIST_DIRECTIVES', family='SET_PERSONA')
@action('ADD_DIRECTIVE', 'REMOVE_DIRECTIVE', 'SET_PERSONA', family='SET_PERSONA', requires=(action_bus.user_only("[Error] Standing instructions can only be set from something Himanshu said, not from a background turn or from something you read."),))
def _run_set_persona(ctx):
    """Handles SET_PERSONA, ADD_DIRECTIVE, REMOVE_DIRECTIVE, LIST_DIRECTIVES."""
    action_type = ctx.action_type
    attrs = ctx.attrs
    ui_queue = ctx.ui_queue
    base_dir = ctx.base_dir
    origin = ctx.origin
    turn_id = ctx.turn_id
    interrupt_event = ctx.interrupt_event
    element_map = ctx.element_map
    response_text = ctx.response_text
    import persona
    base = os.path.dirname(os.path.abspath(__file__))
    # Standing instructions are only ever taken from Himanshu. A
    # page read during research or the body of an email is text
    # VANTA looked at, never an instruction it absorbed.
    # Variant selection, as a table. Each closure is the
    # body that used to be a rung of an inner ladder.
    parts = locals().get('parts')
    res = locals().get('res')
    text = locals().get('text')
    def _v_1_list_directives():
        nonlocal parts, res, text
        res = persona.describe(base)
    def _v_2_remove_directive():
        nonlocal parts, res, text
        res = persona.remove_directive(
            base, attrs.get('id', '') or attrs.get('text', ''))
    def _v_3_add_directive():
        nonlocal parts, res, text
        text = _tag_body(response_text, 'ADD_DIRECTIVE') or attrs.get('text', '')
        res = persona.add_directive(base, text, source='user')
    def _v_default():
        nonlocal parts, res, text
        parts = []
        if attrs.get('address'):
            parts.append(persona.set_address(base, attrs['address']))
        if attrs.get('name'):
            parts.append(persona.set_name(base, attrs['name']))
        res = "\n".join(parts) or (
            '[Error] SET_PERSONA needs address="..." (what to call '
            'him) or name="..." (what he calls you).')
    _variants = {
        'LIST_DIRECTIVES': _v_1_list_directives,
        'REMOVE_DIRECTIVE': _v_2_remove_directive,
        'ADD_DIRECTIVE': _v_3_add_directive,
    }
    _variants.get(action_type, _v_default)()
    execute_actions._last_output = res
    print(f"[Agent] {action_type}: {res.splitlines()[0][:140]}")
    ui_queue.put(("info", f"> {action_type}\n\n{res}"))


@action('CREATE_ACTION', 'LIST_ACTIONS', 'REMOVE_ACTION', 'READ_ACTION', family='CREATE_ACTION')
def _run_create_action(ctx):
    """Handles CREATE_ACTION, LIST_ACTIONS, REMOVE_ACTION, READ_ACTION."""
    action_type = ctx.action_type
    attrs = ctx.attrs
    ui_queue = ctx.ui_queue
    base_dir = ctx.base_dir
    origin = ctx.origin
    turn_id = ctx.turn_id
    interrupt_event = ctx.interrupt_event
    element_map = ctx.element_map
    response_text = ctx.response_text
    import extensions
    base = os.path.dirname(os.path.abspath(__file__))
    # Variant selection, as a table. Each closure is the
    # body that used to be a rung of an inner ladder.
    res = locals().get('res')
    source = locals().get('source')
    def _v_0_list_actions():
        nonlocal res, source
        res = extensions.describe(base)
    def _v_1_read_action():
        nonlocal res, source
        res = extensions.read(base, attrs.get('name', ''))
    def _v_2_remove_action():
        nonlocal res, source
        res = extensions.remove(base, attrs.get('name', ''))
    def _v_default():
        nonlocal res, source
        source = _tag_body(response_text, 'CREATE_ACTION')
        res = extensions.create(
            base, attrs.get('name', ''), source,
            overwrite=str(attrs.get('overwrite', '')).lower() == 'true')
    _variants = {
        'LIST_ACTIONS': _v_0_list_actions,
        'READ_ACTION': _v_1_read_action,
        'REMOVE_ACTION': _v_2_remove_action,
    }
    _variants.get(action_type, _v_default)()
    execute_actions._last_output = res
    print(f"[Agent] {action_type}: {res.splitlines()[0][:140]}")
    ui_queue.put(("info", f"> {action_type}\n\n{res}"))


@action('LIST_WORKFLOWS', 'READ_WORKFLOW', family='CREATE_WORKFLOW')
@action('CREATE_WORKFLOW', 'DEACTIVATE_WORKFLOW', family='CREATE_WORKFLOW', requires=(action_bus.user_only("[Error] Workflows can only be created or deactivated from a task Himanshu initiated."),))
def _run_create_workflow(ctx):
    """Handles CREATE_WORKFLOW, LIST_WORKFLOWS, READ_WORKFLOW, DEACTIVATE_WORKFLOW."""
    action_type = ctx.action_type
    attrs = ctx.attrs
    ui_queue = ctx.ui_queue
    base_dir = ctx.base_dir
    origin = ctx.origin
    turn_id = ctx.turn_id
    interrupt_event = ctx.interrupt_event
    element_map = ctx.element_map
    response_text = ctx.response_text
    import workflows
    base = os.path.dirname(os.path.abspath(__file__))
    identifier = attrs.get('id', '') or attrs.get('name', '')
    # Variant selection, as a table. Each closure is the
    # body that used to be a rung of an inner ladder.
    res = locals().get('res')
    def _v_0_list_workflows():
        nonlocal res
        res = workflows.describe(base)
    def _v_1_read_workflow():
        nonlocal res
        res = workflows.read(base, identifier)
    def _v_3_deactivate_workflow():
        nonlocal res
        res = workflows.deactivate(base, identifier)
    def _v_default():
        nonlocal res
        res = workflows.create(base, _tag_body(response_text, 'CREATE_WORKFLOW'))
    _variants = {
        'LIST_WORKFLOWS': _v_0_list_workflows,
        'READ_WORKFLOW': _v_1_read_workflow,
        'DEACTIVATE_WORKFLOW': _v_3_deactivate_workflow,
    }
    _variants.get(action_type, _v_default)()
    execute_actions._last_output = res
    print(f"[Agent] {action_type}: {res.splitlines()[0][:140]}")
    ui_queue.put(("info", f"> {action_type}\n\n{res}"))


@action('DISCOVER_ACTIONS', family='DISCOVER_ACTIONS')
def _run_discover_actions(ctx):
    """Handles DISCOVER_ACTIONS."""
    action_type = ctx.action_type
    attrs = ctx.attrs
    ui_queue = ctx.ui_queue
    base_dir = ctx.base_dir
    origin = ctx.origin
    turn_id = ctx.turn_id
    interrupt_event = ctx.interrupt_event
    element_map = ctx.element_map
    response_text = ctx.response_text
    import action_registry
    query = attrs.get('query', '')
    # One short line on the notch instead of a results card. The
    # query the model writes for this runs to two hundred
    # characters, so only the shape of it is useful here.
    ui_queue.put(("action", "Looking up what I can do"))
    res = action_registry.discover_text(
        base_dir, query, attrs.get('limit', 12),
        origin=origin)
    execute_actions._last_output = res
    print(f"[Agent] DISCOVER_ACTIONS: {res.splitlines()[0]}")
    ui_queue.put(("info", f"> DISCOVER_ACTIONS\n\n{res}"))


@action('CODING_MODE', family='CODING_MODE')
def _run_coding_mode(ctx):
    """Handles CODING_MODE."""
    action_type = ctx.action_type
    attrs = ctx.attrs
    ui_queue = ctx.ui_queue
    base_dir = ctx.base_dir
    origin = ctx.origin
    turn_id = ctx.turn_id
    interrupt_event = ctx.interrupt_event
    element_map = ctx.element_map
    response_text = ctx.response_text
    import workbench
    res = workbench.open_request(ui_queue, attrs.get('project', ''),
                                 base_dir=base_dir)
    # Build mode is what routes the turn to the coding model
    # (WINTER_MODEL_CODER). Opening the surface and leaving the
    # assistant model in charge would give the editor and the
    # wrong model behind it.
    if res.startswith("[Success]"):
        try:
            import mode_manager
            switch = mode_manager.get_mode_manager().switch(
                "build", source="user",
                reason="coding surface opened")
            if switch.get("changed"):
                res += (f" Switched from {switch['previous']} to "
                        f"build mode.")
        except Exception as exc:
            res += f" (Could not enter build mode: {exc})"
    execute_actions._last_output = res
    print(f"[Agent] CODING_MODE: {res}")
    ui_queue.put(("info", f"> CODING_MODE\n\n{res}"))


@action('IDE_STATUS', 'IDE_OPEN_PROJECT', 'IDE_OPEN_FILE', 'IDE_SHOW_DIFF', family='IDE_STATUS')
def _run_ide_status(ctx):
    """Handles IDE_STATUS, IDE_OPEN_PROJECT, IDE_OPEN_FILE, IDE_SHOW_DIFF."""
    action_type = ctx.action_type
    attrs = ctx.attrs
    ui_queue = ctx.ui_queue
    base_dir = ctx.base_dir
    origin = ctx.origin
    turn_id = ctx.turn_id
    interrupt_event = ctx.interrupt_event
    element_map = ctx.element_map
    response_text = ctx.response_text
    import ide_bridge
    # Variant selection, as a table. Each closure is the
    # body that used to be a rung of an inner ladder.
    res = locals().get('res')
    def _v_0_ide_status():
        nonlocal res
        res = ide_bridge.status(base_dir)['text']
    def _v_1_ide_open_project():
        nonlocal res
        res = ide_bridge.open_project(base_dir, attrs['path'])
    def _v_2_ide_open_file():
        nonlocal res
        res = ide_bridge.open_file(
            base_dir, attrs['path'], attrs.get('line', 1),
            attrs.get('column', 1))
    def _v_default():
        nonlocal res
        res = ide_bridge.show_diff(base_dir, attrs['left'], attrs['right'])
    _variants = {
        'IDE_STATUS': _v_0_ide_status,
        'IDE_OPEN_PROJECT': _v_1_ide_open_project,
        'IDE_OPEN_FILE': _v_2_ide_open_file,
    }
    _variants.get(action_type, _v_default)()
    execute_actions._last_output = res
    ui_queue.put(("info", f"> {action_type}\n\n{res}"))


@action('CODING_AGENT_STATUS', 'CODING_TASK_START', 'CODING_TASK_STATUS', 'CODING_TASK_LIST', 'CODING_TASK_CANCEL', family='CODING_AGENT_STATUS')
def _run_coding_agent_status(ctx):
    """Handles CODING_AGENT_STATUS, CODING_TASK_START, CODING_TASK_STATUS, CODING_TASK_LIST...."""
    action_type = ctx.action_type
    attrs = ctx.attrs
    ui_queue = ctx.ui_queue
    base_dir = ctx.base_dir
    origin = ctx.origin
    turn_id = ctx.turn_id
    interrupt_event = ctx.interrupt_event
    element_map = ctx.element_map
    response_text = ctx.response_text
    import coding_tasks
    # Variant selection, as a table. Each closure is the
    # body that used to be a rung of an inner ladder.
    item = locals().get('item')
    lines = locals().get('lines')
    name = locals().get('name')
    res = locals().get('res')
    states = locals().get('states')
    def _v_0_coding_agent_status():
        nonlocal item, lines, name, res, states
        states = coding_tasks.agent_status()
        lines = [f"{name}: " + (f"available ({item['path']})"
                 if item.get('available') else item.get('error', 'unavailable'))
                 for name, item in states.items()]
        res = "[Success] Coding agents:\n" + "\n".join(lines)
    def _v_1_coding_task_start():
        nonlocal item, lines, name, res, states
        res = coding_tasks.start(
            base_dir, attrs['project'], attrs['prompt'],
            attrs.get('agent', 'codex'), attrs.get('mode', 'implement'))
    def _v_2_coding_task_status():
        nonlocal item, lines, name, res, states
        res = coding_tasks.status(base_dir, attrs['id'])
    def _v_3_coding_task_list():
        nonlocal item, lines, name, res, states
        res = coding_tasks.list_tasks(base_dir, attrs.get('limit', 20))
    def _v_default():
        nonlocal item, lines, name, res, states
        res = coding_tasks.cancel(base_dir, attrs['id'])
    _variants = {
        'CODING_AGENT_STATUS': _v_0_coding_agent_status,
        'CODING_TASK_START': _v_1_coding_task_start,
        'CODING_TASK_STATUS': _v_2_coding_task_status,
        'CODING_TASK_LIST': _v_3_coding_task_list,
    }
    _variants.get(action_type, _v_default)()
    execute_actions._last_output = res
    ui_queue.put(("info", f"> {action_type}\n\n{res[:5000]}"))


@action('ML_BUILD', 'ML_BUILD_STATUS', 'ML_BUILD_LIST', 'ML_BUILD_CANCEL',
        family='ML_BUILD')
def _run_ml_build(ctx):
    """Owns the resumable Kaggle-to-trained-model Build Mode workflow."""
    import ml_build
    import mode_manager
    if ctx.action_type == 'ML_BUILD':
        if mode_manager.current_mode() != mode_manager.WinterMode.BUILD:
            result = ("[Blocked] ML_BUILD runs only in Build Mode. Switch to Build Mode "
                      "and repeat the request; no dataset was downloaded.")
        else:
            result = ml_build.start(
                ctx.base_dir, ctx.attr('query'), dataset=ctx.attr('dataset'),
                target=ctx.attr('target'), project=ctx.attr('project'),
                model=ctx.attr('model') or 'neural_network')
            execute_actions._direct_user_spoken = "Model build started in the background."
    elif ctx.action_type == 'ML_BUILD_STATUS':
        result = ml_build.status(ctx.base_dir, ctx.attr('id'))
    elif ctx.action_type == 'ML_BUILD_LIST':
        result = ml_build.list_jobs(ctx.base_dir, ctx.attr('limit') or 20)
    else:
        result = ml_build.cancel(ctx.base_dir, ctx.attr('id'))
    ctx.report(result)
    ctx.ui_queue.put(("info", f"> {ctx.action_type}\n\n{result[:5000]}"))


@action('LIST_PROJECTS', 'OPEN_PROJECT', 'CREATE_PROJECT', family='LIST_PROJECTS')
@action('DELETE_PROJECT_FILE', 'EDIT_PROJECT_FILE', 'FIND_PROJECT_FILES', 'PROJECT_TREE', 'READ_PROJECT_FILE', 'RUN_IN_PROJECT', 'SEARCH_PROJECT', 'WRITE_PROJECT_FILE', family='LIST_PROJECTS', requires=(action_bus.open_project_required,))
def _run_list_projects(ctx):
    """Handles LIST_PROJECTS, OPEN_PROJECT, CREATE_PROJECT, PROJECT_TREE...."""
    action_type = ctx.action_type
    attrs = ctx.attrs
    ui_queue = ctx.ui_queue
    base_dir = ctx.base_dir
    origin = ctx.origin
    turn_id = ctx.turn_id
    interrupt_event = ctx.interrupt_event
    element_map = ctx.element_map
    response_text = ctx.response_text
    import projects
    base = os.path.dirname(os.path.abspath(__file__))
    # Every file action works on the open project unless one is
    # named, so a turn does not have to repeat it after OPEN_PROJECT.
    project = (attrs.get('project', '') or '').strip() or projects.get_active(base)
    try:
        # Variant selection, as a table. Each closure is the
        # body that used to be a rung of an inner ladder.
        _ = locals().get('_')
        body = locals().get('body')
        command = locals().get('command')
        done = locals().get('done')
        err = locals().get('err')
        find = locals().get('find')
        found = locals().get('found')
        hits = locals().get('hits')
        label = locals().get('label')
        meta = locals().get('meta')
        out = locals().get('out')
        replace = locals().get('replace')
        res = locals().get('res')
        root = locals().get('root')
        tail = locals().get('tail')
        text = locals().get('text')
        def _v_0_list_projects():
            nonlocal _, body, command, done, err, find, found, hits, label, meta, out, replace, res, root, tail, text
            res = projects.describe(base)
        def _v_1_create_project():
            nonlocal _, body, command, done, err, find, found, hits, label, meta, out, replace, res, root, tail, text
            res = projects.create(base, attrs.get('name', ''),
                                  attrs.get('description', ''),
                                  attrs.get('language', ''))
        def _v_2_open_project():
            nonlocal _, body, command, done, err, find, found, hits, label, meta, out, replace, res, root, tail, text
            res = projects.open_project(
                base, attrs.get('name', '') or attrs.get('project', ''))
            # Opening establishes project context; it must not also cover the
            # user's screen with a coding surface. CONTROL_CENTER page=Code or
            # CODING_MODE is the explicit visual request.
            ui_queue.put(("project_changed", projects.get_active(base)))
        def _v_4_project_tree():
            nonlocal _, body, command, done, err, find, found, hits, label, meta, out, replace, res, root, tail, text
            res = "[Success] " + projects.tree(base, project)
        def _v_5_read_project_file():
            nonlocal _, body, command, done, err, find, found, hits, label, meta, out, replace, res, root, tail, text
            text, meta = projects.read_file(
                base, project, attrs.get('path', ''))
            tail = "\n[...truncated]" if meta["truncated"] else ""
            res = (f"[Success] {meta['path']} in {project} "
                   f"({meta['size']} bytes):\n\n{text}{tail}")
        def _v_6_write_project_file():
            nonlocal _, body, command, done, err, find, found, hits, label, meta, out, replace, res, root, tail, text
            body = _tag_body(response_text, 'WRITE_PROJECT_FILE')
            res = projects.write_file(
                base, project, attrs.get('path', ''),
                body if body else attrs.get('content', ''),
                overwrite=str(attrs.get('overwrite', '')).lower() == 'true')
            ui_queue.put(("project_changed", project))
        def _v_7_edit_project_file():
            nonlocal _, body, command, done, err, find, found, hits, label, meta, out, replace, res, root, tail, text
            body = _tag_body(response_text, 'EDIT_PROJECT_FILE')
            find, replace = attrs.get('find', ''), attrs.get('replace', '')
            if body and '<<<REPLACE>>>' in body:
                find, _, replace = body.partition('<<<REPLACE>>>')
                find, replace = find.strip("\n"), replace.strip("\n")
            res = projects.edit_file(
                base, project, attrs.get('path', ''), find, replace,
                attrs.get('count', 1))
            ui_queue.put(("project_changed", project))
        def _v_8_delete_project_file():
            nonlocal _, body, command, done, err, find, found, hits, label, meta, out, replace, res, root, tail, text
            res = projects.delete_file(
                base, project, attrs.get('path', ''),
                confirmed=str(attrs.get('confirmed', '')).lower() == 'true')
            ui_queue.put(("project_changed", project))
        def _v_9_search_project():
            nonlocal _, body, command, done, err, find, found, hits, label, meta, out, replace, res, root, tail, text
            hits = projects.search(base, project, attrs.get('pattern', '')
                                   or attrs.get('query', ''))
            res = ("[Success] " + "\n".join(hits)) if hits else (
                f"[Success] No match for that in {project}.")
        def _v_10_find_project_files():
            nonlocal _, body, command, done, err, find, found, hits, label, meta, out, replace, res, root, tail, text
            found = projects.find_files(base, project,
                                        attrs.get('pattern', '*'))
            res = ("[Success] " + "\n".join(found)) if found else (
                f"[Success] Nothing matching that in {project}.")
        def _v_default():
            nonlocal _, body, command, done, err, find, found, hits, label, meta, out, replace, res, root, tail, text
            root = projects.project_path(base, project)
            command = attrs.get('cmd', '') or attrs.get('command', '')
            if not command.strip():
                res = '[Error] RUN_IN_PROJECT needs cmd="...".'
            else:
                done = subprocess.run(
                    ["powershell", "-NoProfile", "-NonInteractive",
                     "-Command", command],
                    capture_output=True, text=True, cwd=root,
                    timeout=int(attrs.get('timeout', 120) or 120),
                    encoding="utf-8", errors="replace",
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                out = (done.stdout or "").strip()[:6000]
                err = (done.stderr or "").strip()[:3000]
                label = "[Success]" if done.returncode == 0 else "[Error]"
                res = (f"{label} exit {done.returncode} in {project}\n"
                       + (f"stdout:\n{out}\n" if out else "")
                       + (f"stderr:\n{err}" if err else "")).strip()
        _variants = {
            'LIST_PROJECTS': _v_0_list_projects,
            'CREATE_PROJECT': _v_1_create_project,
            'OPEN_PROJECT': _v_2_open_project,
            'PROJECT_TREE': _v_4_project_tree,
            'READ_PROJECT_FILE': _v_5_read_project_file,
            'WRITE_PROJECT_FILE': _v_6_write_project_file,
            'EDIT_PROJECT_FILE': _v_7_edit_project_file,
            'DELETE_PROJECT_FILE': _v_8_delete_project_file,
            'SEARCH_PROJECT': _v_9_search_project,
            'FIND_PROJECT_FILES': _v_10_find_project_files,
        }
        _variants.get(action_type, _v_default)()
    except projects.ProjectError as exc:
        res = f"[Error] {exc}"
    except subprocess.TimeoutExpired:
        res = f"[Error] That command did not finish in {project}."
    except Exception as exc:
        res = f"[Error] {action_type} failed: {exc}"
    execute_actions._last_output = res
    print(f"[Agent] {action_type}: {res.splitlines()[0][:140]}")
    ui_queue.put(("info", f"> {action_type}\n\n{res[:1500]}"))


@action('ANNOTATE_TEXT', family='ANNOTATE_TEXT')
def _run_annotate_text(ctx):
    """Handles ANNOTATE_TEXT."""
    action_type = ctx.action_type
    attrs = ctx.attrs
    ui_queue = ctx.ui_queue
    base_dir = ctx.base_dir
    origin = ctx.origin
    turn_id = ctx.turn_id
    interrupt_event = ctx.interrupt_event
    element_map = ctx.element_map
    response_text = ctx.response_text
    # Pointing at a phrase by name rather than by coordinate. A
    # model reading a downscaled screenshot guesses pixels badly,
    # and a box that lands beside the sentence it names is worse
    # than no box; OCR measures the real screen.
    import system_tools
    wanted = attrs.get('text', '') or attrs.get('find', '')
    box, detail = system_tools.locate_text_box(
        wanted, attrs.get('region', ''))
    if box is None:
        res = detail
    else:
        left, top, right, bottom = box
        pad = 6
        shape = (attrs.get('shape', 'highlight') or 'highlight').lower()
        spec = {
            "shape": shape,
            "color": attrs.get('color', '#ffd60a'),
            "width": 3,
            "label": attrs.get('label', ''),
            "n": attrs.get('n', ''),
        }
        if shape == "callout":
            # The leader starts on the phrase and ends clear of it.
            spec.update(x1=(left + right) // 2, y1=top - 4,
                        x2=(left + right) // 2, y2=max(24, top - 70))
        elif shape == "number":
            spec.update(x1=left - 26, y1=(top + bottom) // 2,
                        x2=left, y2=bottom)
        else:
            spec.update(x1=left - pad, y1=top - pad,
                        x2=right + pad, y2=bottom + pad)
        ui_queue.put(("annotate", spec))
        res = (f"[Success] Drew a {shape} on '{wanted}' at {box}. "
               "It stays until DRAW_CLEAR.")
    execute_actions._last_output = res
    print(f"[Agent] {action_type}: {res.splitlines()[0][:140]}")
    ui_queue.put(("info", f"> {action_type}\n\n{res}"))


@action('DRAW_ON_SCREEN', 'DRAW_SCENE', 'DRAW_CLEAR', family='DRAW_ON_SCREEN')
def _run_draw_on_screen(ctx):
    """Handles DRAW_ON_SCREEN, DRAW_SCENE, DRAW_CLEAR."""
    action_type = ctx.action_type
    attrs = ctx.attrs
    ui_queue = ctx.ui_queue
    base_dir = ctx.base_dir
    origin = ctx.origin
    turn_id = ctx.turn_id
    interrupt_event = ctx.interrupt_event
    element_map = ctx.element_map
    response_text = ctx.response_text
    # Variant selection, as a table. Each closure is the
    # body that used to be a rung of an inner ladder.
    _desk = locals().get('_desk')
    _p1 = locals().get('_p1')
    _p2 = locals().get('_p2')
    _px = locals().get('_px')
    _space = locals().get('_space')
    allowed_shapes = locals().get('allowed_shapes')
    exc = locals().get('exc')
    index = locals().get('index')
    item = locals().get('item')
    items = locals().get('items')
    raw = locals().get('raw')
    res = locals().get('res')
    scene = locals().get('scene')
    shape = locals().get('shape')
    spec = locals().get('spec')
    def _v_0_draw_clear():
        nonlocal _desk, _p1, _p2, _px, _space, allowed_shapes, exc, index, item, items, raw, res, scene, shape, spec
        ui_queue.put(("annotate_clear", ""))
        res = "[Success] Annotations cleared."
    def _v_1_draw_scene():
        nonlocal _desk, _p1, _p2, _px, _space, allowed_shapes, exc, index, item, items, raw, res, scene, shape, spec
        raw = (_tag_body(response_text, 'DRAW_SCENE')
               or attrs.get('scene', ''))
        try:
            scene = json.loads(raw)
            if not isinstance(scene, dict):
                raise ValueError("scene must be a JSON object")
            items = scene.get('items', [])
            if not isinstance(items, list) or not items:
                raise ValueError("scene.items must be a non-empty list")
            if len(items) > 250:
                raise ValueError("scene is limited to 250 items")
            allowed_shapes = {
                'line', 'arrow', 'rectangle', 'oval', 'polygon',
                'arc', 'text'
            }
            for index, item in enumerate(items):
                if not isinstance(item, dict):
                    raise ValueError(f"item {index + 1} must be an object")
                shape = str(item.get('type', '')).lower()
                if shape not in allowed_shapes:
                    raise ValueError(
                        f"item {index + 1} has unsupported type '{shape}'")
            scene['clear'] = str(attrs.get('clear', 'true')).lower() not in {
                'false', '0', 'no'
            }
            if attrs.get('background'):
                scene['background'] = attrs.get('background')
            ui_queue.put(("draw_scene", scene))
            res = (f"[Success] Drew a complete scene with {len(items)} "
                   "items. It stays until DRAW_CLEAR.")
        except (json.JSONDecodeError, ValueError) as exc:
            res = f"[Error] DRAW_SCENE needs valid scene JSON: {exc}."
    def _v_default():
        nonlocal _desk, _p1, _p2, _px, _space, allowed_shapes, exc, index, item, items, raw, res, scene, shape, spec
        def _px(key, default=0):
            try:
                return int(float(attrs.get(key, default)))
            except (TypeError, ValueError):
                return int(default)
        # The renderer's shape dispatch has no fallback branch, so
        # a name it does not know drew nothing at all while the
        # label still appeared and the receipt still claimed
        # success. Refuse it here instead, and accept the obvious
        # synonyms the model reaches for ("rect", "rectangle").
        # Imported here rather than inherited: this used to read an
        # `import action_schema` that lived in execute_actions, and once the
        # body moved into a handler the name was simply gone -- a NameError on
        # every DRAW_ON_SCREEN.
        import action_schema
        shape = action_schema.normalise_shape(attrs.get('shape'))
        # The model reads these off a screenshot, so they carry the
        # same scaling error a click would: a box drawn from raw
        # image coordinates lands far from the thing it points at.
        import desktop as _desk
        _space = attrs.get('space', 'image')
        _p1 = _desk.to_screen(_px('x1'), _px('y1'), _space)
        _p2 = _desk.to_screen(_px('x2'), _px('y2'), _space)
        spec = {
            "shape": shape,
            "color": attrs.get('color', '#ff3b30'),
            "width": _px('width', 4),
            "x1": _p1[0], "y1": _p1[1],
            "x2": _p2[0], "y2": _p2[1],
            "label": attrs.get('label', ''),
            "n": attrs.get('n', ''),
        }
        if not shape:
            res = (f"[Error] DRAW_ON_SCREEN cannot draw "
                   f"shape=\"{attrs.get('shape')}\". Available "
                   f"shapes: {', '.join(action_schema.DRAW_SHAPES)}.")
        elif spec['x2'] == 0 and spec['y2'] == 0:
            res = ("[Error] DRAW_ON_SCREEN needs x1,y1,x2,y2 in screen pixels. "
                   "Take a screenshot first so the coordinates are real.")
        else:
            ui_queue.put(("annotate", spec))
            res = (f"[Success] Drew a {shape} from ({spec['x1']}, {spec['y1']}) to "
                   f"({spec['x2']}, {spec['y2']})"
                   + (f" labelled '{spec['label']}'" if spec['label'] else "")
                   + ". It stays until DRAW_CLEAR.")
    _variants = {
        'DRAW_CLEAR': _v_0_draw_clear,
        'DRAW_SCENE': _v_1_draw_scene,
    }
    _variants.get(action_type, _v_default)()
    execute_actions._last_output = res
    print(f"[Agent] {action_type}: {res}")


@action('WRITE_CODE', 'RUN_SANDBOX', family='WRITE_CODE')
def _run_write_code(ctx):
    """Handles WRITE_CODE, RUN_SANDBOX."""
    action_type = ctx.action_type
    attrs = ctx.attrs
    ui_queue = ctx.ui_queue
    base_dir = ctx.base_dir
    origin = ctx.origin
    turn_id = ctx.turn_id
    interrupt_event = ctx.interrupt_event
    element_map = ctx.element_map
    response_text = ctx.response_text
    import codetools
    body = ''
    match = re.search(
        rf'<ACTION\s+[^>]*type="{action_type}"[^>]*>([\s\S]*?)</ACTION>',
        response_text, re.IGNORECASE)
    if match:
        body = match.group(1)

    def _timeout():
        try:
            return int(float(attrs.get('timeout', 60)))
        except (TypeError, ValueError):
            return 60

    # Variant selection, as a table. Each closure is the
    # body that used to be a rung of an inner ladder.
    res = locals().get('res')
    def _v_0_write_code():
        nonlocal res
        res = codetools.write_code(
            base_dir,
            attrs.get('path', '') or attrs.get('file', ''),
            body or attrs.get('code', ''),
            language=attrs.get('language', ''),
            overwrite=attrs.get('overwrite', '').strip().lower() == 'true',
            run_after=attrs.get('run', '').strip().lower() == 'true',
            timeout=_timeout())
    def _v_default():
        nonlocal res
        res = codetools.run_sandbox(
            base_dir,
            code=body or attrs.get('code', ''),
            language=attrs.get('language', 'python'),
            timeout=_timeout(),
            path=attrs.get('path', ''))
    _variants = {
        'WRITE_CODE': _v_0_write_code,
    }
    _variants.get(action_type, _v_default)()
    execute_actions._last_output = res
    print(f"[Agent] {action_type}: {res.splitlines()[0][:110]}")
    ui_queue.put(("info", f"> {action_type}\n\n{res[:1500]}"))


@action('SCREEN_LIGHT', 'SCREEN_LIGHT_OFF', family='SCREEN_LIGHT')
def _run_screen_light(ctx):
    """Handles SCREEN_LIGHT, SCREEN_LIGHT_OFF."""
    action_type = ctx.action_type
    attrs = ctx.attrs
    ui_queue = ctx.ui_queue
    base_dir = ctx.base_dir
    origin = ctx.origin
    turn_id = ctx.turn_id
    interrupt_event = ctx.interrupt_event
    element_map = ctx.element_map
    response_text = ctx.response_text
    # Variant selection, as a table. Each closure is the
    # body that used to be a rung of an inner ladder.
    colour = locals().get('colour')
    opacity = locals().get('opacity')
    preset = locals().get('preset')
    presets = locals().get('presets')
    res = locals().get('res')
    def _v_0_screen_light_off():
        nonlocal colour, opacity, preset, presets, res
        ui_queue.put(("screen_light_off", ""))
        res = "[Success] Screen light off."
    def _v_default():
        nonlocal colour, opacity, preset, presets, res
        preset = (attrs.get('preset', '') or '').strip().lower()
        presets = {
            'light': ('#ffffff', 1.0), 'softbox': ('#fff6e8', 0.95),
            'warm': ('#ffd9a0', 0.9), 'cool': ('#d8ecff', 0.9),
            'dim': ('#000000', 0.55), 'blackout': ('#000000', 0.97),
            'red': ('#ff3b30', 0.85), 'focus': ('#000000', 0.35),
        }
        colour, opacity = presets.get(preset, ('#ffffff', 1.0))
        colour = attrs.get('color', '') or colour
        if attrs.get('opacity'):
            try:
                opacity = float(attrs.get('opacity'))
            except (TypeError, ValueError):
                pass
        ui_queue.put(("screen_light", {"color": colour, "opacity": opacity}))
        res = (f"[Success] Screen light on ({preset or 'light'}, {colour}, "
               f"{opacity:.0%}). Use SCREEN_LIGHT_OFF to clear it.")
    _variants = {
        'SCREEN_LIGHT_OFF': _v_0_screen_light_off,
    }
    _variants.get(action_type, _v_default)()
    execute_actions._last_output = res
    print(f"[Agent] {action_type}: {res}")


@action('SHUTDOWN', 'RESTART', family='SHUTDOWN')
def _run_shutdown(ctx):
    """Handles SHUTDOWN, RESTART."""
    action_type = ctx.action_type
    attrs = ctx.attrs
    ui_queue = ctx.ui_queue
    base_dir = ctx.base_dir
    origin = ctx.origin
    turn_id = ctx.turn_id
    interrupt_event = ctx.interrupt_event
    element_map = ctx.element_map
    response_text = ctx.response_text
    # A model-authored approved="true" is not approval. Power
    # actions require the opaque id of a row the human approval
    # UI has actually settled as approved.
    from memory.state_db import execute_read
    approval_id = attrs.get('approval_id', '')
    approved = execute_read(
        "SELECT id FROM approvals WHERE id=? AND action_type=? "
        "AND status='approved' LIMIT 1",
        (approval_id, action_type)) if approval_id else []
    if not approved:
        res = ("[Approval Required] Power control was not executed. "
               "Approve the pending VANTA power request first.")
    else:
        import system_controls
        res = system_controls.shutdown(
            confirmed=True, restart=action_type == 'RESTART')
    execute_actions._last_output = res
    print(f"[Agent] {action_type}: {res}")
    ui_queue.put(("info", f"> {action_type}\n\n{res}"))


@action('IMPORT_HEALTH', family='IMPORT_HEALTH')
def _run_import_health(ctx):
    """Handles IMPORT_HEALTH."""
    action_type = ctx.action_type
    attrs = ctx.attrs
    ui_queue = ctx.ui_queue
    base_dir = ctx.base_dir
    origin = ctx.origin
    turn_id = ctx.turn_id
    interrupt_event = ctx.interrupt_event
    element_map = ctx.element_map
    response_text = ctx.response_text
    import health_import
    res = health_import.import_health(
        base_dir, attrs.get('path', '') or attrs.get('file', ''))
    execute_actions._last_output = res
    print(f"[Agent] IMPORT_HEALTH: {res.splitlines()[0][:110]}")
    # Health detail belongs on the dashboard, not read aloud.
    if _classify_output(res):
        set_direct_user_delivery(
            res,
            "I've imported your health data, sir. The summary is on your dashboard.",
            surface="dashboard", title="Health import summary")
    ui_queue.put(("info", f"> IMPORT_HEALTH\n\n{res}"))


@action('WHY_FACT', family='WHY_FACT')
def _run_why_fact(ctx):
    """Handles WHY_FACT."""
    action_type = ctx.action_type
    attrs = ctx.attrs
    ui_queue = ctx.ui_queue
    base_dir = ctx.base_dir
    origin = ctx.origin
    turn_id = ctx.turn_id
    interrupt_event = ctx.interrupt_event
    element_map = ctx.element_map
    response_text = ctx.response_text
    from memory.neo4j_store import store as _graph
    from memory.neo4j_store import display_name
    rows = _graph.explain_fact(
        attrs.get('subject', '') or 'Himanshu',
        attrs.get('predicate', ''), attrs.get('object', ''))
    if not rows:
        res = f"I have no record of that about {attrs.get('subject','Himanshu')}."
    else:
        lines = []
        for row in rows:
            pred = str(row['predicate']).replace('_', ' ').lower()
            line = f"- {display_name(row['subject'])} {pred} {display_name(row['object'])}"
            if row.get('valid_to'):
                line += " (no longer current)"
            lines.append(line)
            lines.append(f"    {row['evidence']}")
            if str(row.get('fact_class')) == 'derived_inference':
                state = row.get('review_status')
                lines.append(f"    this is my own inference, {state}"
                             if state != 'accepted' else
                             "    originally my inference, which you confirmed")
            if int(row.get('mention_count') or 1) > 1:
                lines.append(f"    you've mentioned this {row['mention_count']} times")
        res = "\n".join(lines)
    execute_actions._last_output = res
    print(f"[Agent] WHY_FACT: {len(rows)} fact(s)")
    ui_queue.put(("info", f"> WHY_FACT\n\n{res}"))


@action('NOTE_GAP', 'LIST_GAPS', 'FILL_GAP', family='NOTE_GAP')
def _run_note_gap(ctx):
    """Handles NOTE_GAP, LIST_GAPS, FILL_GAP."""
    action_type = ctx.action_type
    attrs = ctx.attrs
    ui_queue = ctx.ui_queue
    base_dir = ctx.base_dir
    origin = ctx.origin
    turn_id = ctx.turn_id
    interrupt_event = ctx.interrupt_event
    element_map = ctx.element_map
    response_text = ctx.response_text
    import knowledge_gaps
    # Variant selection, as a table. Each closure is the
    # body that used to be a rung of an inner ladder.
    res = locals().get('res')
    def _v_0_list_gaps():
        nonlocal res
        res = knowledge_gaps.listing(base_dir)
    def _v_1_note_gap():
        nonlocal res
        res = knowledge_gaps.note(
            base_dir, attrs.get('topic', ''), attrs.get('question', ''),
            why=attrs.get('why', ''), predicate=attrs.get('predicate', ''),
            subject=attrs.get('subject', '') or 'Himanshu')
    def _v_default():
        nonlocal res
        res = knowledge_gaps.fill(
            base_dir, attrs.get('id', '') or attrs.get('topic', ''),
            attrs.get('answer', ''))
    _variants = {
        'LIST_GAPS': _v_0_list_gaps,
        'NOTE_GAP': _v_1_note_gap,
    }
    _variants.get(action_type, _v_default)()
    execute_actions._last_output = res
    print(f"[Agent] {action_type}: {res.splitlines()[0][:110]}")
    ui_queue.put(("info", f"> {action_type}\n\n{res}"))


@action('REVIEW_INSIGHTS', family='REVIEW_INSIGHTS')
def _run_review_insights(ctx):
    """Handles REVIEW_INSIGHTS."""
    action_type = ctx.action_type
    attrs = ctx.attrs
    ui_queue = ctx.ui_queue
    base_dir = ctx.base_dir
    origin = ctx.origin
    turn_id = ctx.turn_id
    interrupt_event = ctx.interrupt_event
    element_map = ctx.element_map
    response_text = ctx.response_text
    import inference
    from memory.neo4j_store import store as _graph
    rows = _graph.pending_inferences(limit=int(attrs.get('limit', '10')))
    if attrs.get('refresh', '').strip().lower() == 'true' and not rows:
        import llm_router
        inference.run_inference_pass(llm_router.call_llm)
        rows = _graph.pending_inferences(limit=int(attrs.get('limit', '10')))
    res = inference.format_pending(rows)
    execute_actions._last_output = res
    print(f"[Agent] REVIEW_INSIGHTS: {len(rows)} pending")
    ui_queue.put(("info", f"> REVIEW_INSIGHTS\n\n{res}"))


@action('CONFIRM_INSIGHT', 'REJECT_INSIGHT', family='CONFIRM_INSIGHT')
def _run_confirm_insight(ctx):
    """Handles CONFIRM_INSIGHT, REJECT_INSIGHT."""
    action_type = ctx.action_type
    attrs = ctx.attrs
    ui_queue = ctx.ui_queue
    base_dir = ctx.base_dir
    origin = ctx.origin
    turn_id = ctx.turn_id
    interrupt_event = ctx.interrupt_event
    element_map = ctx.element_map
    response_text = ctx.response_text
    accept = action_type == 'CONFIRM_INSIGHT'
    subject = attrs.get('subject', '') or 'Himanshu'
    predicate = attrs.get('predicate', '')
    obj = attrs.get('object', '')
    if not predicate:
        execute_actions._last_output = f"Error: {action_type} needs a predicate."
    else:
        from memory.neo4j_store import store as _graph
        changed = _graph.set_inference_status(subject, predicate, obj or None, accept=accept)
        if not changed:
            res = "No matching pending insight was found."
        else:
            verb = "Confirmed" if accept else "Dismissed"
            res = f"{verb}: " + "; ".join(
                f"{c['subject']} {c['predicate']} {c['object']}" for c in changed
            )
            if accept:
                res += ". I'll treat that as known from now on."
            else:
                res += ". I won't raise it again."
        execute_actions._last_output = res
        print(f"[Agent] {action_type}: {res}")
        ui_queue.put(("info", f"> {action_type}\n\n{res}"))


@action('LEARN_KNOWLEDGE', family='LEARN_KNOWLEDGE')
def _run_learn_knowledge(ctx):
    """Handles LEARN_KNOWLEDGE."""
    action_type = ctx.action_type
    attrs = ctx.attrs
    ui_queue = ctx.ui_queue
    base_dir = ctx.base_dir
    origin = ctx.origin
    turn_id = ctx.turn_id
    interrupt_event = ctx.interrupt_event
    element_map = ctx.element_map
    response_text = ctx.response_text
    title = attrs.get('title', 'Learning')
    text = attrs.get('text', '')
    if text:
        msg = f"Absorbing real-time knowledge: {title}"
        print(f"[Action] {msg}")
        ui_queue.put(("action", msg))
        try:
            import memory
            memory.save_knowledge(title, text)
            execute_actions._last_output = f"[Success] Knowledge '{title}' saved to the unified graph."
            ui_queue.put(("info", execute_actions._last_output))
        except Exception as e:
            execute_actions._last_output = f"[Error] Failed to inject knowledge to DB: {e}"
            print(execute_actions._last_output)


@action('DEEP_RESEARCH', family='DEEP_RESEARCH')
def _run_deep_research(ctx):
    """Handles DEEP_RESEARCH."""
    action_type = ctx.action_type
    attrs = ctx.attrs
    ui_queue = ctx.ui_queue
    base_dir = ctx.base_dir
    origin = ctx.origin
    turn_id = ctx.turn_id
    interrupt_event = ctx.interrupt_event
    element_map = ctx.element_map
    response_text = ctx.response_text
    topic = attrs.get('topic', '')
    if topic:
        msg = f"Autonomous Deep Research: '{topic}'"
        print(f"[Action] {msg}")
        ui_queue.put(("action", msg))
        ui_queue.put(("info", f"Executing deep web search for:\n{topic}..."))

        try:
            import requests
            tavily_key = os.getenv("TAVILY_API_KEY")
            if not tavily_key:
                raise Exception("Tavily API key is missing in your .env file!")

            payload = {
                "api_key": tavily_key,
                "query": topic,
                "search_depth": "advanced",
                "include_answer": True,
                "include_raw_content": True
            }
            response = requests.post("https://api.tavily.com/search", json=payload, timeout=25)
            response.raise_for_status()
            data = response.json()

            summary = data.get("answer", "")
            results = data.get("results", [])

            raw_content = ""
            if summary:
                raw_content += f"[Summary]\n{summary}\n\n"
            for res in results:
                raw_content += f"[Content from {res.get('title')}]\n{res.get('raw_content') or res.get('content', '')}\n\n"

            if not raw_content.strip():
                raise Exception("No deep content found.")

            # 1. Save source evidence to Neo4j memory
            import memory
            memory.save_knowledge(topic, raw_content)

            # 2. Queue for Graph DB (Research Mode)
            from memory.consolidator import queue_for_consolidation
            # we pass the current turn_id as source_id for traceability
            queue_for_consolidation(raw_content, source_id=turn_id, mode="research")

            execute_actions._last_output = f"[Success] Deep Research complete. {len(raw_content.split())} words stored with graph extraction queued."
            ui_queue.put(("info", execute_actions._last_output))
        except Exception as e:
            err = f"[Error] Deep Research failed: {e}"
            execute_actions._last_output = err
            print(err)
            ui_queue.put(("info", f"> DEEP_RESEARCH\n\n{err}"))


@action('SEARCH_WEB', family='SEARCH_WEB')
def _run_search_web(ctx):
    """Handles SEARCH_WEB."""
    action_type = ctx.action_type
    attrs = ctx.attrs
    ui_queue = ctx.ui_queue
    base_dir = ctx.base_dir
    origin = ctx.origin
    turn_id = ctx.turn_id
    interrupt_event = ctx.interrupt_event
    element_map = ctx.element_map
    response_text = ctx.response_text
    query = attrs.get('query', '')
    if query:
        msg = f"Web Search: '{query}'"
        print(f"[Action] {msg}")
        ui_queue.put(("action", msg))
        ui_queue.put(("info", f"Searching web for:\n{query}"))

        try:
            from providers import research_api
            data = research_api.advanced_search(
                query, depth=attrs.get('depth', 'advanced'),
                max_results=int(attrs.get('limit', '5') or 5),
                provider=attrs.get('provider', 'auto'), include_raw=True,
                include_domains=[d.strip() for d in str(attrs.get('include_domains') or '').split(',') if d.strip()],
                exclude_domains=[d.strip() for d in str(attrs.get('exclude_domains') or '').split(',') if d.strip()],
                timeout=20)
            results_text = research_api.format_for_model(data)

            execute_actions._last_output = results_text
            print(f"[Command Output]\n{results_text}")
            ui_queue.put(("info", f"> SEARCH_WEB\n\n{results_text[:1000]}..."))
        except Exception as e:
            err = f"[Error] Web search failed: {e}"
            execute_actions._last_output = err
            print(err)
            ui_queue.put(("info", f"> SEARCH_WEB\n\n{err}"))


@action('KNOWLEDGE_SEARCH', 'TRENDING_NEWS', family='KNOWLEDGE_SEARCH')
def _run_knowledge_search(ctx):
    """Handles KNOWLEDGE_SEARCH, TRENDING_NEWS."""
    action_type = ctx.action_type
    attrs = ctx.attrs
    ui_queue = ctx.ui_queue
    base_dir = ctx.base_dir
    origin = ctx.origin
    turn_id = ctx.turn_id
    interrupt_event = ctx.interrupt_event
    element_map = ctx.element_map
    response_text = ctx.response_text
    query = attrs.get('query', '').strip()
    if query:
        from providers import knowledge_api
        raw_sources = attrs.get('sources', '').strip().lower()
        sources = None if raw_sources in {'', 'auto'} else [
            item.strip() for item in raw_sources.split(',') if item.strip()]
        purpose = ('news' if action_type == 'TRENDING_NEWS'
                   else attrs.get('purpose', 'knowledge'))
        try:
            data = knowledge_api.search(
                query, sources=sources,
                limit=int(attrs.get('limit', 8)), purpose=purpose)
            if action_type == 'TRENDING_NEWS' and not data.get('results'):
                # Free RSS/community feeds can all be reachable yet return no
                # matches.  An empty list is not a news briefing, so fall back
                # to the existing Tavily/Google provider before telling the
                # user that current news is unavailable.
                try:
                    from providers import research_api
                    fallback = research_api.advanced_search(
                        query, depth='basic',
                        max_results=int(attrs.get('limit', 8)),
                        provider='auto', include_raw=False, timeout=18)
                    result = ("[News retrieval fallback: current web search]\n" +
                              research_api.format_for_model(fallback))
                    result_count = len(fallback.get('results', []))
                    source_status = "web-search fallback"
                except Exception as fallback_error:
                    result = (knowledge_api.format_for_model(data) +
                              "\nFallback unavailable: " +
                              str(fallback_error)[:240])
                    result_count = 0
                    source_status = "no currently available provider"
            else:
                result = knowledge_api.format_for_model(data)
                result_count = len(data.get('results', []))
                source_status = (f"{len(data['sources']) - len(data['errors'])}/"
                                 f"{len(data['sources'])} sources")
            execute_actions._last_output = result
            ui_queue.put(("info", f"> {action_type}\n\n{result}"))
            print(f"[Agent] {action_type}: {result_count} results from "
                  f"{source_status}.")
        except Exception as exc:
            err = f"[Error] Federated knowledge search failed: {exc}"
            execute_actions._last_output = err
            print(err)
            ui_queue.put(("info", f"> {action_type}\n\n{err}"))


@action('OPEN_PAPER', family='OPEN_PAPER')
def _run_open_paper(ctx):
    """Resolve and open one paper without trusting a model-authored URL."""
    from providers import knowledge_api
    import browser
    title = ctx.attr('title').strip()
    if not title:
        ctx.report('[Error] OPEN_PAPER requires the paper title.')
        return
    ctx.step(f"Resolving paper: {title[:48]}")
    try:
        resolved = knowledge_api.resolve_paper(
            title, author=ctx.attr('author'), year=ctx.attr('year'))
        match = resolved['match']
        url = match['url']
        navigation = browser.goto(
            url, tab=ctx.attr('tab') or None,
            visible=not ctx.flag('background', False))
        if '[Error]' in str(navigation) or '[Blocked]' in str(navigation):
            ctx.report(str(navigation))
            return
        ctx.report(
            "[Success] Opened the verified academic paper.\n"
            f"Title: {match['title']}\n"
            f"Source: {match['source']}\n"
            f"Identity score: {match['identity_score']:.2f}\n"
            f"URL: {url}\n"
            "Do not replace this page with another candidate unless the user asks."
        )
    except Exception as exc:
        ctx.report(f"[Error] Could not resolve the requested paper safely: {exc}")


@action('SEARCH_RECALL', family='SEARCH_RECALL')
def _run_search_recall(ctx):
    """Handles SEARCH_RECALL."""
    action_type = ctx.action_type
    attrs = ctx.attrs
    ui_queue = ctx.ui_queue
    base_dir = ctx.base_dir
    origin = ctx.origin
    turn_id = ctx.turn_id
    interrupt_event = ctx.interrupt_event
    element_map = ctx.element_map
    response_text = ctx.response_text
    query = attrs.get('query', '')
    if query:
        import recall
        msg = f"Searching visual memory for: '{query}'"
        print(f"[Action] {msg}")
        ui_queue.put(("action", msg))
        results = recall.search_recall(base_dir, query)
        execute_actions._last_output = results
        print(f"[Command Output]\n{results}")
        ui_queue.put(("info", f"> SEARCH_RECALL\n\n{results[:1000]}..."))


@action('SPOTIFY_PLAY', family='SPOTIFY_PLAY')
def _run_spotify_play(ctx):
    """Handles SPOTIFY_PLAY."""
    action_type = ctx.action_type
    attrs = ctx.attrs
    ui_queue = ctx.ui_queue
    base_dir = ctx.base_dir
    origin = ctx.origin
    turn_id = ctx.turn_id
    interrupt_event = ctx.interrupt_event
    element_map = ctx.element_map
    response_text = ctx.response_text
    query = attrs.get('query', '')
    from providers import spotify_api
    res = spotify_api.spotify_play(base_dir, query)
    execute_actions._last_output = res
    print(f"[Agent] SPOTIFY_PLAY: {res}")
    ui_queue.put(("info", f"> SPOTIFY_PLAY\n\n{res}"))


@action('SPOTIFY_PAUSE', family='SPOTIFY_PAUSE')
def _run_spotify_pause(ctx):
    """Handles SPOTIFY_PAUSE."""
    action_type = ctx.action_type
    attrs = ctx.attrs
    ui_queue = ctx.ui_queue
    base_dir = ctx.base_dir
    origin = ctx.origin
    turn_id = ctx.turn_id
    interrupt_event = ctx.interrupt_event
    element_map = ctx.element_map
    response_text = ctx.response_text
    from providers import spotify_api
    res = spotify_api.spotify_pause(base_dir)
    execute_actions._last_output = res
    print(f"[Agent] SPOTIFY_PAUSE: {res}")
    ui_queue.put(("info", f"> SPOTIFY_PAUSE\n\n{res}"))


@action('SPOTIFY_SKIP', family='SPOTIFY_SKIP')
def _run_spotify_skip(ctx):
    """Handles SPOTIFY_SKIP."""
    action_type = ctx.action_type
    attrs = ctx.attrs
    ui_queue = ctx.ui_queue
    base_dir = ctx.base_dir
    origin = ctx.origin
    turn_id = ctx.turn_id
    interrupt_event = ctx.interrupt_event
    element_map = ctx.element_map
    response_text = ctx.response_text
    from providers import spotify_api
    res = spotify_api.spotify_skip(base_dir)
    execute_actions._last_output = res
    print(f"[Agent] SPOTIFY_SKIP: {res}")
    ui_queue.put(("info", f"> SPOTIFY_SKIP\n\n{res}"))


@action('READ_CALENDAR', family='READ_CALENDAR')
def _run_read_calendar(ctx):
    """Handles READ_CALENDAR."""
    action_type = ctx.action_type
    attrs = ctx.attrs
    ui_queue = ctx.ui_queue
    base_dir = ctx.base_dir
    origin = ctx.origin
    turn_id = ctx.turn_id
    interrupt_event = ctx.interrupt_event
    element_map = ctx.element_map
    response_text = ctx.response_text
    try:
        days_back = int(attrs.get('days_back', 0))
        days_ahead = int(attrs.get('days_ahead', 14))
        from providers import google_api
        res = google_api.read_calendar(base_dir, max_results=5, days_back=days_back, days_ahead=days_ahead)
        execute_actions._last_output = res
        print(f"[Agent] READ_CALENDAR:\n{res}")
        ui_queue.put(("info", f"> READ_CALENDAR\n\n{res}"))
    except Exception as e:
        err = f"[Error] READ_CALENDAR failed: {e}"
        execute_actions._last_output = err
        print(err)
        ui_queue.put(("info", f"> READ_CALENDAR\n\n{err}"))


@action('ADD_CALENDAR_EVENT', family='ADD_CALENDAR_EVENT')
def _run_add_calendar_event(ctx):
    """Handles ADD_CALENDAR_EVENT."""
    action_type = ctx.action_type
    attrs = ctx.attrs
    ui_queue = ctx.ui_queue
    base_dir = ctx.base_dir
    origin = ctx.origin
    turn_id = ctx.turn_id
    interrupt_event = ctx.interrupt_event
    element_map = ctx.element_map
    response_text = ctx.response_text
    try:
        start_time = attrs.get('start_time', '')
        end_time = attrs.get('end_time', '')
        event_date = attrs.get('date', '')
        summary = attrs.get('summary', '')
        # How many calendar events this one reply asks for. It used to be
        # counted once in execute_actions and read from here; after the body
        # moved into a handler that name no longer existed, which would have
        # turned the bulk-creation guard into a NameError -- and the guard is
        # the only thing standing between a confused reply and twelve events
        # in his calendar.
        calendar_create_count = _calendar_create_count(response_text)
        purpose = attrs.get('purpose', '').strip().lower()
        allowed_purposes = {
            'user_commitment', 'user_reminder', 'deadline',
        }
        if purpose not in allowed_purposes:
            execute_actions._last_output = (
                "[Calendar Policy] Calendar is for something "
                "Himanshu must personally attend, prepare for, "
                "decide, or do. Background checks belong in a "
                "durable WATCH, not his calendar.")
        elif summary and (event_date or (start_time and end_time)):
            from providers import google_api
            res = google_api.add_calendar_event(
                base_dir, start_time, end_time, summary,
                event_date=event_date,
                location=attrs.get('location', ''),
                description=attrs.get('description', ''))
            # The cached calendar the conflict check reads is now
            # stale: it would warn about an event just removed, or
            # miss one just added.
            try:
                import schedule as _schedule
                _schedule.invalidate_cache()
            except Exception:
                pass

            execute_actions._last_output = res
            print(f"[Agent] ADD_CALENDAR_EVENT: {res}")
            ui_queue.put(("info", f"> ADD_CALENDAR_EVENT\n\n{res}"))
        else:
            execute_actions._last_output = "Error: provide summary plus either date or both start_time and end_time"
    except Exception as e:
        err = f"[Error] ADD_CALENDAR_EVENT failed: {e}"
        execute_actions._last_output = err
        print(err)
        ui_queue.put(("info", f"> ADD_CALENDAR_EVENT\n\n{err}"))


@action('LIST_CALENDAR_EVENTS', family='LIST_CALENDAR_EVENTS')
def _run_list_calendar_events(ctx):
    """Handles LIST_CALENDAR_EVENTS."""
    action_type = ctx.action_type
    attrs = ctx.attrs
    ui_queue = ctx.ui_queue
    base_dir = ctx.base_dir
    origin = ctx.origin
    turn_id = ctx.turn_id
    interrupt_event = ctx.interrupt_event
    element_map = ctx.element_map
    response_text = ctx.response_text
    try:
        days_back = int(attrs.get('days_back', 0))
        days_ahead = int(attrs.get('days_ahead', 14))
        from providers import google_api
        res = google_api.list_calendar_events_with_ids(base_dir, max_results=10, days_back=days_back, days_ahead=days_ahead)
        execute_actions._last_output = res
        print(f"[Agent] LIST_CALENDAR_EVENTS:\n{res}")
        ui_queue.put(("info", f"> LIST_CALENDAR_EVENTS\n\n{res}"))
    except Exception as e:
        err = f"[Error] LIST_CALENDAR_EVENTS failed: {e}"
        execute_actions._last_output = err
        print(err)
        ui_queue.put(("info", f"> LIST_CALENDAR_EVENTS\n\n{err}"))


@action('LIST_TASKS', family='LIST_TASKS')
def _run_list_tasks(ctx):
    """Handles LIST_TASKS."""
    action_type = ctx.action_type
    attrs = ctx.attrs
    ui_queue = ctx.ui_queue
    base_dir = ctx.base_dir
    origin = ctx.origin
    turn_id = ctx.turn_id
    interrupt_event = ctx.interrupt_event
    element_map = ctx.element_map
    response_text = ctx.response_text
    try:
        max_results = int(attrs.get('max_results', 10))
        from providers import google_api
        res = google_api.list_google_tasks(
            base_dir, max_results=max_results,
            tasklist=attrs.get('list', '') or attrs.get('tasklist', ''),
            include_completed=attrs.get('include_completed', '').strip().lower() == 'true',
            all_lists=attrs.get('all_lists', '').strip().lower() == 'true',
        )
        execute_actions._last_output = res
        print(f"[Agent] LIST_TASKS:\n{res}")
        ui_queue.put(("info", f"> LIST_TASKS\n\n{res}"))
    except Exception as e:
        err = f"[Error] LIST_TASKS failed: {e}"
        execute_actions._last_output = err
        print(err)
        ui_queue.put(("info", f"> LIST_TASKS\n\n{err}"))


@action('ADD_TASK', family='ADD_TASK')
def _run_add_task(ctx):
    """Handles ADD_TASK."""
    action_type = ctx.action_type
    attrs = ctx.attrs
    ui_queue = ctx.ui_queue
    base_dir = ctx.base_dir
    origin = ctx.origin
    turn_id = ctx.turn_id
    interrupt_event = ctx.interrupt_event
    element_map = ctx.element_map
    response_text = ctx.response_text
    try:
        title = attrs.get('title', '')
        notes = attrs.get('notes', '')
        due = attrs.get('due', '')
        if title:
            from providers import google_api
            res = google_api.add_google_task(
                base_dir, title, notes, due_date=due,
                tasklist=attrs.get('list', '') or attrs.get('tasklist', ''),
            )
            execute_actions._last_output = res
            print(f"[Agent] ADD_TASK: {res}")
            ui_queue.put(("info", f"> ADD_TASK\n\n{res}"))
        else:
            execute_actions._last_output = "Error: title is required for ADD_TASK"
    except Exception as e:
        err = f"[Error] ADD_TASK failed: {e}"
        execute_actions._last_output = err
        print(err)
        ui_queue.put(("info", f"> ADD_TASK\n\n{err}"))


@action('ADD_TASKS', family='ADD_TASKS')
def _run_add_tasks(ctx):
    """Handles ADD_TASKS."""
    action_type = ctx.action_type
    attrs = ctx.attrs
    ui_queue = ctx.ui_queue
    base_dir = ctx.base_dir
    origin = ctx.origin
    turn_id = ctx.turn_id
    interrupt_event = ctx.interrupt_event
    element_map = ctx.element_map
    response_text = ctx.response_text
    try:
        raw_tasks = _tag_body(response_text, 'ADD_TASKS') or attrs.get('tasks', '')
        tasks = json.loads(html.unescape(raw_tasks))
        if not isinstance(tasks, list) or not tasks:
            raise ValueError("tasks must be a non-empty JSON array")
        if len(tasks) > 50:
            raise ValueError("one batch may contain at most 50 tasks")
        from providers import google_api
        outputs, created = [], 0
        default_list = attrs.get('list', '') or attrs.get('tasklist', '')
        for index, item in enumerate(tasks, 1):
            if not isinstance(item, dict) or not str(item.get('title', '')).strip():
                outputs.append(f"[{index}] FAILED: missing title")
                continue
            try:
                result = google_api.add_google_task(
                    base_dir, str(item['title']).strip(),
                    str(item.get('notes', '') or ''),
                    due_date=str(item.get('due', '') or ''),
                    tasklist=str(item.get('list', '') or default_list))
                ok = not str(result).lstrip().startswith(('[Error]', 'Error:'))
                created += int(ok)
                outputs.append(f"[{index}] {'OK' if ok else 'FAILED'}: {result}")
            except Exception as item_error:
                outputs.append(f"[{index}] FAILED: {item_error}")
        status = "Success" if created == len(tasks) else "Partial"
        res = (f"[{status}] Created {created}/{len(tasks)} Google Tasks.\n" +
               "\n".join(outputs))
        execute_actions._last_output = res
        print(f"[Agent] ADD_TASKS: {created}/{len(tasks)} created")
        ui_queue.put(("info", f"> ADD_TASKS\n\n{res}"))
    except Exception as e:
        err = f"[Error] ADD_TASKS failed: {e}"
        execute_actions._last_output = err
        print(err)
        ui_queue.put(("info", f"> ADD_TASKS\n\n{err}"))


@action('COMPLETE_TASK', family='COMPLETE_TASK')
def _run_complete_task(ctx):
    """Handles COMPLETE_TASK."""
    action_type = ctx.action_type
    attrs = ctx.attrs
    ui_queue = ctx.ui_queue
    base_dir = ctx.base_dir
    origin = ctx.origin
    turn_id = ctx.turn_id
    interrupt_event = ctx.interrupt_event
    element_map = ctx.element_map
    response_text = ctx.response_text
    try:
        from providers import google_api
        res = google_api.complete_google_task(
            base_dir, attrs.get('task_id', '') or attrs.get('id', ''),
            tasklist=attrs.get('list', '') or attrs.get('tasklist', ''),
        )
        execute_actions._last_output = res
        print(f"[Agent] COMPLETE_TASK: {res}")
        ui_queue.put(("info", f"> COMPLETE_TASK\n\n{res}"))
    except Exception as e:
        err = f"[Error] COMPLETE_TASK failed: {e}"
        execute_actions._last_output = err
        print(err)
        ui_queue.put(("info", f"> COMPLETE_TASK\n\n{err}"))


@action('UPDATE_TASK', family='UPDATE_TASK')
def _run_update_task(ctx):
    """Handles UPDATE_TASK."""
    action_type = ctx.action_type
    attrs = ctx.attrs
    ui_queue = ctx.ui_queue
    base_dir = ctx.base_dir
    origin = ctx.origin
    turn_id = ctx.turn_id
    interrupt_event = ctx.interrupt_event
    element_map = ctx.element_map
    response_text = ctx.response_text
    try:
        from providers import google_api
        res = google_api.update_google_task(
            base_dir, attrs.get('task_id', '') or attrs.get('id', ''),
            title=attrs.get('title', ''), notes=attrs.get('notes', ''),
            due_date=attrs.get('due', ''),
            tasklist=attrs.get('list', '') or attrs.get('tasklist', ''),
        )
        execute_actions._last_output = res
        print(f"[Agent] UPDATE_TASK: {res}")
        ui_queue.put(("info", f"> UPDATE_TASK\n\n{res}"))
    except Exception as e:
        err = f"[Error] UPDATE_TASK failed: {e}"
        execute_actions._last_output = err
        print(err)
        ui_queue.put(("info", f"> UPDATE_TASK\n\n{err}"))


@action('DELETE_TASK', family='DELETE_TASK')
def _run_delete_task(ctx):
    """Delete one exact Google Task selected by its stable ID."""
    from providers import google_api
    result = google_api.delete_google_task(
        ctx.base_dir,
        ctx.attr('task_id') or ctx.attr('id'),
        tasklist=ctx.attr('list') or ctx.attr('tasklist'),
    )
    ctx.report(result)
    ctx.say(f"> DELETE_TASK\n\n{result}")


@action('UPDATE_CALENDAR_EVENT', 'FIND_FREE_SLOTS', 'MARK_EMAIL_READ', 'ARCHIVE_EMAIL', family='UPDATE_CALENDAR_EVENT')
def _run_update_calendar_event(ctx):
    """Handles UPDATE_CALENDAR_EVENT, FIND_FREE_SLOTS, MARK_EMAIL_READ, ARCHIVE_EMAIL."""
    action_type = ctx.action_type
    attrs = ctx.attrs
    ui_queue = ctx.ui_queue
    base_dir = ctx.base_dir
    origin = ctx.origin
    turn_id = ctx.turn_id
    interrupt_event = ctx.interrupt_event
    element_map = ctx.element_map
    response_text = ctx.response_text
    try:
        from providers import google_api

        def _int(key, default):
            try:
                return int(float(attrs.get(key, default)))
            except (TypeError, ValueError):
                return default

        # Variant selection, as a table. Each closure is the
        # body that used to be a rung of an inner ladder.
        res = locals().get('res')
        def _v_0_update_calendar_event():
            nonlocal res
            res = google_api.update_calendar_event(
                base_dir, attrs.get('event_id', ''),
                start_time=attrs.get('start_time', ''),
                end_time=attrs.get('end_time', ''),
                summary=attrs.get('summary', ''),
                location=attrs.get('location', ''),
                description=attrs.get('description', ''))
            # The cached calendar the conflict check reads is now stale: it
            # would warn about an event just removed, or miss one just added.
            try:
                import schedule as _schedule
                _schedule.invalidate_cache()
            except Exception:
                pass
        def _v_1_find_free_slots():
            nonlocal res
            res = google_api.find_free_slots(
                base_dir, days_ahead=_int('days_ahead', 7),
                duration_minutes=_int('duration_minutes', 60),
                day_start_hour=_int('day_start_hour', 9),
                day_end_hour=_int('day_end_hour', 21))
        def _v_default():
            nonlocal res
            res = google_api.modify_email(
                base_dir, attrs.get('message_id', '') or attrs.get('id', ''),
                mark_read=action_type == 'MARK_EMAIL_READ',
                archive=action_type == 'ARCHIVE_EMAIL')
        _variants = {
            'UPDATE_CALENDAR_EVENT': _v_0_update_calendar_event,
            'FIND_FREE_SLOTS': _v_1_find_free_slots,
        }
        _variants.get(action_type, _v_default)()
        execute_actions._last_output = res
        print(f"[Agent] {action_type}: {res.splitlines()[0][:120]}")
        ui_queue.put(("info", f"> {action_type}\n\n{res}"))
    except Exception as e:
        err = f"[Error] {action_type} failed: {e}"
        execute_actions._last_output = err
        print(err)
        ui_queue.put(("info", f"> {action_type}\n\n{err}"))


@action('LIST_TASK_LISTS', family='LIST_TASK_LISTS')
def _run_list_task_lists(ctx):
    """Handles LIST_TASK_LISTS."""
    action_type = ctx.action_type
    attrs = ctx.attrs
    ui_queue = ctx.ui_queue
    base_dir = ctx.base_dir
    origin = ctx.origin
    turn_id = ctx.turn_id
    interrupt_event = ctx.interrupt_event
    element_map = ctx.element_map
    response_text = ctx.response_text
    try:
        from providers import google_api
        res = google_api.list_task_lists(base_dir)
        execute_actions._last_output = res
        print(f"[Agent] LIST_TASK_LISTS:\n{res}")
        ui_queue.put(("info", f"> LIST_TASK_LISTS\n\n{res}"))
    except Exception as e:
        err = f"[Error] LIST_TASK_LISTS failed: {e}"
        execute_actions._last_output = err
        print(err)
        ui_queue.put(("info", f"> LIST_TASK_LISTS\n\n{err}"))


@action('TRACK_COMMITMENT', family='TRACK_COMMITMENT')
def _run_track_commitment(ctx):
    """Handles TRACK_COMMITMENT."""
    action_type = ctx.action_type
    attrs = ctx.attrs
    ui_queue = ctx.ui_queue
    base_dir = ctx.base_dir
    origin = ctx.origin
    turn_id = ctx.turn_id
    interrupt_event = ctx.interrupt_event
    element_map = ctx.element_map
    response_text = ctx.response_text
    try:
        import commitments
        what = attrs.get('what', '')
        hard_time = attrs.get('hard_time', '')
        location = attrs.get('location', '')
        prep = int(attrs.get('prep_minutes', '0'))
        travel = int(attrs.get('travel_minutes', '0'))
        if what and hard_time:
            res = commitments.add(base_dir, {
                "what": what,
                "hard_time": hard_time,
                "location": location,
                "prep_minutes": prep,
                "travel_minutes": travel,
            })
            execute_actions._last_output = res
            print(f"[Agent] TRACK_COMMITMENT:\n{res}")
            ui_queue.put(("info", f"> TRACK_COMMITMENT\n\n{res}"))
        else:
            execute_actions._last_output = "Error: missing 'what' or 'hard_time'."
    except Exception as e:
        err = f"[Error] TRACK_COMMITMENT failed: {e}"
        execute_actions._last_output = err
        print(err)


@action('LIST_COMMITMENTS', family='LIST_COMMITMENTS')
def _run_list_commitments(ctx):
    """Handles LIST_COMMITMENTS."""
    action_type = ctx.action_type
    attrs = ctx.attrs
    ui_queue = ctx.ui_queue
    base_dir = ctx.base_dir
    origin = ctx.origin
    turn_id = ctx.turn_id
    interrupt_event = ctx.interrupt_event
    element_map = ctx.element_map
    response_text = ctx.response_text
    try:
        import commitments
        res = commitments.due_soon(base_dir, hours=int(attrs.get('hours', '168')))
        execute_actions._last_output = res
        print(f"[Agent] LIST_COMMITMENTS:\n{res}")
        ui_queue.put(("info", f"> LIST_COMMITMENTS\n\n{res}"))
    except Exception as e:
        err = f"[Error] LIST_COMMITMENTS failed: {e}"
        execute_actions._last_output = err
        print(err)


@action('COMPLETE_COMMITMENT', family='COMPLETE_COMMITMENT')
def _run_complete_commitment(ctx):
    """Handles COMPLETE_COMMITMENT."""
    action_type = ctx.action_type
    attrs = ctx.attrs
    ui_queue = ctx.ui_queue
    base_dir = ctx.base_dir
    origin = ctx.origin
    turn_id = ctx.turn_id
    interrupt_event = ctx.interrupt_event
    element_map = ctx.element_map
    response_text = ctx.response_text
    try:
        import commitments
        cid = attrs.get('id', '')
        res = commitments.complete(base_dir, cid) if cid else "Error: missing 'id'. Use LIST_COMMITMENTS first."
        execute_actions._last_output = res
        print(f"[Agent] COMPLETE_COMMITMENT:\n{res}")
        ui_queue.put(("info", f"> COMPLETE_COMMITMENT\n\n{res}"))
    except Exception as e:
        err = f"[Error] COMPLETE_COMMITMENT failed: {e}"
        execute_actions._last_output = err
        print(err)


@action('GET_PASSWORD', family='GET_PASSWORD')
def _run_get_password(ctx):
    """Handles GET_PASSWORD."""
    action_type = ctx.action_type
    attrs = ctx.attrs
    ui_queue = ctx.ui_queue
    base_dir = ctx.base_dir
    origin = ctx.origin
    turn_id = ctx.turn_id
    interrupt_event = ctx.interrupt_event
    element_map = ctx.element_map
    response_text = ctx.response_text
    try:
        key = attrs.get('key', '').lower()
        pw_file = os.path.join(base_dir, "vault", "passwords.json")
        if os.path.exists(pw_file):
            with open(pw_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
            found = False
            for k, v in data.items():
                if key in k.lower():
                    execute_actions._last_output = f"Password for '{k}': {v}"
                    found = True
                    break
            if not found:
                execute_actions._last_output = f"No password found matching '{key}'"
        else:
            execute_actions._last_output = "Error: passwords.json not found."
    except Exception as e:
        execute_actions._last_output = f"[Error] GET_PASSWORD failed: {e}"


@action('DELETE_CALENDAR_EVENT', family='DELETE_CALENDAR_EVENT')
def _run_delete_calendar_event(ctx):
    """Handles DELETE_CALENDAR_EVENT."""
    action_type = ctx.action_type
    attrs = ctx.attrs
    ui_queue = ctx.ui_queue
    base_dir = ctx.base_dir
    origin = ctx.origin
    turn_id = ctx.turn_id
    interrupt_event = ctx.interrupt_event
    element_map = ctx.element_map
    response_text = ctx.response_text
    try:
        event_id = attrs.get('event_id', '')
        if event_id:
            from providers import google_api
            res = google_api.delete_calendar_event(base_dir, event_id)
            # The cached calendar the conflict check reads is now
            # stale: it would warn about an event just removed, or
            # miss one just added.
            try:
                import schedule as _schedule
                _schedule.invalidate_cache()
            except Exception:
                pass

            execute_actions._last_output = res
            print(f"[Agent] DELETE_CALENDAR_EVENT: {res}")
            ui_queue.put(("info", f"> DELETE_CALENDAR_EVENT\n\n{res}"))
        else:
            execute_actions._last_output = "Error: missing event_id. Use LIST_CALENDAR_EVENTS first to get IDs."
    except Exception as e:
        err = f"[Error] DELETE_CALENDAR_EVENT failed: {e}"
        execute_actions._last_output = err
        print(err)
        ui_queue.put(("info", f"> DELETE_CALENDAR_EVENT\n\n{err}"))


@action('LOOKUP_CONTACTS', family='LOOKUP_CONTACTS')
def _run_lookup_contacts(ctx):
    """Handles LOOKUP_CONTACTS."""
    action_type = ctx.action_type
    attrs = ctx.attrs
    ui_queue = ctx.ui_queue
    base_dir = ctx.base_dir
    origin = ctx.origin
    turn_id = ctx.turn_id
    interrupt_event = ctx.interrupt_event
    element_map = ctx.element_map
    response_text = ctx.response_text
    requested = attrs.get('names', '') or attrs.get('name', '')
    names = [part.strip() for part in re.split(r'[,;|]', requested) if part.strip()]
    if not names:
        execute_actions._last_output = "[Error] LOOKUP_CONTACTS requires one or more explicit names."
    else:
        try:
            from memory.contacts import lookup_contacts
            rows = lookup_contacts(names)
            found = [f"{row['name']}: {row['phone']}" for row in rows if row['found']]
            missing = [row['name'] for row in rows if not row['found']]
            parts = list(found)
            if missing:
                parts.append("Not found: " + ", ".join(missing))
            result = "\n".join(parts) if parts else "None of the requested contacts were found."
            execute_actions._last_output = f"[Success] Resolved {len(found)} of {len(rows)} requested contacts locally."
            set_direct_user_delivery(result, result)
            ui_queue.put(("info", f"> CONTACT LOOKUP\n\n{result}"))
        except Exception as exc:
            execute_actions._last_output = f"[Error] LOOKUP_CONTACTS failed: {exc}"
            ui_queue.put(("info", execute_actions._last_output))


@action('CHECK_GOOGLE_CONNECTION', family='CHECK_GOOGLE_CONNECTION')
def _run_check_google_connection(ctx):
    """Handles CHECK_GOOGLE_CONNECTION."""
    action_type = ctx.action_type
    attrs = ctx.attrs
    ui_queue = ctx.ui_queue
    base_dir = ctx.base_dir
    origin = ctx.origin
    turn_id = ctx.turn_id
    interrupt_event = ctx.interrupt_event
    element_map = ctx.element_map
    response_text = ctx.response_text
    from providers.google_api import google_connection_status
    status = google_connection_status(base_dir)
    result = status['message']
    execute_actions._last_output = ("[Success] " if status['ok'] else "[Attention] ") + result
    set_direct_user_delivery(result, result)
    ui_queue.put(("info", result))


@action('DRIVE_READ', 'DRIVE_RECENT', 'DRIVE_RESTORE', 'DRIVE_COPY', 'DRIVE_CREATE_FOLDER', 'DRIVE_DOWNLOAD', 'DRIVE_INFO', 'DRIVE_LIST', 'DRIVE_MOVE', 'DRIVE_RENAME', 'DRIVE_SEARCH', 'DRIVE_SHARE', 'DRIVE_TRASH', 'DRIVE_UPLOAD', family='DRIVE_COPY')
def _run_drive_copy(ctx):
    """Handles DRIVE_COPY, DRIVE_CREATE_FOLDER, DRIVE_DOWNLOAD, DRIVE_INFO...."""
    action_type = ctx.action_type
    attrs = ctx.attrs
    ui_queue = ctx.ui_queue
    base_dir = ctx.base_dir
    origin = ctx.origin
    turn_id = ctx.turn_id
    interrupt_event = ctx.interrupt_event
    element_map = ctx.element_map
    response_text = ctx.response_text
    from providers import drive_api
    # Variant selection, as a table. Each closure is the
    # body that used to be a rung of an inner ladder.
    data = locals().get('data')
    result = locals().get('result')
    def _v_0_drive_list():
        nonlocal data, result
        data = drive_api.list_files(
            base_dir, attrs.get('folder_id', 'root'),
            attrs.get('limit', 50), attrs.get('page_token') or None)
        result = data['text']
    def _v_1_drive_search():
        nonlocal data, result
        data = drive_api.search_files(
            base_dir, attrs.get('query', ''), attrs.get('limit', 30),
            attrs.get('mime_type') or None, attrs.get('folder_id') or None)
        result = data['text']
    def _v_2_drive_info():
        nonlocal data, result
        result = drive_api.file_info(base_dir, attrs['file_id'])['text']
    def _v_3_drive_download():
        nonlocal data, result
        result = drive_api.download_file(
            base_dir, attrs['file_id'], attrs.get('destination', ''),
            attrs.get('overwrite', '').strip().lower() == 'true')
    def _v_4_drive_upload():
        nonlocal data, result
        result = drive_api.upload_file(
            base_dir, attrs['path'], attrs.get('parent_id', 'root'),
            attrs.get('name', ''), attrs.get('mime_type', ''))
    def _v_5_drive_create_folder():
        nonlocal data, result
        result = drive_api.create_folder(
            base_dir, attrs['name'], attrs.get('parent_id', 'root'))
    def _v_6_drive_rename():
        nonlocal data, result
        result = drive_api.rename_item(base_dir, attrs['file_id'], attrs['name'])
    def _v_7_drive_move():
        nonlocal data, result
        result = drive_api.move_item(base_dir, attrs['file_id'], attrs['parent_id'])
    def _v_8_drive_copy():
        nonlocal data, result
        result = drive_api.copy_item(
            base_dir, attrs['file_id'], attrs.get('name', ''),
            attrs.get('parent_id', ''))
    def _v_9_drive_trash():
        nonlocal data, result
        result = drive_api.trash_item(base_dir, attrs['file_id'])
    def _v_10_drive_share():
        nonlocal data, result
        result = drive_api.share_item(
            base_dir, attrs['file_id'], attrs['email'],
            attrs.get('role', 'reader'))
    def _v_11_drive_read():
        nonlocal data, result
        try:
            chars = int(attrs.get('max_chars') or 12000)
        except ValueError:
            chars = 12000
        result = drive_api.read_file(base_dir, attrs['file_id'], chars)['text']
    def _v_12_drive_recent():
        nonlocal data, result
        result = drive_api.recent_files(
            base_dir, attrs.get('limit', 15), attrs.get('days', 14),
            attrs.get('mime_type') or None)['text']
    def _v_13_drive_restore():
        nonlocal data, result
        result = drive_api.restore_item(base_dir, attrs['file_id'])
    def _v_default():
        nonlocal data, result
        result = f"[Error] Unsupported Drive action: {action_type}"
    _variants = {
        'DRIVE_LIST': _v_0_drive_list,
        'DRIVE_SEARCH': _v_1_drive_search,
        'DRIVE_INFO': _v_2_drive_info,
        'DRIVE_DOWNLOAD': _v_3_drive_download,
        'DRIVE_UPLOAD': _v_4_drive_upload,
        'DRIVE_CREATE_FOLDER': _v_5_drive_create_folder,
        'DRIVE_RENAME': _v_6_drive_rename,
        'DRIVE_MOVE': _v_7_drive_move,
        'DRIVE_COPY': _v_8_drive_copy,
        'DRIVE_TRASH': _v_9_drive_trash,
        'DRIVE_SHARE': _v_10_drive_share,
        'DRIVE_READ': _v_11_drive_read,
        'DRIVE_RECENT': _v_12_drive_recent,
        'DRIVE_RESTORE': _v_13_drive_restore,
    }
    _variants.get(action_type, _v_default)()
    execute_actions._last_output = result
    # Search results and file receipts are appropriate for the
    # dashboard; speech stays short and lets the turn model decide
    # which details actually answer the request.
    ui_queue.put(("info", f"> {action_type}\n\n{result}"))


def _office_verify(kind, result):
    """Re-open what was written and report what is really in it."""
    path = result["path"]
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        raise RuntimeError(f"the file was not written: {path}")
    if kind == "CREATE_PRESENTATION":
        from pptx import Presentation
        count = len(Presentation(path).slides)
        return f"[Success] Saved a {count}-slide presentation ({result.get('theme')} theme) to {path}."
    if kind == "CREATE_DOCUMENT":
        from docx import Document
        Document(path)
        return f"[Success] Saved a Word document (~{result.get('words', 0)} words) to {path}."
    if kind == "CREATE_SPREADSHEET":
        from openpyxl import load_workbook
        book = load_workbook(path)
        names = ", ".join(f"{s['sheet']} ({s['rows']} rows x {s['columns']} cols)"
                          for s in result["sheets"])
        return f"[Success] Saved a workbook with {len(book.sheetnames)} sheet(s): {names}. File: {path}."
    import pymupdf
    with pymupdf.open(path) as pdf:
        return f"[Success] Saved a {pdf.page_count}-page PDF to {path}."


@action('CREATE_PRESENTATION', 'CREATE_DOCUMENT', 'CREATE_SPREADSHEET', 'CREATE_PDF',
        'EXTRACT_TEXT', family='CREATE_PRESENTATION')
def _run_office(ctx):
    """Makes and reads Office-style files from Markdown."""
    import office_docs
    kind = ctx.action_type
    attrs = ctx.attrs
    try:
        if kind == 'EXTRACT_TEXT':
            try:
                limit = max(500, min(int(attrs.get('max_chars') or 12000), 60000))
            except ValueError:
                limit = 12000
            result = office_docs.extract_text(attrs.get('path', ''), limit)
        else:
            body = (_tag_body(ctx.response_text, kind) or attrs.get('content', '')
                    or attrs.get('outline', '')).strip()
            if not body:
                raise ValueError(
                    "there is no content: write the outline as Markdown between "
                    f"<ACTION type=\"{kind}\" ...> and </ACTION>")
            title = attrs.get('title', '')
            extension = {'CREATE_PRESENTATION': 'pptx', 'CREATE_DOCUMENT': 'docx',
                         'CREATE_SPREADSHEET': 'xlsx', 'CREATE_PDF': 'pdf'}[kind]
            first_heading = next((l.lstrip('# ').strip() for l in body.splitlines()
                                  if l.startswith('# ')), '')
            out = office_docs.resolve_output(
                attrs.get('path', ''), title or first_heading or 'untitled', extension,
                ctx.base_dir)
            near = out.parent
            if kind == 'CREATE_PRESENTATION':
                built = office_docs.build_presentation(
                    body, title=title, theme=attrs.get('theme', 'dark'),
                    footer=attrs.get('footer'), author=attrs.get('author', ''),
                    output=out, near=near)
            elif kind == 'CREATE_DOCUMENT':
                built = office_docs.build_document(
                    body, title=title, author=attrs.get('author', ''), output=out, near=near)
            elif kind == 'CREATE_SPREADSHEET':
                built = office_docs.build_spreadsheet(body, title=title, output=out)
            else:
                built = office_docs.build_pdf(body, title=title, output=out, near=near)
            result = _office_verify(kind, built)
    except Exception as exc:
        result = f"[Error] {kind} failed: {type(exc).__name__}: {exc}"
    execute_actions._last_output = result
    ctx.ui_queue.put(("info", f"> {kind}\n\n{result[:1800]}"))


@action('OPEN_PATH', 'DOWNLOAD_FILE', 'ZIP_PATH', 'UNZIP_FILE', 'CALCULATE', 'TIME_IN',
        'NOTE_ADD', 'NOTES_LIST', 'NOTE_DELETE', 'PDF_MERGE', 'PDF_EXTRACT',
        family='OPEN_PATH')
def _run_life_tools(ctx):
    """The everyday tools: open, download, zip, calculate, time, notes, PDFs."""
    import life_tools
    kind = ctx.action_type
    attrs = ctx.attrs
    base = ctx.base_dir
    try:
        if kind == 'OPEN_PATH':
            result = life_tools.open_path(attrs.get('path', ''),
                                          attrs.get('reveal', '').strip().lower() == 'true')
        elif kind == 'DOWNLOAD_FILE':
            result = life_tools.download_file(attrs.get('url', ''), attrs.get('destination', ''),
                                              attrs.get('name', ''))
        elif kind == 'ZIP_PATH':
            result = life_tools.zip_path(attrs.get('path', ''), attrs.get('destination', ''))
        elif kind == 'UNZIP_FILE':
            result = life_tools.unzip_file(attrs.get('path', ''), attrs.get('destination', ''))
        elif kind == 'CALCULATE':
            result = life_tools.calculate(attrs.get('expression', ''))
        elif kind == 'TIME_IN':
            result = life_tools.time_in(attrs.get('place', ''), attrs.get('at', ''))
        elif kind == 'NOTE_ADD':
            result = life_tools.add_note(base, attrs.get('text', '') or _tag_body(ctx.response_text, kind),
                                         attrs.get('tags', ''))
        elif kind == 'NOTES_LIST':
            result = life_tools.list_notes(base, attrs.get('query', ''), attrs.get('limit', 10))
        elif kind == 'NOTE_DELETE':
            result = life_tools.delete_note(base, attrs.get('id', ''))
        elif kind == 'PDF_MERGE':
            result = life_tools.pdf_merge(attrs.get('paths', ''), attrs.get('name', ''))
        else:
            result = life_tools.pdf_extract(attrs.get('path', ''), attrs.get('pages', ''),
                                            attrs.get('name', ''))
    except Exception as exc:
        result = f"[Error] {kind} failed: {type(exc).__name__}: {exc}"
    execute_actions._last_output = result
    ctx.ui_queue.put(("info", f"> {kind}\n\n{result[:1800]}"))


@action('GET_SELECTION', family='GET_SELECTION')
def _run_get_selection(ctx):
    """Reads what he has highlighted, without a screenshot."""
    import selection
    grabbed = selection.grab(allow_copy=True)
    result = selection.describe(grabbed)
    execute_actions._last_output = result
    ctx.ui_queue.put(("info", f"> GET_SELECTION\n\n{result[:1500]}"))


@action('OPEN_BRAIN_MAP', family='OPEN_BRAIN_MAP')
def _run_open_brain_map(ctx):
    """Starts the local graph server and opens the 3D map in a browser."""
    import brain_server
    try:
        info = brain_server.ensure_running(ctx.base_dir)
        focus = ctx.attrs.get('focus', '')
        address = brain_server.open_in_browser(info, focus)
        result = (f"[Success] The brain map is open in his browser "
                  f"({info['nodes']} nodes, {info['edges']} links)."
                  if info.get('nodes') is not None else
                  f"[Attention] The brain map server is running at {address}, but the "
                  f"graph database did not answer: {info.get('error', 'unavailable')}.")
    except Exception as exc:
        result = f"[Error] Could not start the brain map: {type(exc).__name__}: {exc}"
    execute_actions._last_output = result
    ctx.ui_queue.put(("info", f"> OPEN_BRAIN_MAP\n\n{result}"))


@action('PEOPLE_SEARCH', 'PEOPLE_GET', 'PEOPLE_CREATE', 'PEOPLE_UPDATE',
        'PEOPLE_DELETE', family='PEOPLE_SEARCH')
def _run_people(ctx):
    """Handles the Google People (Contacts) actions."""
    attrs = ctx.attrs
    base_dir = ctx.base_dir
    action_type = ctx.action_type
    from providers import people_api
    contact_id = attrs.get('contact_id', '') or attrs.get('id', '')
    try:
        if action_type == 'PEOPLE_SEARCH':
            result = people_api.search_people(
                base_dir, attrs.get('query', '') or attrs.get('name', ''),
                attrs.get('limit', 10))['text']
        elif action_type == 'PEOPLE_GET':
            result = people_api.get_person(base_dir, contact_id)['text']
        elif action_type == 'PEOPLE_CREATE':
            result = people_api.create_person(
                base_dir, attrs.get('name', ''), attrs.get('phone', ''),
                attrs.get('email', ''), attrs.get('organization', ''),
                attrs.get('title', ''), attrs.get('notes', ''),
                attrs.get('birthday', ''))
        elif action_type == 'PEOPLE_UPDATE':
            result = people_api.update_person(
                base_dir, contact_id, attrs.get('name', ''),
                attrs.get('phone', ''), attrs.get('email', ''),
                attrs.get('organization', ''), attrs.get('title', ''),
                attrs.get('notes', ''), attrs.get('birthday', ''))
        elif action_type == 'PEOPLE_DELETE':
            result = people_api.delete_person(base_dir, contact_id)
        else:
            result = f"[Error] Unsupported People action: {action_type}"
    except (ValueError, people_api.ContactsWriteNotGranted) as exc:
        result = f"[Error] {exc}"
    except Exception as exc:
        result = f"[Error] {action_type} failed: {type(exc).__name__}: {exc}"
    execute_actions._last_output = result
    ctx.ui_queue.put(("info", f"> {action_type}\n\n{result}"))


@action('SYNC_GOOGLE_CONTACTS', family='SYNC_GOOGLE_CONTACTS')
def _run_sync_google_contacts(ctx):
    """Handles SYNC_GOOGLE_CONTACTS."""
    action_type = ctx.action_type
    attrs = ctx.attrs
    ui_queue = ctx.ui_queue
    base_dir = ctx.base_dir
    origin = ctx.origin
    turn_id = ctx.turn_id
    interrupt_event = ctx.interrupt_event
    element_map = ctx.element_map
    response_text = ctx.response_text
    from providers.google_api import sync_contacts
    count = sync_contacts(base_dir)
    result = f"Google Contacts sync completed with {count} contacts."
    execute_actions._last_output = f"[Success] {result}" if count else f"[Attention] {result}"
    set_direct_user_delivery(result, result)
    ui_queue.put(("info", result))


@action('LIST_RECENT_EVENTS', family='LIST_RECENT_EVENTS')
def _run_list_recent_events(ctx):
    """Handles LIST_RECENT_EVENTS."""
    action_type = ctx.action_type
    attrs = ctx.attrs
    ui_queue = ctx.ui_queue
    base_dir = ctx.base_dir
    origin = ctx.origin
    turn_id = ctx.turn_id
    interrupt_event = ctx.interrupt_event
    element_map = ctx.element_map
    response_text = ctx.response_text
    try:
        from providers.google_api import recent_completed_events
        rows = recent_completed_events(base_dir, hours_back=int(attrs.get('hours_back', 48) or 48))
        if rows:
            result = "Recently completed events:\n" + "\n".join(
                f"- {row['summary']} — {row['start']}" for row in rows)
        else:
            result = "No recently completed calendar events were found."
        execute_actions._last_output = f"[Success] Found {len(rows)} recent events locally."
        if rows:
            set_direct_user_delivery(
                result,
                "I found the recent events. The details are on your dashboard.",
                surface="dashboard", title="Recent events")
        else:
            set_direct_user_delivery(result, result)
        ui_queue.put(("info", result))
    except Exception as exc:
        execute_actions._last_output = f"[Error] Recent event lookup failed: {exc}"


@action('LIST_FOLLOWUPS', family='LIST_FOLLOWUPS')
def _run_list_followups(ctx):
    """Handles LIST_FOLLOWUPS."""
    action_type = ctx.action_type
    attrs = ctx.attrs
    ui_queue = ctx.ui_queue
    base_dir = ctx.base_dir
    origin = ctx.origin
    turn_id = ctx.turn_id
    interrupt_event = ctx.interrupt_event
    element_map = ctx.element_map
    response_text = ctx.response_text
    import followups
    result = followups.status(base_dir)
    execute_actions._last_output = "[Success] Follow-up ledger loaded locally."
    set_direct_user_delivery(result, result)
    ui_queue.put(("info", result))


@action('REAUTHORIZE_GOOGLE', family='REAUTHORIZE_GOOGLE')
def _run_reauthorize_google(ctx):
    """Handles REAUTHORIZE_GOOGLE."""
    action_type = ctx.action_type
    attrs = ctx.attrs
    ui_queue = ctx.ui_queue
    base_dir = ctx.base_dir
    origin = ctx.origin
    turn_id = ctx.turn_id
    interrupt_event = ctx.interrupt_event
    element_map = ctx.element_map
    response_text = ctx.response_text
    try:
        from providers.google_api import reauthorize_google
        result = reauthorize_google(base_dir)
        execute_actions._last_output = f"[Success] {result}"
        set_direct_user_delivery(result, result)
        ui_queue.put(("info", result))
    except Exception as exc:
        message = f"Google reauthorization failed: {exc}"
        execute_actions._last_output = f"[Error] {message}"
        set_direct_user_delivery(message, message)
        ui_queue.put(("info", message))


@action('READ_EMAILS', family='READ_EMAILS')
def _run_read_emails(ctx):
    """Handles READ_EMAILS."""
    action_type = ctx.action_type
    attrs = ctx.attrs
    ui_queue = ctx.ui_queue
    base_dir = ctx.base_dir
    origin = ctx.origin
    turn_id = ctx.turn_id
    interrupt_event = ctx.interrupt_event
    element_map = ctx.element_map
    response_text = ctx.response_text
    try:
        from providers import google_api
        import session_context
        query = attrs.get('query', '')
        res = google_api.read_emails(base_dir, max_results=5, query=query)
        if str(res).lstrip().startswith("[Error]"):
            ctx.report(res)
            ui_queue.put(("info", f"> READ_EMAILS\n\n{res}"))
            return

        # A private-result handle improves follow-up references but
        # is not required to retrieve mail. Neo4j being offline
        # must never turn a successful Gmail read into a failure.
        try:
            handle_ref = session_context.add_handle(
                f"READ_EMAILS result (query: {query})", res,
                slot="email")
        except Exception as graph_error:
            print(f"[Memory] Email handle unavailable; delivering mail locally: {graph_error}")
            handle_ref = (
                "READ_EMAILS result delivered locally "
                "(durable reference unavailable)")
        # The configured task model needs bounded evidence, not an opaque
        # handle it has no action to dereference.  Pass sender, subject,
        # attachment names and the provider's short snippet; never attachment
        # bytes or a full message body. The identity gate above this handler
        # still prevents a guest turn from reading any mailbox data.
        execute_actions._last_output = (
            f"[Success] {handle_ref}\n"
            + _email_digest_for_model(res))
        # Return one useful digest through the ordinary verified
        # completion path. The old direct payload dumped every
        # matching message onto the dashboard before the agent had
        # a chance to triage it.
        import email_briefing
        import llm_router
        records = email_briefing.from_google_result(res)
        verdict = email_briefing.triage(records, llm_router.call_llm)
        summary = (verdict or {}).get("summary")
        try:
            from email_memory import remember_emails
            remember_emails(records, verdict)
        except Exception as memory_error:
            print(f"[Email Memory] Could not persist triaged mail: {memory_error}")
        if records:
            summary = summary or "There is no useful new email that needs your attention."
            execute_actions._last_output += f"\nInbox summary: {summary}"
        execute_actions._direct_user_delivery = None
        execute_actions._direct_user_result = None
        execute_actions._direct_user_spoken = None
        print(f"[Agent] READ_EMAILS: summarized {len(records)} message(s).")
        ui_queue.put(("info", f"> READ_EMAILS\n\n{handle_ref}"))
    except Exception as e:
        err = f"[Error] READ_EMAILS failed: {e}"
        execute_actions._last_output = err
        print(err)
        ui_queue.put(("info", f"> READ_EMAILS\n\n{err}"))


@action('SEND_EMAIL', family='SEND_EMAIL')
def _run_send_email(ctx):
    """Send the exact approved message, including validated attachments."""
    from providers import google_api

    body = ctx.attr('body') or _tag_body(ctx.response_text, 'SEND_EMAIL')
    attachments = (ctx.attr('attachments') or ctx.attr('attachment')
                   or ctx.attr('file_path'))
    args = (ctx.base_dir, ctx.attr('to'), ctx.attr('subject'), body)
    result = (google_api.send_email(*args, attachments=attachments)
              if attachments else google_api.send_email(*args))
    ctx.report(result)
    ctx.say(f"> SEND_EMAIL\n\n{result}")


@action('REPLY_EMAIL', family='REPLY_EMAIL')
def _run_reply_email(ctx):
    """Reply inside the exact Gmail thread identified by READ_EMAILS."""
    from providers import google_api

    body = ctx.attr('body') or _tag_body(ctx.response_text, 'REPLY_EMAIL')
    attachments = (ctx.attr('attachments') or ctx.attr('attachment')
                   or ctx.attr('file_path'))
    args = (ctx.base_dir, ctx.attr('message_id'), body)
    result = (google_api.reply_email(*args, attachments=attachments)
              if attachments else google_api.reply_email(*args))
    ctx.report(result)
    ctx.say(f"> REPLY_EMAIL\n\n{result}")


@action('DOWNLOAD_EMAIL_ATTACHMENT', family='DOWNLOAD_EMAIL_ATTACHMENT')
def _run_download_email_attachment(ctx):
    """Download one attachment selected from a concrete Gmail message."""
    from providers import google_api
    result = google_api.download_email_attachment(
        ctx.base_dir,
        ctx.attr('message_id') or ctx.attr('id'),
        attachment_id=ctx.attr('attachment_id'),
        filename=ctx.attr('filename') or ctx.attr('name'),
        destination=ctx.attr('destination'),
    )
    ctx.report(result)
    ctx.say(f"> DOWNLOAD_EMAIL_ATTACHMENT\n\n{result}")


@action('WAIT', family='WAIT')
def _run_wait(ctx):
    """Handles WAIT."""
    action_type = ctx.action_type
    attrs = ctx.attrs
    ui_queue = ctx.ui_queue
    base_dir = ctx.base_dir
    origin = ctx.origin
    turn_id = ctx.turn_id
    interrupt_event = ctx.interrupt_event
    element_map = ctx.element_map
    response_text = ctx.response_text
    wait_time = float(attrs.get('time', 1.0))
    msg = f"Waiting for {wait_time}s"
    print(f"[Action] {msg}")
    ui_queue.put(("action", msg))
    deadline = time.monotonic() + max(0.0, wait_time)
    while time.monotonic() < deadline:
        if interrupt_event is not None and interrupt_event.is_set():
            execute_actions._last_output = "[Interrupted] Wait stopped to hear Himanshu."
            break
        time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))


@action('SEND_WHATSAPP', family='SEND_WHATSAPP')
def _run_send_whatsapp(ctx):
    """Handles SEND_WHATSAPP."""
    action_type = ctx.action_type
    attrs = ctx.attrs
    ui_queue = ctx.ui_queue
    base_dir = ctx.base_dir
    origin = ctx.origin
    turn_id = ctx.turn_id
    interrupt_event = ctx.interrupt_event
    element_map = ctx.element_map
    response_text = ctx.response_text
    contact = attrs.get('contact', '')
    message = attrs.get('message', '')
    file_path = attrs.get('file_path', '')
    allowed, err = _action_allowed("SEND_WHATSAPP", origin)
    if not allowed:
        execute_actions._last_output = err
    elif contact:
        if file_path and not message:
            execute_actions._last_output = f"[Error] Sending files via WhatsApp is not currently supported. You can only send text using the message='...' attribute."
            print(execute_actions._last_output)
            ui_queue.put(("info", f"> SEND_WHATSAPP\n\n{execute_actions._last_output}"))
        elif message:
            from providers import whatsapp
            try:
                res = whatsapp.send_message(contact, message, base_dir)
                execute_actions._last_output = res
                print(f"[Action] SEND_WHATSAPP: {res}")
                ui_queue.put(("info", f"> SEND_WHATSAPP\n\n{res}"))
            except Exception as e:
                err = f"[Error] SEND_WHATSAPP failed: {e}"
                execute_actions._last_output = err
                print(err)
                ui_queue.put(("info", f"> SEND_WHATSAPP\n\n{err}"))


@action('BROWSER_START', family='BROWSER_START')
def _run_browser_start(ctx):
    """Handles BROWSER_START."""
    action_type = ctx.action_type
    attrs = ctx.attrs
    ui_queue = ctx.ui_queue
    base_dir = ctx.base_dir
    origin = ctx.origin
    turn_id = ctx.turn_id
    interrupt_event = ctx.interrupt_event
    element_map = ctx.element_map
    response_text = ctx.response_text
    import browser
    execute_actions._last_output = browser.start(
        visible=not ctx.flag('background', False))


@action('BROWSER_TABS', family='BROWSER_TABS')
def _run_browser_tabs(ctx):
    """Handles BROWSER_TABS."""
    action_type = ctx.action_type
    attrs = ctx.attrs
    ui_queue = ctx.ui_queue
    base_dir = ctx.base_dir
    origin = ctx.origin
    turn_id = ctx.turn_id
    interrupt_event = ctx.interrupt_event
    element_map = ctx.element_map
    response_text = ctx.response_text
    import browser
    execute_actions._last_output = browser.tabs()


@action('YOUTUBE_SEARCH', family='YOUTUBE_SEARCH')
def _run_youtube_search(ctx):
    """Search YouTube through its official Data API, without opening tabs."""
    from providers import youtube_api
    result = youtube_api.search_receipt(
        ctx.attr('query'), ctx.attr('limit', 5), ctx.attr('order', 'relevance'))
    ctx.report(result)
    ctx.say(f"> YOUTUBE_SEARCH\n\n{result[:1600]}")


@action('YOUTUBE_PLAY', family='YOUTUBE_PLAY')
def _run_youtube_play(ctx):
    """Resolve one official YouTube result and verify browser playback."""
    import browser
    from providers import youtube_api
    try:
        video = youtube_api.resolve_video(
            query=ctx.attr('query'), video_id=ctx.attr('video_id'),
            url=ctx.attr('url'))
        ctx.step(f"Opening {video.get('title') or 'the YouTube video'}")
        result = browser.youtube_play(video['url'], tab=ctx.attr('tab') or None)
    except youtube_api.YouTubeError as exc:
        result = f"[Error] {exc}"
    ctx.report(result)
    ctx.say(f"> YOUTUBE_PLAY\n\n{result[:1200]}")


@action('YOUTUBE_FULLSCREEN', family='YOUTUBE_FULLSCREEN')
def _run_youtube_fullscreen(ctx):
    """Toggle only YouTube's player fullscreen and verify the browser state."""
    import browser
    enabled = ctx.flag('enabled', True)
    ctx.step("Entering YouTube full screen" if enabled
             else "Leaving YouTube full screen")
    result = browser.youtube_fullscreen(
        enabled=enabled, tab=ctx.attr('tab') or None)
    ctx.report(result)
    ctx.say(f"> YOUTUBE_FULLSCREEN\n\n{result[:1200]}")


@action('BROWSER_GOTO', family='BROWSER_GOTO')
def _run_browser_goto(ctx):
    """Handles BROWSER_GOTO."""
    action_type = ctx.action_type
    attrs = ctx.attrs
    ui_queue = ctx.ui_queue
    base_dir = ctx.base_dir
    origin = ctx.origin
    turn_id = ctx.turn_id
    interrupt_event = ctx.interrupt_event
    element_map = ctx.element_map
    response_text = ctx.response_text
    url = attrs.get('url', '')
    if url:
        import browser
        background = ctx.flag('background', False) or _turn_runs_in_background()
        purpose = ctx.attr('purpose', '').strip()
        msg = ((f"Checking {purpose} in the background" if purpose else
                "Checking that in the background") if background else
               f"Navigating managed browser to: {url}")
        print(f"[Action] {msg}")
        ui_queue.put(("action", msg))
        execute_actions._last_output = browser.goto(
            url, tab=attrs.get('tab') or None, visible=not background)
    else:
        execute_actions._last_output = (
            "[Error] BROWSER_GOTO requires the destination URL. No navigation occurred.")


@action('BROWSER_READ', 'BROWSER_SNAPSHOT', family='BROWSER_READ')
def _run_browser_read(ctx):
    """Handles BROWSER_READ, BROWSER_SNAPSHOT."""
    action_type = ctx.action_type
    attrs = ctx.attrs
    ui_queue = ctx.ui_queue
    base_dir = ctx.base_dir
    origin = ctx.origin
    turn_id = ctx.turn_id
    interrupt_event = ctx.interrupt_event
    element_map = ctx.element_map
    response_text = ctx.response_text
    import browser
    msg = "Reading managed browser snapshot..."
    print(f"[Action] {msg}")
    ui_queue.put(("action", msg))
    snapshot_text = browser.snapshot(
        tab=attrs.get('tab') or None,
        query=attrs.get('query', ''))
    snapshot_text = str(snapshot_text)
    # A syntactically successful snapshot of about:blank is not evidence that
    # a portal was visited.  It used to satisfy the outcome and let the model
    # narrate a blocker without ever issuing BROWSER_GOTO.
    try:
        marker = "[Browser Snapshot]"
        payload = json.loads(snapshot_text.split(marker, 1)[1].strip())
        current_url = str(payload.get("url") or "").strip()
    except Exception:
        current_url = ""
    if not current_url or current_url.lower() in {"about:blank", "about:newtab"}:
        execute_actions._last_output = (
            "[Error] The managed browser is still blank, so no web page was "
            "read. Run BROWSER_GOTO with the requested URL, verify its "
            "navigation receipt, then take a new BROWSER_SNAPSHOT.")
    else:
        execute_actions._last_output = (
            cross_browser_note(snapshot_text) + snapshot_text)


@action('BROWSER_CLICK', family='BROWSER_CLICK')
def _run_browser_click(ctx):
    """Handles BROWSER_CLICK."""
    action_type = ctx.action_type
    attrs = ctx.attrs
    ui_queue = ctx.ui_queue
    base_dir = ctx.base_dir
    origin = ctx.origin
    turn_id = ctx.turn_id
    interrupt_event = ctx.interrupt_event
    element_map = ctx.element_map
    response_text = ctx.response_text
    ref = attrs.get('ref', '')
    if not ref and attrs.get('selector'):
        ref = attrs['selector']
        if not ref.startswith(('css=', 'xpath=')):
            ref = 'css=' + ref
    if ref:
        import browser
        msg = f"Clicking managed browser element: {ref}"
        print(f"[Action] {msg}")
        ui_queue.put(("action", msg))
        execute_actions._last_output = browser.click(ref, tab=attrs.get('tab') or None)


@action('BROWSER_TYPE', family='BROWSER_TYPE')
def _run_browser_type(ctx):
    """Handles BROWSER_TYPE."""
    action_type = ctx.action_type
    attrs = ctx.attrs
    ui_queue = ctx.ui_queue
    base_dir = ctx.base_dir
    origin = ctx.origin
    turn_id = ctx.turn_id
    interrupt_event = ctx.interrupt_event
    element_map = ctx.element_map
    response_text = ctx.response_text
    ref = attrs.get('ref', '')
    if not ref and attrs.get('selector'):
        ref = attrs['selector']
        if not ref.startswith(('css=', 'xpath=')):
            ref = 'css=' + ref
    text_to_type = attrs.get('text', '')
    if ref:
        import browser
        msg = f"Typing into managed browser element: {ref}"
        print(f"[Action] {msg}")
        ui_queue.put(("action", msg))
        execute_actions._last_output = browser.type_text(ref, text_to_type, tab=attrs.get('tab') or None)


@action('BROWSER_SELECT', 'BROWSER_CHECK', 'BROWSER_UPLOAD', 'BROWSER_FILL_FORM', 'BROWSER_SUBMIT', family='BROWSER_SELECT')
def _run_browser_select(ctx):
    """Handles BROWSER_SELECT, BROWSER_CHECK, BROWSER_UPLOAD, BROWSER_FILL_FORM...."""
    action_type = ctx.action_type
    attrs = ctx.attrs
    ui_queue = ctx.ui_queue
    base_dir = ctx.base_dir
    origin = ctx.origin
    turn_id = ctx.turn_id
    interrupt_event = ctx.interrupt_event
    element_map = ctx.element_map
    response_text = ctx.response_text
    import browser
    tab = attrs.get('tab') or None
    # Variant selection, as a table. Each closure is the
    # body that used to be a rung of an inner ladder.
    chunk = locals().get('chunk')
    fields = locals().get('fields')
    key = locals().get('key')
    kind = locals().get('kind')
    value = locals().get('value')
    def _v_0_browser_select():
        nonlocal chunk, fields, key, kind, value
        execute_actions._last_output = browser.select(
            attrs.get('ref', ''), attrs.get('value', ''), tab=tab)
    def _v_1_browser_check():
        nonlocal chunk, fields, key, kind, value
        execute_actions._last_output = browser.check(
            attrs.get('ref', ''),
            attrs.get('checked', 'true').strip().lower() not in {'false', '0', 'no'},
            tab=tab)
    def _v_2_browser_upload():
        nonlocal chunk, fields, key, kind, value
        execute_actions._last_output = browser.upload(
            attrs.get('ref', ''), attrs.get('path', ''), tab=tab)
    def _v_3_browser_submit():
        nonlocal chunk, fields, key, kind, value
        execute_actions._last_output = browser.submit(
            attrs.get('ref', '') or None, tab=tab)
    def _v_default():
        nonlocal chunk, fields, key, kind, value
        fields = {}
        for chunk in str(attrs.get('fields', '')).split('|'):
            chunk = chunk.strip()
            if not chunk or '=' not in chunk:
                continue
            key, value = chunk.split('=', 1)
            kind = 'fill'
            if ':' in key:
                key, kind = key.rsplit(':', 1)
            fields[key.strip()] = {"kind": kind.strip().lower(), "value": value.strip()}
        execute_actions._last_output = (
            browser.fill_form(fields, tab=tab) if fields else
            '[Error] BROWSER_FILL_FORM needs fields="e1=value | e2:select=Option".')
    _variants = {
        'BROWSER_SELECT': _v_0_browser_select,
        'BROWSER_CHECK': _v_1_browser_check,
        'BROWSER_UPLOAD': _v_2_browser_upload,
        'BROWSER_SUBMIT': _v_3_browser_submit,
    }
    _variants.get(action_type, _v_default)()
    ui_queue.put(("info", f"> {action_type}\n\n{execute_actions._last_output[:800]}"))


@action('BROWSER_PRESS', family='BROWSER_PRESS')
def _run_browser_press(ctx):
    """Handles BROWSER_PRESS."""
    action_type = ctx.action_type
    attrs = ctx.attrs
    ui_queue = ctx.ui_queue
    base_dir = ctx.base_dir
    origin = ctx.origin
    turn_id = ctx.turn_id
    interrupt_event = ctx.interrupt_event
    element_map = ctx.element_map
    response_text = ctx.response_text
    import browser
    execute_actions._last_output = browser.press(
        attrs.get('ref', ''), attrs.get('key', 'Enter'), tab=attrs.get('tab') or None
    )


@action('BROWSER_DIALOG', family='BROWSER_DIALOG')
def _run_browser_dialog(ctx):
    """Inspect, accept or dismiss a native JavaScript browser dialog."""
    attrs = ctx.attrs
    import browser
    execute_actions._last_output = browser.dialog(
        mode=attrs.get('mode', 'status'),
        text=attrs.get('text', ''),
        tab=attrs.get('tab') or None,
    )
    ctx.ui_queue.put((
        "info", f"> BROWSER_DIALOG\n\n{execute_actions._last_output[:800]}"))


@action('BROWSER_WAIT', family='BROWSER_WAIT')
def _run_browser_wait(ctx):
    """Handles BROWSER_WAIT."""
    action_type = ctx.action_type
    attrs = ctx.attrs
    ui_queue = ctx.ui_queue
    base_dir = ctx.base_dir
    origin = ctx.origin
    turn_id = ctx.turn_id
    interrupt_event = ctx.interrupt_event
    element_map = ctx.element_map
    response_text = ctx.response_text
    import browser
    execute_actions._last_output = browser.wait(
        text=attrs.get('text') or None,
        ms=int(attrs.get('ms', '1000')),
        tab=attrs.get('tab') or None,
    )


@action('BROWSER_CLOSE', family='BROWSER_CLOSE')
def _run_browser_close(ctx):
    """Handles BROWSER_CLOSE."""
    action_type = ctx.action_type
    attrs = ctx.attrs
    ui_queue = ctx.ui_queue
    base_dir = ctx.base_dir
    origin = ctx.origin
    turn_id = ctx.turn_id
    interrupt_event = ctx.interrupt_event
    element_map = ctx.element_map
    response_text = ctx.response_text
    import browser
    msg = "Closing managed browser session..."
    print(f"[Action] {msg}")
    ui_queue.put(("action", msg))
    execute_actions._last_output = browser.close(
        tab=attrs.get('tab') or None,
        all_tabs=attrs.get('all', 'false').lower() == 'true',
    )


@action('DELETE_FILE', family='DELETE_FILE')
def _run_delete_file(ctx):
    """Handles DELETE_FILE."""
    action_type = ctx.action_type
    attrs = ctx.attrs
    ui_queue = ctx.ui_queue
    base_dir = ctx.base_dir
    origin = ctx.origin
    turn_id = ctx.turn_id
    interrupt_event = ctx.interrupt_event
    element_map = ctx.element_map
    response_text = ctx.response_text
    path = attrs.get('path', '')
    allowed, err = _requires_confirmation(attrs, f"DELETE_FILE '{path}'", origin)
    if not allowed:
        execute_actions._last_output = err
    elif path:
        try:
            if os.path.isdir(path):
                import shutil
                shutil.rmtree(path)
            else:
                os.remove(path)
            execute_actions._last_output = f"[Success] Deleted: {path}"
            print(execute_actions._last_output)
        except Exception as e:
            execute_actions._last_output = f"[Error] DELETE_FILE failed: {e}"


@action('MOVE_FILE', family='MOVE_FILE')
def _run_move_file(ctx):
    """Handles MOVE_FILE."""
    action_type = ctx.action_type
    attrs = ctx.attrs
    ui_queue = ctx.ui_queue
    base_dir = ctx.base_dir
    origin = ctx.origin
    turn_id = ctx.turn_id
    interrupt_event = ctx.interrupt_event
    element_map = ctx.element_map
    response_text = ctx.response_text
    src = str(attrs.get('src', '') or '').strip()
    dst = str(attrs.get('dst', '') or '').strip()
    try:
        import artifact_context
        if not src or src.casefold() in {"it", "that", "this", "latest", "latest file"}:
            recent = artifact_context.latest()
            src = recent["path"] if recent else ""
        if dst.casefold() in {"desktop", "the desktop", "my desktop"}:
            dst = artifact_context.desktop_directory()
        if not src:
            execute_actions._last_output = (
                "[Error] MOVE_FILE has no verified source. Create or locate the file first.")
        elif not dst:
            execute_actions._last_output = "[Error] MOVE_FILE needs a destination."
        elif not os.path.exists(src):
            execute_actions._last_output = f"[Error] MOVE_FILE source does not exist: {src}"
        else:
            import shutil
            final_dst = os.path.join(dst, os.path.basename(src)) if os.path.isdir(dst) else dst
            final_dst = os.path.abspath(final_dst)
            if os.path.exists(final_dst):
                execute_actions._last_output = (
                    f"[Error] MOVE_FILE refused to overwrite the existing destination: {final_dst}")
            else:
                os.makedirs(os.path.dirname(final_dst), exist_ok=True)
                moved_to = shutil.move(src, final_dst)
                artifact_context.moved(src, moved_to)
                if os.path.exists(moved_to) and not os.path.exists(src):
                    execute_actions._last_output = (
                        f"[Success] Moved '{src}' to '{moved_to}'. "
                        "Verified: destination exists and source is absent.")
                else:
                    execute_actions._last_output = (
                        "[Error] MOVE_FILE returned without satisfying filesystem verification.")
                print(execute_actions._last_output)
    except Exception as e:
        execute_actions._last_output = f"[Error] MOVE_FILE failed: {e}"


@action('COPY_FILE', family='COPY_FILE')
def _run_copy_file(ctx):
    """Handles COPY_FILE."""
    action_type = ctx.action_type
    attrs = ctx.attrs
    ui_queue = ctx.ui_queue
    base_dir = ctx.base_dir
    origin = ctx.origin
    turn_id = ctx.turn_id
    interrupt_event = ctx.interrupt_event
    element_map = ctx.element_map
    response_text = ctx.response_text
    # Not destructive — no confirmation gate needed.
    src = attrs.get('src', '')
    dst = attrs.get('dst', '')
    if src and dst:
        try:
            import shutil
            if os.path.isdir(src):
                shutil.copytree(src, dst, dirs_exist_ok=True)
            else:
                shutil.copy2(src, dst)
            execute_actions._last_output = f"[Success] Copied '{src}' to '{dst}'"
            print(execute_actions._last_output)
        except Exception as e:
            execute_actions._last_output = f"[Error] COPY_FILE failed: {e}"


@action('WAIT_FOR', family='WAIT_FOR')
def _run_wait_for(ctx):
    """Handles WAIT_FOR."""
    action_type = ctx.action_type
    attrs = ctx.attrs
    ui_queue = ctx.ui_queue
    base_dir = ctx.base_dir
    origin = ctx.origin
    turn_id = ctx.turn_id
    interrupt_event = ctx.interrupt_event
    element_map = ctx.element_map
    response_text = ctx.response_text
    path = attrs.get('path', '')
    timeout = float(attrs.get('timeout', 600))
    if path:
        msg = f"Waiting for: {path} (timeout {timeout}s)"
        print(f"[Action] {msg}")
        ui_queue.put(("action", msg))
        start = time.time()
        found = False
        while time.time() - start < timeout:
            if os.path.exists(path):
                found = True
                break
            time.sleep(2.0)
        elapsed = time.time() - start
        if found:
            execute_actions._last_output = f"[WAIT_FOR] '{path}' appeared after {elapsed:.0f}s."
        else:
            execute_actions._last_output = f"[WAIT_FOR] Timed out after {timeout:.0f}s waiting for '{path}'. It never appeared."
        print(execute_actions._last_output)


@action('NOTIFY', family='NOTIFY')
def _run_notify(ctx):
    """Handles NOTIFY."""
    action_type = ctx.action_type
    attrs = ctx.attrs
    ui_queue = ctx.ui_queue
    base_dir = ctx.base_dir
    origin = ctx.origin
    turn_id = ctx.turn_id
    interrupt_event = ctx.interrupt_event
    element_map = ctx.element_map
    response_text = ctx.response_text
    text = attrs.get('text', '')
    silent = attrs.get('silent', 'true').strip().lower() == 'true'
    if text:
        print(f"[Notify] {text}")
        ui_queue.put(("info", f"[Note] {text}"))
        # A note on VANTA's own dashboard only reaches Himanshu if
        # he happens to be looking at it. Raise a real Windows toast
        # so it lands in Action Centre and survives being missed.
        try:
            import notifications
            toast = notifications.notify(
                attrs.get('title', '') or 'VANTA', text,
                urgent=attrs.get('urgent', '').strip().lower() == 'true',
                silent=silent)
            execute_actions._last_output = toast
        except Exception as e:
            execute_actions._last_output = f"[Success] Noted (toast unavailable: {e})"
        try:
            import json as _json
            notif_file = os.path.join(base_dir, "vault", "system", "notifications.json")
            os.makedirs(os.path.dirname(notif_file), exist_ok=True)
            items = []
            if os.path.exists(notif_file):
                with open(notif_file, 'r', encoding='utf-8') as f:
                    try: items = _json.load(f)
                    except Exception: items = []
            import datetime as _dt
            items.append({"text": text, "time": _dt.datetime.now().isoformat(), "silent": silent})
            with open(notif_file, 'w', encoding='utf-8') as f:
                _json.dump(items[-200:], f, indent=2)
        except Exception as e:
            print(f"[Notify] Could not persist: {e}")
        # Keep the toast's own receipt: it reports whether Windows
        # actually accepted the notification, which a generic
        # "[Notified]" would hide.
        if not str(getattr(execute_actions, '_last_output', '')).startswith(
                ('[Success] Notified', '[Error]')):
            execute_actions._last_output = f"[Notified: silent={silent}]"


@action('WATCH_CREATE', 'WATCH_LIST', 'WATCH_STATUS', 'WATCH_PAUSE', 'WATCH_RESUME', 'WATCH_CANCEL', 'WATCH_CHECK', 'WATCH_HISTORY', family='WATCH_CREATE')
def _run_watch_create(ctx):
    """Handles WATCH_CREATE, WATCH_LIST, WATCH_STATUS, WATCH_PAUSE...."""
    action_type = ctx.action_type
    attrs = ctx.attrs
    ui_queue = ctx.ui_queue
    base_dir = ctx.base_dir
    origin = ctx.origin
    turn_id = ctx.turn_id
    interrupt_event = ctx.interrupt_event
    element_map = ctx.element_map
    response_text = ctx.response_text
    import watches
    identifier = attrs.get('id', '') or attrs.get('label', '')
    try:
        # Variant selection, as a table. Each closure is the
        # body that used to be a rung of an inner ladder.
        checked = locals().get('checked')
        desktop = locals().get('desktop')
        match = locals().get('match')
        observed = locals().get('observed')
        res = locals().get('res')
        source = locals().get('source')
        value = locals().get('value')
        def _v_0_watch_create():
            nonlocal checked, desktop, match, observed, res, source, value
            source = (attrs.get('source', '') or attrs.get('url', '')).strip()
            if source.lower() in {'active', 'this', 'current'}:
                import desktop
                source = desktop.active_browser_url()
                if source.startswith('[Error]'):
                    res = source
                else:
                    res = watches.create(
                        attrs.get('kind', 'price'), source,
                        attrs.get('condition', 'below'), attrs.get('target', ''),
                        attrs.get('label', ''), attrs.get('interval', '30m'),
                        attrs.get('currency', 'INR'))
            else:
                res = watches.create(
                    attrs.get('kind', 'price'), source,
                    attrs.get('condition', 'below'), attrs.get('target', ''),
                    attrs.get('label', ''), attrs.get('interval', '30m'),
                    attrs.get('currency', 'INR'))
            # Establish an initial evidence point now. A blocked
            # store page does not undo the durable watch; it records
            # the error and backs off for the next attempt.
            match = re.search(r'Watch (watch-[a-f0-9]+)', res)
            if res.startswith('[Success]') and match:
                checked = watches.check_one(match.group(1))
                if checked.get('ok'):
                    value = checked.get('observation', {}).get('value')
                    res += f" Initial observation: {value}."
                    if checked.get('crossed'):
                        res += f" {checked.get('alert')}"
                else:
                    res += (" The first check could not be verified yet: "
                            f"{checked.get('message')}. It remains active with backoff.")
        def _v_1_watch_list():
            nonlocal checked, desktop, match, observed, res, source, value
            res = '[Success] ' + watches.listing()
        def _v_2_watch_status():
            nonlocal checked, desktop, match, observed, res, source, value
            res = watches.status(identifier)
            if not res.startswith('[Error]'):
                res = '[Success] ' + res
        def _v_3_watch_pause():
            nonlocal checked, desktop, match, observed, res, source, value
            res = watches.set_status(identifier, 'paused')
        def _v_4_watch_resume():
            nonlocal checked, desktop, match, observed, res, source, value
            res = watches.set_status(identifier, 'active')
        def _v_5_watch_cancel():
            nonlocal checked, desktop, match, observed, res, source, value
            res = watches.set_status(identifier, 'cancelled')
        def _v_6_watch_history():
            nonlocal checked, desktop, match, observed, res, source, value
            res = watches.history(identifier, attrs.get('limit', '20'))
            if not res.startswith('[Error]'):
                res = '[Success] ' + res
        def _v_default():
            nonlocal checked, desktop, match, observed, res, source, value
            checked = watches.check_one(identifier)
            if checked.get('ok'):
                observed = checked.get('observation', {})
                res = (f"[Success] Checked {identifier}: {observed.get('value')} "
                       f"via {observed.get('method')}; condition "
                       f"{'met' if checked.get('condition_met') else 'not met'}.")
                if checked.get('crossed'):
                    res += ' ' + checked.get('alert', '')
            else:
                res = f"[Error] Watch check failed: {checked.get('message')}"
        _variants = {
            'WATCH_CREATE': _v_0_watch_create,
            'WATCH_LIST': _v_1_watch_list,
            'WATCH_STATUS': _v_2_watch_status,
            'WATCH_PAUSE': _v_3_watch_pause,
            'WATCH_RESUME': _v_4_watch_resume,
            'WATCH_CANCEL': _v_5_watch_cancel,
            'WATCH_HISTORY': _v_6_watch_history,
        }
        _variants.get(action_type, _v_default)()
    except (ValueError, TypeError) as exc:
        res = f"[Error] Invalid watch: {exc}"
    except Exception as exc:
        res = f"[Error] Watch operation failed: {exc}"
    execute_actions._last_output = res
    print(f"[Agent] {action_type}: {res.splitlines()[0][:180]}")
    ui_queue.put(("info", f"> {action_type}\n\n{res}"))


@action('ADD_LIFE_GOAL', family='ADD_LIFE_GOAL')
def _run_add_life_goal(ctx):
    """Handles ADD_LIFE_GOAL."""
    action_type = ctx.action_type
    attrs = ctx.attrs
    ui_queue = ctx.ui_queue
    base_dir = ctx.base_dir
    origin = ctx.origin
    turn_id = ctx.turn_id
    interrupt_event = ctx.interrupt_event
    element_map = ctx.element_map
    response_text = ctx.response_text
    title = attrs.get('title', '')
    if title:
        import life_model
        res = life_model.add_goal(base_dir, title, attrs.get('horizon', 'ongoing'), attrs.get('priority', 'medium'))
        execute_actions._last_output = res
        ui_queue.put(("info", f"> LIFE GOAL\n\n{res}"))


@action('SET_LIFE_PREFERENCE', family='SET_LIFE_PREFERENCE')
def _run_set_life_preference(ctx):
    """Handles SET_LIFE_PREFERENCE."""
    action_type = ctx.action_type
    attrs = ctx.attrs
    ui_queue = ctx.ui_queue
    base_dir = ctx.base_dir
    origin = ctx.origin
    turn_id = ctx.turn_id
    interrupt_event = ctx.interrupt_event
    element_map = ctx.element_map
    response_text = ctx.response_text
    key, value = attrs.get('key', ''), attrs.get('value', '')
    if key and value:
        import life_model
        res = life_model.set_preference(base_dir, key, value)
        execute_actions._last_output = res
        ui_queue.put(("info", f"> LIFE PREFERENCE\n\n{res}"))


@action('SHOW_ROUTE', family='SHOW_ROUTE')
def _run_show_route(ctx):
    """Handles SHOW_ROUTE."""
    action_type = ctx.action_type
    attrs = ctx.attrs
    ui_queue = ctx.ui_queue
    base_dir = ctx.base_dir
    origin = ctx.origin
    turn_id = ctx.turn_id
    interrupt_event = ctx.interrupt_event
    element_map = ctx.element_map
    response_text = ctx.response_text
    # The route geometry was already being fetched and thrown away:
    # maps_api returns a full GeoJSON LineString and only the
    # "36.5 km, 50 minutes" summary ever reached him. This draws it
    # on screen, where the shape of the journey is the useful part.
    from providers import maps_api
    import hud_state
    origin = attrs.get('origin', '') or attrs.get('from', '')
    destination = attrs.get('destination', '') or attrs.get('to', '')
    if not origin or not destination:
        res = ('[Error] SHOW_ROUTE needs origin="..." and '
               'destination="...".')
    else:
        found = maps_api.route(origin, destination,
                               attrs.get('mode', 'driving'),
                               traffic=str(attrs.get('traffic', 'true')
                                           ).lower() != 'false')
        if not found.get('ok'):
            res = f"[Error] {found.get('error', 'Route unavailable')}"
        else:
            import route_render
            label = (attrs.get('label', '')
                     or f"{origin} to {destination}")
            hud_state.show(base_dir, label, [], card_id='route',
                           kind='route', payload=found)
            ui_queue.put(("hud_refresh", ""))
            ui_queue.put(("route_panel", found))
            shown = route_render.all_routes(found)
            caption = route_render.describe_all(found).replace("\n", "; ")
            points = len(route_render.coordinates_of(found))
            res = (f"[Success] {len(shown)} route{'s' if len(shown) != 1 else ''} "
                   f"drawn on his screen: {label} -- {caption} "
                   f"({points} points in the recommended one). They are also "
                   f"on the Map page of the Control Center.")
    execute_actions._last_output = res
    print(f"[Action] SHOW_ROUTE: {res.splitlines()[0][:130]}")
    ui_queue.put(("info", f"> SHOW_ROUTE\n\n{res}"))


@action('GET_TRAVEL_TIME', family='GET_TRAVEL_TIME')
def _run_get_travel_time(ctx):
    """Handles GET_TRAVEL_TIME."""
    action_type = ctx.action_type
    attrs = ctx.attrs
    ui_queue = ctx.ui_queue
    base_dir = ctx.base_dir
    origin = ctx.origin
    turn_id = ctx.turn_id
    interrupt_event = ctx.interrupt_event
    element_map = ctx.element_map
    response_text = ctx.response_text
    origin = attrs.get('origin', '')
    destination = attrs.get('destination', '')
    mode = attrs.get('mode', 'driving').lower()
    if origin and destination:
        msg = f"Getting {mode} time: {origin} -> {destination}"
        print(f"[Action] {msg}")
        ui_queue.put(("action", msg))
        try:
            from providers import maps_api
            wants_traffic = str(attrs.get('traffic', 'true')).strip().lower() not in {'0', 'false', 'no'}
            res = maps_api.get_travel_time(origin, destination, mode, traffic=wants_traffic)
            execute_actions._last_output = res
            ui_queue.put(("info", f"> GET_TRAVEL_TIME\n\n{res}"))
        except Exception as e:
            execute_actions._last_output = f"[Error] GET_TRAVEL_TIME failed: {e}"
            print(execute_actions._last_output)
    else:
        execute_actions._last_output = "[Error] GET_TRAVEL_TIME requires 'origin' and 'destination' attributes."


@action('FLIGHT_SEARCH', family='FLIGHT_SEARCH')
def _run_flight_search(ctx):
    """Handles FLIGHT_SEARCH."""
    action_type = ctx.action_type
    attrs = ctx.attrs
    ui_queue = ctx.ui_queue
    base_dir = ctx.base_dir
    origin = ctx.origin
    turn_id = ctx.turn_id
    interrupt_event = ctx.interrupt_event
    element_map = ctx.element_map
    response_text = ctx.response_text
    try:
        from providers import flights_api
        result = flights_api.search(
            attrs.get('origin', ''), attrs.get('destination', ''),
            attrs.get('departure_date', ''),
            return_date=attrs.get('return_date', ''),
            adults=int(attrs.get('adults', 1) or 1),
            travel_class=int(attrs.get('travel_class', 1) or 1),
            nonstop=str(attrs.get('nonstop', '')).lower() in {'1', 'true', 'yes'},
            max_price=(int(attrs['max_price']) if attrs.get('max_price') else None),
            limit=int(attrs.get('limit', 8) or 8))
        res = '[Success] ' + flights_api.describe(result)
    except (ValueError, TypeError) as exc:
        res = f"[Error] Invalid flight search: {exc}"
    except Exception as exc:
        res = f"[Error] Flight search unavailable: {exc}"
    execute_actions._last_output = res
    print(f"[Agent] FLIGHT_SEARCH: {res.splitlines()[0][:180]}")
    ui_queue.put(("info", f"> FLIGHT SEARCH\n\n{res}"))


@action('PRICE_SEARCH', family='PRICE_SEARCH')
def _run_price_search(ctx):
    """Search structured marketplace prices without screen scraping."""
    attrs = ctx.attrs
    try:
        from providers import pricing_api
        result = pricing_api.search(
            attrs.get('query', ''),
            location=attrs.get('location', 'India'),
            country=attrs.get('country', 'in'),
            language=attrs.get('language', 'en'),
            product_id=attrs.get('product_id', ''),
            limit=int(attrs.get('limit', 10) or 10),
        )
        res = '[Success] ' + pricing_api.describe(result)
    except (ValueError, TypeError) as exc:
        res = f"[Error] Invalid product price search: {exc}"
    except Exception as exc:
        res = f"[Error] Product pricing unavailable: {exc}"
    execute_actions._last_output = res
    print(f"[Agent] PRICE_SEARCH: {res.splitlines()[0][:180]}")
    ctx.ui_queue.put(("info", f"> PRODUCT PRICES\n\n{res}"))


@action('FLIGHT_WATCH_CREATE', family='FLIGHT_WATCH_CREATE')
def _run_flight_watch_create(ctx):
    """Handles FLIGHT_WATCH_CREATE."""
    action_type = ctx.action_type
    attrs = ctx.attrs
    ui_queue = ctx.ui_queue
    base_dir = ctx.base_dir
    origin = ctx.origin
    turn_id = ctx.turn_id
    interrupt_event = ctx.interrupt_event
    element_map = ctx.element_map
    response_text = ctx.response_text
    try:
        import watches
        res = watches.create_flight(
            attrs.get('origin', ''), attrs.get('destination', ''),
            attrs.get('departure_date', ''), attrs.get('target', ''),
            return_date=attrs.get('return_date', ''),
            adults=attrs.get('adults', '1'),
            travel_class=attrs.get('travel_class', '1'),
            nonstop=str(attrs.get('nonstop', '')).lower() in {'1', 'true', 'yes'},
            label=attrs.get('label', ''), interval=attrs.get('interval', '6h'),
            currency=attrs.get('currency', 'INR'))
        match = re.search(r'Watch (watch-[a-f0-9]+)', res)
        if res.startswith('[Success]') and match:
            checked = watches.check_one(match.group(1))
            if checked.get('ok'):
                res += f" Current lowest fare: INR {float(checked['observation']['value']):,.0f}."
            else:
                res += f" Initial check deferred: {checked.get('message')}."
    except (ValueError, TypeError) as exc:
        res = f"[Error] Invalid flight watch: {exc}"
    except Exception as exc:
        res = f"[Error] Flight watch unavailable: {exc}"
    execute_actions._last_output = res
    print(f"[Agent] FLIGHT_WATCH_CREATE: {res.splitlines()[0][:180]}")
    ui_queue.put(("info", f"> FLIGHT PRICE WATCH\n\n{res}"))


@action('PRICE_WATCH_CREATE', family='PRICE_WATCH_CREATE')
def _run_price_watch_create(ctx):
    """Create and verify a structured, durable product-price watch."""
    attrs = ctx.attrs
    try:
        import watches
        res = watches.create_product_price(
            attrs.get('query', ''), attrs.get('target', ''),
            product_id=attrs.get('product_id', ''),
            location=attrs.get('location', 'India'),
            label=attrs.get('label', ''),
            interval=attrs.get('interval', '12h'),
            currency=attrs.get('currency', 'INR'))
        match = re.search(r'Watch (watch-[a-f0-9]+)', res)
        if res.startswith('[Success]') and match:
            checked = watches.check_one(match.group(1))
            if checked.get('ok'):
                observed = checked.get('observation') or {}
                res += (f" Current observed price: INR "
                        f"{float(observed['value']):,.0f} from "
                        f"{observed.get('merchant') or 'the pricing API'}.")
            else:
                res += f" Initial check deferred: {checked.get('message')}."
    except (ValueError, TypeError) as exc:
        res = f"[Error] Invalid product price watch: {exc}"
    except Exception as exc:
        res = f"[Error] Product price watch unavailable: {exc}"
    execute_actions._last_output = res
    print(f"[Agent] PRICE_WATCH_CREATE: {res.splitlines()[0][:180]}")
    ctx.ui_queue.put(("info", f"> PRODUCT PRICE WATCH\n\n{res}"))


@action('FIND_API', family='FIND_API')
def _run_find_api(ctx):
    """Handles FIND_API."""
    action_type = ctx.action_type
    attrs = ctx.attrs
    ui_queue = ctx.ui_queue
    base_dir = ctx.base_dir
    origin = ctx.origin
    turn_id = ctx.turn_id
    interrupt_event = ctx.interrupt_event
    element_map = ctx.element_map
    response_text = ctx.response_text
    try:
        import api_index
        need = attrs.get('need', '')
        wants_refresh = str(attrs.get('refresh', '')).strip().lower() in {'1', 'true', 'yes'}
        if wants_refresh or not api_index.load(base_dir):
            note = api_index.refresh(base_dir)
            print(f"[Agent] FIND_API: {note}")
        try:
            limit = max(1, min(int(attrs.get('limit', 5) or 5), 10))
        except (TypeError, ValueError):
            limit = 5
        res = api_index.describe(
            base_dir, need, limit=limit,
            auth=attrs.get('auth', 'any'),
            category=attrs.get('category', ''),
            region=attrs.get('region', ''),
            official_only=str(attrs.get('official_only', '')).strip().lower()
            in {'1', 'true', 'yes'})
        execute_actions._last_output = res
        print(f"[Agent] FIND_API:\n{res}")
        ui_queue.put(("info", f"> FIND_API {need}\n\n{res}"))
    except Exception as e:
        execute_actions._last_output = f"[Error] FIND_API failed: {e}"
        print(f"[Error] FIND_API failed: {e}")


@action('API_CATALOG_STATUS', family='API_CATALOG_STATUS')
def _run_api_catalog_status(ctx):
    """Handles API_CATALOG_STATUS."""
    action_type = ctx.action_type
    attrs = ctx.attrs
    ui_queue = ctx.ui_queue
    base_dir = ctx.base_dir
    origin = ctx.origin
    turn_id = ctx.turn_id
    interrupt_event = ctx.interrupt_event
    element_map = ctx.element_map
    response_text = ctx.response_text
    try:
        import api_index
        if (str(attrs.get('refresh', '')).strip().lower() in
                {'1', 'true', 'yes'}):
            api_index.refresh(base_dir)
        res = api_index.status(base_dir)
        execute_actions._last_output = res
        ui_queue.put(("info", f"> API CATALOG\n\n{res}"))
    except Exception as exc:
        execute_actions._last_output = f"[Error] API catalog status failed: {exc}"


@action('API_CHECK', family='API_CHECK')
def _run_api_check(ctx):
    """Handles API_CHECK."""
    action_type = ctx.action_type
    attrs = ctx.attrs
    ui_queue = ctx.ui_queue
    base_dir = ctx.base_dir
    origin = ctx.origin
    turn_id = ctx.turn_id
    interrupt_event = ctx.interrupt_event
    element_map = ctx.element_map
    response_text = ctx.response_text
    try:
        import api_index
        res = api_index.check_candidate(
            base_dir, attrs.get('name', ''), attrs.get('timeout', 10))
        execute_actions._last_output = res
        ui_queue.put(("info", f"> API CHECK\n\n{res}"))
    except Exception as exc:
        execute_actions._last_output = f"[Error] API check failed: {exc}"


@action('BOOK_RIDE', family='BOOK_RIDE')
def _run_book_ride(ctx):
    """Handles BOOK_RIDE."""
    action_type = ctx.action_type
    attrs = ctx.attrs
    ui_queue = ctx.ui_queue
    base_dir = ctx.base_dir
    origin = ctx.origin
    turn_id = ctx.turn_id
    interrupt_event = ctx.interrupt_event
    element_map = ctx.element_map
    response_text = ctx.response_text
    try:
        from providers.rides_api import prepare_ride
        url, described = prepare_ride(
            attrs.get('destination', ''), attrs.get('pickup', ''))
        if url is None:
            execute_actions._last_output = described
            print(f"[Agent] BOOK_RIDE: {described}")
        else:
            # Opening the app IS the handover. VANTA fills the
            # form; the person who gets charged taps Request. No
            # approval gate is added on top because the Uber screen
            # is a better one -- it shows the price and the pickup
            # pin, which no gate here could.
            import webbrowser
            webbrowser.open(url)
            execute_actions._last_output = f"[Success] {described}"
            print(f"[Agent] BOOK_RIDE: {described}")
            ui_queue.put(("info", f"> BOOK_RIDE\n\n{described}\n{url}"))
    except Exception as e:
        execute_actions._last_output = f"[Error] BOOK_RIDE failed: {e}"
        print(f"[Error] BOOK_RIDE failed: {e}")


@action('GEOCODE', family='GEOCODE')
def _run_geocode(ctx):
    """Handles GEOCODE."""
    action_type = ctx.action_type
    attrs = ctx.attrs
    ui_queue = ctx.ui_queue
    base_dir = ctx.base_dir
    origin = ctx.origin
    turn_id = ctx.turn_id
    interrupt_event = ctx.interrupt_event
    element_map = ctx.element_map
    response_text = ctx.response_text
    query = attrs.get('query', '')
    if query:
        msg = f"Geocoding: {query}"
        print(f"[Action] {msg}")
        ui_queue.put(("action", msg))
        try:
            import requests as _req
            r = _req.get(
                "https://nominatim.openstreetmap.org/search",
                params={"q": query, "format": "json", "limit": 1},
                headers={"User-Agent": "VANTA-agent/1.0"},
                timeout=10
            )
            results = r.json()
            if results:
                top = results[0]
                output = (f"[Geocode: {query}]\n"
                          f"Resolved: {top.get('display_name')}\n"
                          f"Lat: {top.get('lat')}, Lon: {top.get('lon')}")
            else:
                output = f"[Geocode: {query}] No results found."
            execute_actions._last_output = output
            print(output)
            ui_queue.put(("info", f"> GEOCODE\n\n{output}"))
        except Exception as e:
            execute_actions._last_output = f"[Error] Geocode failed: {e}"
            print(execute_actions._last_output)


@action('RUN_BLENDER_SCRIPT', family='RUN_BLENDER_SCRIPT')
def _run_run_blender_script(ctx):
    """Handles RUN_BLENDER_SCRIPT."""
    action_type = ctx.action_type
    attrs = ctx.attrs
    ui_queue = ctx.ui_queue
    base_dir = ctx.base_dir
    origin = ctx.origin
    turn_id = ctx.turn_id
    interrupt_event = ctx.interrupt_event
    element_map = ctx.element_map
    response_text = ctx.response_text
    code = attrs.get('code', '').strip()
    if not code:
        block_match = re.search(r'<ACTION\s+type="RUN_BLENDER_SCRIPT"[^>]*>([\s\S]*?)</ACTION>', response_text, re.IGNORECASE)
        if block_match:
            code = block_match.group(1).strip()
    if code:
        msg = "Running Blender script..."
        print(f"[Action] {msg}")
        ui_queue.put(("action", msg))
        try:
            import tempfile
            import codetools
            blender_exe, blender_error = codetools.find_blender()
            if blender_error:
                execute_actions._last_output = blender_error
            else:
                with tempfile.NamedTemporaryFile(mode='w', suffix='.py', delete=False, encoding='utf-8') as tmp:
                    tmp.write(code)
                    tmp_path = tmp.name
                result = subprocess.run(
                    [blender_exe, "--background", "--python", tmp_path],
                    capture_output=True, text=True, timeout=180
                )
                out = (result.stdout or "").strip()
                err = (result.stderr or "").strip()
                combined = f"{out}\n{err}".strip() if err else out
                if len(combined) > 3000:
                    combined = combined[-3000:]
                if result.returncode:
                    execute_actions._last_output = (
                        f"[Error] Blender exited with code {result.returncode}.\n"
                        f"{combined or 'No diagnostic output was produced.'}")
                else:
                    execute_actions._last_output = (
                        "[Success] Blender completed the one-shot headless "
                        "script and exited with code 0. No visible Blender "
                        "session remains.\n" +
                        (combined or "No script output was produced."))
                print(f"[Blender Output]\n{combined}")
                ui_queue.put(("info", f"> BLENDER\n\n{combined[:1000]}"))
        except subprocess.TimeoutExpired:
            execute_actions._last_output = "[Error] Blender script timed out after 180s."
        except Exception as e:
            execute_actions._last_output = f"[Error] Blender run failed: {e}"
            print(execute_actions._last_output)
        finally:
            try:
                if 'tmp_path' in locals() and os.path.exists(tmp_path):
                    os.unlink(tmp_path)
            except OSError:
                pass


@action('RUNTIME_APPLY', 'RUNTIME_STATUS', 'RUNTIME_RESUME', 'RUNTIME_PRESENT',
        family='RUNTIME_APPLY', requires=(action_bus.user_only(),))
def _run_project_runtime(ctx):
    """Apply code to, inspect, or open a persistent executable project."""
    import project_runtime

    adapter = ctx.attr('adapter', 'blender') or 'blender'
    project = ctx.attr('project') or ctx.attr('name')
    try:
        timeout = max(3, min(int(float(ctx.attr('timeout', 120))), 600))
    except (TypeError, ValueError):
        timeout = 120

    try:
        if ctx.action_type == 'RUNTIME_APPLY':
            code = ctx.attr('code') or _tag_body(
                ctx.response_text, 'RUNTIME_APPLY')
            ctx.step(f"Updating {project} in {adapter}...")
            receipt = project_runtime.apply(
                ctx.base_dir, adapter, project, code,
                instruction=ctx.attr('instruction'), timeout=timeout,
                open_after=ctx.flag('open', True),
                interrupt_event=ctx.interrupt_event)
        elif ctx.action_type == 'RUNTIME_STATUS':
            ctx.step(f"Inspecting {project}...")
            receipt = project_runtime.status(
                ctx.base_dir, adapter, project, timeout=timeout)
        elif ctx.action_type == 'RUNTIME_PRESENT':
            enabled = str(ctx.attr('action', 'enter')).strip().casefold() not in {
                'exit', 'leave', 'close', 'off', 'disable', 'false', '0'}
            ctx.step(f"{'Presenting' if enabled else 'Restoring'} {project}...")
            receipt = project_runtime.present(
                ctx.base_dir, adapter, project, enabled=enabled,
                timeout=timeout)
        else:
            ctx.step(f"Opening {project} in {adapter}...")
            receipt = project_runtime.open_project(
                ctx.base_dir, adapter, project, timeout=timeout)
    except Exception as exc:
        receipt = f"[Error] {ctx.action_type} failed: {exc}"

    ctx.report(receipt)
    ctx.say(f"> {ctx.action_type}\n\n{receipt}")


@action('RUN_PYTHON', family='RUN_PYTHON')
def _run_run_python(ctx):  # records its own receipt
    """Handles RUN_PYTHON."""
    action_type = ctx.action_type
    attrs = ctx.attrs
    ui_queue = ctx.ui_queue
    base_dir = ctx.base_dir
    origin = ctx.origin
    turn_id = ctx.turn_id
    interrupt_event = ctx.interrupt_event
    element_map = ctx.element_map
    response_text = ctx.response_text
    # Execute arbitrary Python code and return the output
    code = attrs.get('code', '').strip()
    # Also support block-style <ACTION type="RUN_PYTHON">...code...</ACTION>
    if not code:
        code = _tag_body(response_text, 'RUN_PYTHON')
    reason = python_needs_approval(code) if code else ""
    if reason:
        # The same gate RUN_COMMAND goes through, for the same
        # reasons. Pure computation still runs freely.
        res = _queue_for_approval(
            'RUN_PYTHON', dict(attrs, code=code),
            ui_queue, origin)
        print(f"[Action] RUN_PYTHON needs approval because {reason}.")
        execute_actions._last_output = res
        _record('RUN_PYTHON', res)
        return action_bus.FLOW_RECORDED
    if code:
        msg = "Running Python code..."
        print(f"[Action] {msg}")
        ui_queue.put(("action", msg))
        ui_queue.put(("info", f"Running Python:\n{code[:200]}..."))
        process = None
        tmp_path = None
        try:
            import tempfile, sys
            venv_python = os.path.join(os.path.dirname(__file__), ".venv", "Scripts", "python.exe")
            if not os.path.exists(venv_python):
                venv_python = sys.executable
            with tempfile.NamedTemporaryFile(mode='w', suffix='.py', delete=False, encoding='utf-8') as tmp:
                tmp.write(code)
                tmp_path = tmp.name
            process = subprocess.Popen(
                [venv_python, tmp_path],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                cwd=os.path.dirname(__file__),
                creationflags=getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0),
            )
            deadline = time.monotonic() + 60
            while True:
                if interrupt_event is not None and interrupt_event.is_set():
                    _terminate_started_process(process)
                    process.communicate(timeout=5)
                    execute_actions._last_output = '[Interrupted] Python and its child processes were stopped.'
                    ui_queue.put(('info', execute_actions._last_output))
                    return
                try:
                    out, err = process.communicate(timeout=0.12)
                    break
                except subprocess.TimeoutExpired:
                    if time.monotonic() >= deadline:
                        _terminate_started_process(process)
                        process.communicate(timeout=5)
                        execute_actions._last_output = '[Error] Python code timed out after 60 seconds and was stopped.'
                        ui_queue.put(('info', execute_actions._last_output))
                        return
            out = (out or "").strip()
            err = (err or "").strip()
            combined = f"{out}\n{err}".strip() if err else out
            if process.returncode != 0:
                combined = f'[Error] Python failed with exit code {process.returncode}.\n{combined}'.strip()
            elif not combined:
                combined = '[Success] Python completed with exit_code=0; no output.'
            if len(combined) > 3000:
                combined = combined[:3000] + "\n...(output truncated)"
            execute_actions._last_output = combined
            print(f"[Python Output]\n{combined}")
            ui_queue.put(("info", f"> PYTHON OUTPUT\n\n{combined[:1000]}"))
        except Exception as e:
            if process is not None and process.poll() is None:
                _terminate_started_process(process)
            execute_actions._last_output = f"[Error] Failed to run Python: {e}"
            print(execute_actions._last_output)
            ui_queue.put(("info", execute_actions._last_output))
        finally:
            if tmp_path and os.path.exists(tmp_path):
                os.unlink(tmp_path)


@action('READ_FILE', family='READ_FILE')
def _run_read_file(ctx):
    """Handles READ_FILE."""
    action_type = ctx.action_type
    attrs = ctx.attrs
    ui_queue = ctx.ui_queue
    base_dir = ctx.base_dir
    origin = ctx.origin
    turn_id = ctx.turn_id
    interrupt_event = ctx.interrupt_event
    element_map = ctx.element_map
    response_text = ctx.response_text
    file_path = attrs.get('path', '') or attrs.get('file', '')
    if file_path:
        msg = f"Reading file: {file_path}"
        print(f"[Action] {msg}")
        ui_queue.put(("action", msg))
        try:
            ext = os.path.splitext(file_path)[1].lower()
            if ext == '.ipynb':
                import nbformat
                with open(file_path, 'r', encoding='utf-8') as f:
                    nb = nbformat.read(f, as_version=4)
                lines = []
                for i, cell in enumerate(nb.cells):
                    lines.append(f"--- Cell {i+1} ({cell.cell_type}) ---")
                    lines.append(cell.source)
                    if cell.cell_type == 'code' and cell.get('outputs'):
                        for out in cell.outputs:
                            if 'text' in out:
                                lines.append(f"[Output] {''.join(out['text'])}")
                            elif 'data' in out and 'text/plain' in out['data']:
                                lines.append(f"[Output] {out['data']['text/plain']}")
                content = "\n".join(lines)
            elif ext == '.csv':
                import pandas as pd
                df = pd.read_csv(file_path)
                content = f"Shape: {df.shape}\n\nColumns: {list(df.columns)}\n\nFirst 10 rows:\n{df.head(10).to_string()}\n\nData types:\n{df.dtypes.to_string()}\n\nBasic stats:\n{df.describe().to_string()}"
            else:
                with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                    content = f.read()
            if len(content) > 6000:
                content = content[:6000] + "\n...(file truncated for context)"
            execute_actions._last_output = f"[File: {os.path.basename(file_path)}]\n{content}"
            print(f"[Action] Read {len(content)} chars from {file_path}")
            ui_queue.put(("info", f"> READ_FILE\n\n{os.path.basename(file_path)} loaded ({len(content)} chars)"))
        except Exception as e:
            execute_actions._last_output = f"[Error] Could not read file {file_path}: {e}"
            print(execute_actions._last_output)
            ui_queue.put(("info", execute_actions._last_output))


@action('GITHUB_READ_REPO', family='GITHUB_READ_REPO')
def _run_github_read_repo(ctx):
    """Handles GITHUB_READ_REPO."""
    action_type = ctx.action_type
    attrs = ctx.attrs
    ui_queue = ctx.ui_queue
    base_dir = ctx.base_dir
    origin = ctx.origin
    turn_id = ctx.turn_id
    interrupt_event = ctx.interrupt_event
    element_map = ctx.element_map
    response_text = ctx.response_text
    repo = attrs.get('repo') or attrs.get('url', '')
    if repo:
        msg = f"Reading GitHub repository: {repo}"
        print(f"[Action] {msg}")
        ui_queue.put(("action", msg))
        try:
            from providers import github_api
            data = github_api.read_repository(
                base_dir, repo, limit=int(attrs.get('limit', 80)))
            file_list = "\n".join(data['files']) or "(no files)"
            output = (
                f"[GitHub Repo: {data['repo']}]\n"
                f"Description: {data['description'] or '(none)'}\n"
                f"Visibility: {'private' if data['private'] else 'public'}\n"
                f"Default branch: {data['default_branch']}\n"
                f"Language: {data['language'] or '(unknown)'}\n"
                f"Updated: {data['updated_at']}\n\n"
                f"README:\n{data['readme'] or '(no README)'}\n\n"
                f"Files:\n{file_list}")
            if len(output) > 6000:
                output = output[:6000] + "\n...(truncated)"
            execute_actions._last_output = output
            ui_queue.put(("info", f"> GITHUB_READ_REPO\n\n{data['repo']} loaded"))
        except Exception as e:
            execute_actions._last_output = f"[Error] GitHub repo read failed: {e}"
            print(execute_actions._last_output)


@action('GITHUB_LIST_REPOS', family='GITHUB_LIST_REPOS')
def _run_github_list_repos(ctx):
    """List repositories visible to VANTA's installed GitHub App."""
    from providers import github_api
    try:
        repositories = github_api.list_repositories(
            ctx.base_dir, limit=int(ctx.attr('limit', 100)))
        result = ("[Success] GitHub App can access "
                  f"{len(repositories)} repositories:\n" +
                  ("\n".join(f"- {name}" for name in repositories)
                   if repositories else "(none)"))
    except Exception as exc:
        result = f"[Error] GitHub repository listing failed: {exc}"
    ctx.report(result)
    ctx.say(f"> GITHUB_LIST_REPOS\n\n{result}")


@action('GITHUB_READ_FILE', family='GITHUB_READ_FILE')
def _run_github_read_file(ctx):
    """Handles GITHUB_READ_FILE."""
    action_type = ctx.action_type
    attrs = ctx.attrs
    ui_queue = ctx.ui_queue
    base_dir = ctx.base_dir
    origin = ctx.origin
    turn_id = ctx.turn_id
    interrupt_event = ctx.interrupt_event
    element_map = ctx.element_map
    response_text = ctx.response_text
    repo = attrs.get('repo') or attrs.get('url', '')
    path = attrs.get('path', '')
    if repo:
        msg = f"Reading GitHub file: {repo}{('/' + path) if path else ''}"
        print(f"[Action] {msg}")
        ui_queue.put(("action", msg))
        try:
            from providers import github_api
            data = github_api.read_file(
                base_dir, repo, path=path, ref=attrs.get('ref', ''))
            content = data['content']
            if len(content) > 6000:
                content = content[:6000] + "\n...(truncated)"
            execute_actions._last_output = (
                f"[GitHub File: {data['repo']}/{data['path']} "
                f"@ {data['ref']}]\n{content}")
            ui_queue.put(("info", f"> GITHUB_READ_FILE\n\n{data['path']} loaded"))
        except Exception as e:
            execute_actions._last_output = f"[Error] GitHub file read failed: {e}"
            print(execute_actions._last_output)


@action('GITHUB_SEARCH', family='GITHUB_SEARCH')
def _run_github_search(ctx):
    """Handles GITHUB_SEARCH."""
    action_type = ctx.action_type
    attrs = ctx.attrs
    ui_queue = ctx.ui_queue
    base_dir = ctx.base_dir
    origin = ctx.origin
    turn_id = ctx.turn_id
    interrupt_event = ctx.interrupt_event
    element_map = ctx.element_map
    response_text = ctx.response_text
    query = attrs.get('query', '')
    # `type` identifies the action itself. Reusing it for the
    # search category caused the parser to dispatch an unknown
    # action called REPOSITORIES or CODE.
    search_type = attrs.get('search_type', 'repositories').lower()
    if search_type not in {'repositories', 'code'}:
        search_type = 'repositories'
    if query:
        msg = f"Searching GitHub: {query}"
        print(f"[Action] {msg}")
        ui_queue.put(("action", msg))
        try:
            import requests as _req
            gh_token = os.getenv("GITHUB_TOKEN", "")
            headers = {"Accept": "application/vnd.github.v3+json"}
            if gh_token and gh_token != "your_github_pat_here":
                headers["Authorization"] = f"token {gh_token}"
            r = _req.get(f"https://api.github.com/search/{search_type}",
                         params={"q": query, "per_page": 8, "sort": "stars"},
                         headers=headers, timeout=10)
            if r.status_code == 200:
                items = r.json().get("items", [])
                lines = []
                for item in items:
                    if search_type == "repositories":
                        lines.append(f"- {item.get('full_name')} ★{item.get('stargazers_count',0)}\n  {item.get('description','')}\n  {item.get('html_url','')}")
                    else:
                        lines.append(f"- {item.get('repository',{}).get('full_name','')}: {item.get('path','')}\n  {item.get('html_url','')}")
                output = f"[GitHub Search: {query}]\n\n" + "\n\n".join(lines)
                execute_actions._last_output = output
                ui_queue.put(("info", f"> GITHUB_SEARCH\n\n{len(items)} results for '{query}'"))
            else:
                execute_actions._last_output = f"[Error] GitHub search failed: status {r.status_code}"
        except Exception as e:
            execute_actions._last_output = f"[Error] GitHub search failed: {e}"
            print(execute_actions._last_output)


@action('OPEN_URL', family='OPEN_URL')
def _run_open_url(ctx):
    """Handles OPEN_URL."""
    action_type = ctx.action_type
    attrs = ctx.attrs
    ui_queue = ctx.ui_queue
    base_dir = ctx.base_dir
    origin = ctx.origin
    turn_id = ctx.turn_id
    interrupt_event = ctx.interrupt_event
    element_map = ctx.element_map
    response_text = ctx.response_text
    url = attrs.get('url', '')
    msg = f"Opening URL: {url}"
    print(f"[Action] {msg}")
    ui_queue.put(("action", msg))
    # This wrote no receipt at all, so the pipeline's own "the handler never
    # reported anything" guard fired and told the model "OPEN_URL produced no
    # result. Check that its required attributes were supplied and spelled
    # correctly" -- about a browser that had in fact opened. The model then
    # went looking for a misspelling that was never there. The page having
    # loaded is not something this can know; which route opened it is, so
    # that is what it says.
    if not url:
        ctx.report('[Error] OPEN_URL needs url="...".')
        return
    try:
        _navigate_browser(url, ui_queue)
        import time as _time
        _last_opened_url.update(url=url, at=_time.time())
        ctx.report(f"[Success] Opened {url} in HIS default browser, for him "
                   f"to look at. That is not the browser you control: "
                   f"BROWSER_READ and BROWSER_SNAPSHOT cannot see it. If you "
                   f"need to read the page, check for something on it or work "
                   f"inside it, use BROWSER_GOTO on this URL instead. Whether "
                   f"the page loaded is not confirmed here.")
    except Exception as e:
        print(f"[Action Error] _navigate_browser failed ({e}); falling back to webbrowser.open")
        import webbrowser
        opened = webbrowser.open(url)
        time.sleep(2.5)
        if opened:
            ctx.report(f"[Success] Opened {url} in the default browser "
                       f"(the controlled browser was unavailable: {e}).")
        else:
            ctx.report(f"[Error] Could not open {url}: the controlled browser "
                       f"failed ({e}) and the default browser refused it too.")




# ------------------------------------------------------------------ #
# PURSUITS -- work that outlives a turn
#
# Six handlers, six decorators, and nothing else to edit: the catalogue, the
# retrieval index, the schema, the authorization class and the mode policy all
# come from the declarations in capabilities.py. This is what the bus was for.
# ------------------------------------------------------------------ #

def _pursuit_lines(ctx):
    """The tag body as a list of lines, for the actions that take a plan."""
    body = _tag_body(ctx.response_text, ctx.action_type)
    return [line.strip(" -*\t") for line in body.split("\n") if line.strip()]


def _pursuit_failure(ctx, exc):
    """A refusal that says what to do instead, never a bare exception."""
    ctx.report(f"[Error] {exc}")
    ctx.say(f"> {ctx.action_type}\n\n{ctx.receipt}")
    return None


@action("START_PURSUIT", family="pursuits",
        receipt="the pursuit id and how many steps it starts with")
def _run_start_pursuit(ctx):
    """Begin tracking work that spans more than one conversation."""
    import pursuits
    ctx.step("Starting a pursuit")
    try:
        result = pursuits.start(ctx.base_dir, ctx.attr("title"),
                                ctx.attr("why"), _pursuit_lines(ctx))
    except pursuits.PursuitError as exc:
        return _pursuit_failure(ctx, exc)
    ctx.report(result)
    ctx.say(f"> START_PURSUIT\n\n{result}")
    return None


@action("LIST_PURSUITS", family="pursuits",
        receipt="each open pursuit with its counted progress and blocker")
def _run_list_pursuits(ctx):
    """What is being worked on across conversations."""
    import pursuits
    result = pursuits.summary(ctx.base_dir, include_closed=ctx.flag("closed"))
    ctx.report(f"[Success] {result}")
    ctx.say(f"> LIST_PURSUITS\n\n{result}")
    return None


@action("PURSUIT_STATUS", family="pursuits",
        receipt="the steps with their states, receipts and blockers")
def _run_pursuit_status(ctx):
    """One pursuit in full, so the next step is real rather than guessed."""
    import pursuits
    try:
        result = pursuits.describe(ctx.base_dir, ctx.attr("id"))
    except pursuits.PursuitError as exc:
        return _pursuit_failure(ctx, exc)
    ctx.report(f"[Success] {result}")
    ctx.say(f"> PURSUIT_STATUS\n\n{result}")
    return None


@action("PLAN_PURSUIT", family="pursuits",
        receipt="how many steps were added and the progress after")
def _run_plan_pursuit(ctx):
    """Break a pursuit into steps that can each be finished and evidenced."""
    import pursuits
    steps = _pursuit_lines(ctx)
    if not steps:
        ctx.report('[Error] PLAN_PURSUIT needs the steps in the tag body, one '
                   'per line, between <ACTION ...> and </ACTION>.')
        ctx.say(ctx.receipt)
        return None
    try:
        result = pursuits.plan(ctx.base_dir, ctx.attr("id"), steps)
    except pursuits.PursuitError as exc:
        return _pursuit_failure(ctx, exc)
    ctx.report(result)
    ctx.say(f"> PLAN_PURSUIT\n\n{result}")
    return None


@action("ADVANCE_PURSUIT", family="pursuits",
        receipt="the step, the evidence recorded against it, and the progress")
def _run_advance_pursuit(ctx):
    """Close a step against evidence, or record what is blocking it."""
    import pursuits
    try:
        result = pursuits.advance(ctx.base_dir, ctx.attr("id"),
                                  ctx.attr("step"), ctx.attr("receipt"),
                                  ctx.attr("blocked"))
    except pursuits.PursuitError as exc:
        return _pursuit_failure(ctx, exc)
    ctx.report(result)
    ctx.say(f"> ADVANCE_PURSUIT\n\n{result}")
    return None


@action("CLOSE_PURSUIT", family="pursuits",
        receipt="the final state and how many steps were left unfinished")
def _run_close_pursuit(ctx):
    """Finish a pursuit, so what remains open stays an honest list."""
    import pursuits
    state = (ctx.attr("state") or pursuits.DONE).lower()
    try:
        result = pursuits.close(ctx.base_dir, ctx.attr("id"),
                                ctx.attr("outcome"), state)
    except pursuits.PursuitError as exc:
        return _pursuit_failure(ctx, exc)
    ctx.report(result)
    ctx.say(f"> CLOSE_PURSUIT\n\n{result}")
    return None


@action("CURIOSITY_LOG", family="curiosity",
        receipt="each recent session with its subject, reason and sources")
def _run_curiosity_log(ctx):
    """What it read up on while the machine was idle."""
    import curiosity
    try:
        limit = int(ctx.attr("limit") or 8)
    except ValueError:
        limit = 8
    result = curiosity.log_text(ctx.base_dir, max(1, min(limit, 30)))
    ctx.report(result)
    ctx.say(f"> CURIOSITY_LOG\n\n{result}")
    return None


@action("CURIOSITY_PAUSE", family="curiosity",
        receipt="until when background research is paused, or that it is on")
def _run_curiosity_pause(ctx):
    """Stop background reading for a while, or let it carry on."""
    import curiosity
    raw = ctx.attr("minutes")
    try:
        minutes = float(raw)
    except (TypeError, ValueError):
        ctx.report('[Error] CURIOSITY_PAUSE needs minutes="N" -- 0 turns it '
                   'back on.')
        ctx.say(ctx.receipt)
        return None
    result = curiosity.pause(ctx.base_dir, min(minutes, 60 * 24 * 90))
    ctx.report(result)
    ctx.say(f"> CURIOSITY_PAUSE\n\n{result}")
    return None


@action("SYNC_BROWSER_LOGIN", family="browser",
        receipt="how many cookies arrived and which sites, never a value")
def _run_sync_browser_login(ctx):
    """Borrow the sessions from Himanshu's own Firefox."""
    import browser
    ctx.step("Borrowing your Firefox sessions")
    raw = ctx.attr("domains")
    domains = [part.strip() for part in raw.replace(",", " ").split()
               if part.strip()] or None
    result = browser.sync_login(domains)
    ctx.report(result)
    ctx.say(f"> SYNC_BROWSER_LOGIN\n\n{result}")
    return None


# ------------------------------------------------------------------ #
# FACE IDENTITY -- explicit enrollment, local matching, graph linkage
# ------------------------------------------------------------------ #

def _face_attempts(ctx, reason, work, retry_marker, retain=True, tries=3):
    """Capture and run `work(path)` until a face is found, up to `tries` frames.

    One dim frame often has no findable face and the next does, so a miss is
    retried before it is reported.
    """
    import camera
    result = "[Error] The camera gave no frame."
    for attempt in range(tries):
        shot = camera.capture(ctx.base_dir, mode="verify" if retain else "awareness",
                              reason=reason, retain=retain)
        if not shot.ok:
            return shot.for_model()
        try:
            result = work(shot.path)
        finally:
            if not retain:
                try:
                    os.remove(shot.path)
                except OSError:
                    pass
        if retry_marker not in result:
            break
        time.sleep(0.8)
    return result


@action("REMEMBER_FACE", family="face_identity",
        requires=(action_bus.user_only(),),
        receipt="person, encrypted template id, model and quality")
def _run_remember_face(ctx):
    """Capture one person and link an encrypted template to the graph."""
    import face_identity
    person, phone = ctx.attr("person"), ctx.attr("phone")
    ctx.step(f"Learning {person}'s face")
    result = _face_attempts(
        ctx, f"remember {person}",
        lambda path: face_identity.enroll_person(person, path, phone=phone),
        "No sufficiently clear face")
    ctx.report(result)
    ctx.say(f"> REMEMBER_FACE\n\n{result}")
    return None


@action("WHO_IS_THIS", family="face_identity",
        requires=(action_bus.user_only(),),
        receipt="each visible face's name, match score and linked phone number")
def _run_who_is_this(ctx):
    """Look through the webcam and name whoever is there from the graph."""
    import face_identity
    ctx.step("Looking at who is there")
    result = _face_attempts(ctx, "who is this", face_identity.who_is_this,
                            "could not see a clear face", retain=False)
    ctx.report(result)
    ctx.say(f"> WHO_IS_THIS\n\n{result}")
    return None


@action("LIST_KNOWN_FACES", family="face_identity",
        requires=(action_bus.user_only(),),
        receipt="known-person count and template count for each")
def _run_list_known_faces(ctx):
    import face_identity
    result = face_identity.known_people()
    ctx.report(result)
    ctx.say(f"> LIST_KNOWN_FACES\n\n{result}")
    return None


@action("VERIFY_PRIMARY_FACE", family="face_identity",
        requires=(action_bus.user_only(),),
        receipt="fresh local primary-face result")
def _run_verify_primary_face(ctx):
    import camera_awareness
    state, status = camera_awareness.verify_primary_now(ctx.base_dir)
    wording = {
        "primary_confirmed": "Himanshu is recognized in the current frame.",
        "other_or_unknown_face": (
            "A face is visible, but it does not match Himanshu confidently."),
        "person_visible_face_unclear": (
            "A person is visible, but the face is not clear enough to match."),
        "no_person_visible": "No person is clearly visible in the current frame.",
        "low_light_or_uncertain": (
            "The current frame is too uncertain for a reliable face check."),
        "capture_failed": "The fresh camera check failed; identity is unverified.",
    }
    result = "[Success] " + wording.get(status, "Identity remains unverified.")
    ctx.report(result)
    ctx.say(f"> VERIFY_PRIMARY_FACE\n\n{result}")
    return state


@action("FORGET_FACE", family="face_identity",
        requires=(action_bus.user_only(),),
        receipt="person and number of biometric templates erased")
def _run_forget_face(ctx):
    import face_identity
    result = face_identity.forget_person(ctx.attr("person"))
    ctx.report(result)
    ctx.say(f"> FORGET_FACE\n\n{result}")
    return None


# ------------------------------------------------------------------ #
# RECEIPT CONTRACTS
#
# What each side-effecting action owes as evidence that it happened.
#
# The recurring defect in this system is an action claiming a success it had
# not earned, and every instance has had the same shape: a receipt that
# asserts instead of citing. A calendar event whose receipt was a bare URL --
# so the critic could not verify it, and the model opened the URL and landed
# on Google's marketing page. A typing action reporting len(text) while a
# character had been dropped. A draw that rendered nothing and reported
# success. A file save that silently did nothing and spent an approval doing
# it.
#
# A contract is a sentence saying what a success must return. Listing them in
# one table rather than in 53 decorators is deliberate: the question worth
# being able to ask is "what does each action owe?", and a table answers it.
#
# VERIFIED_RECEIPTS names the ones additionally held to their contract at run
# time -- a success that cites nothing checkable is downgraded to
# [Unverified] rather than passed on as done. A contract without verification
# is documentation. Useful, but not a control, so the set grows as each one is
# confirmed against the real handler rather than all at once.
# ------------------------------------------------------------------ #

RECEIPT_CONTRACTS = {
    # --- the machine ------------------------------------------------- #
    "RUN_COMMAND": "the command, its exit status, and the output it produced",
    "RUN_PYTHON": "the interpreter's own stdout and stderr, not a summary",
    "RUN_BLENDER_SCRIPT": "Blender's exit status and the file it wrote",
    "RUNTIME_APPLY": "the committed revision, saved artifact path, Blender process id, and observed scene state",
    "RUNTIME_STATUS": "the project revision, artifact path, Blender process id, and observed scene state",
    "RUNTIME_RESUME": "the project name, artifact path, and Blender process id",
    "RUNTIME_PRESENT": "the project name, Blender process id, presentation state, and observed scene state",
    "KILL_PROCESS": "the process name and pid that was ended",
    "LAUNCH_APP": "the window that actually came up, not the launch attempt",
    "LOCK_SCREEN": "that the workstation reported itself locked",
    "SHUTDOWN": "the scheduled time, or the refusal",
    "RESTART": "the scheduled time, or the refusal",
    "SET_TIMER": "the duration and the clock time it will fire at",

    # --- files --------------------------------------------------------- #
    "MOVE_FILE": "the source and destination paths, after the move",
    "DELETE_FILE": "the path removed and where its backup went",
    "WRITE_PROJECT_FILE": "the project-relative path and the byte count",
    "EDIT_PROJECT_FILE": "the path, and how many occurrences were replaced",
    "DELETE_PROJECT_FILE": "the path removed and where its backup went",
    "RUN_IN_PROJECT": "the command, its exit status and its output",
    "WRITE_CODE": "the path written and the byte count",
    "COPY_FILE": "the source and destination paths, after the copy",
    "CODING_TASK_START": "the task id, so its progress can be asked about",
    "CODING_TASK_CANCEL": "the task id and its state after cancelling",

    # --- Drive --------------------------------------------------------- #
    "DRIVE_UPLOAD": "the Drive file id and the byte count, read back from the "
                    "API response rather than from the request",
    "DRIVE_CREATE_FOLDER": "the new folder's Drive id",
    "DRIVE_RENAME": "the file id and its name as Drive now reports it",
    "DRIVE_MOVE": "the file id and its parent folder after the move",
    "DRIVE_COPY": "the id of the copy, which is not the id of the original",
    "DRIVE_TRASH": "the file id and that Drive reports it trashed",
    "DRIVE_SHARE": "who it was shared with and the permission id",
    "DRIVE_RESTORE": "the file id and its name as Drive reports it after leaving the trash",
    "PEOPLE_CREATE": "the new contact's resource name, read back from the People API",
    "PEOPLE_UPDATE": "the contact's resource name and which fields changed, from the API response",
    "PEOPLE_DELETE": "the contact's resource name and name, after the API accepted the delete",
    "OPEN_BRAIN_MAP": "that the local graph server answered a request for its own data, and how many nodes it holds",
    "CREATE_PRESENTATION": "the .pptx path and slide count, read back from the saved file",
    "CREATE_DOCUMENT": "the .docx path and word count, read back from the saved file",
    "CREATE_SPREADSHEET": "the .xlsx path with each sheet's row and column counts, read back from the saved file",
    "CREATE_PDF": "the .pdf path and page count, read back from the saved file",
    "OPEN_PATH": "that the path exists and was handed to the default app or Explorer",
    "DOWNLOAD_FILE": "the saved path and its byte size on disk, which must be above zero",
    "ZIP_PATH": "the archive path, its size and how many files it holds",
    "UNZIP_FILE": "how many files were written and the folder they went to",
    "NOTE_ADD": "the note id, read back from the notes file after writing",
    "NOTE_DELETE": "the deleted note id, confirmed absent from the notes file",
    "PDF_MERGE": "the merged PDF path and page count",
    "PDF_EXTRACT": "the new PDF path and how many pages it holds",

    # --- calendar and tasks --------------------------------------------- #
    "ADD_CALENDAR_EVENT": "the event id, title, time and timezone, read back "
                          "from the API response",
    "UPDATE_CALENDAR_EVENT": "the event id and its time after the change, "
                             "read back from the API response",
    "DELETE_CALENDAR_EVENT": "the event id that was removed",
    "ADD_TASK": "the task id and its title as stored",
    "UPDATE_TASK": "the task id and what changed",
    "COMPLETE_TASK": "the task id and its completion time",
    "TRACK_COMMITMENT": "the commitment id and when it is due",
    "COMPLETE_COMMITMENT": "the commitment id and its completion time",

    # --- messages ------------------------------------------------------- #
    "SEND_EMAIL": "the Gmail message id, recipient, subject and attachment "
                  "filenames returned after Gmail accepted the message",
    "REPLY_EMAIL": "the original Gmail message id, sent message id, thread "
                   "id, recipient, subject and attachment filenames",
    "SEND_TELEGRAM": "the recipient and that Telegram accepted the message",
    "SEND_TELEGRAM_FILE": "the local filename, byte size, Telegram message "
                          "id and paired recipient after Telegram accepts it",
    "SEND_WHATSAPP": "the resolved contact and that the message was sent, "
                     "never the contact string that was asked for",

    # --- watches --------------------------------------------------------- #
    "WATCH_CREATE": "the watch id and the condition it is watching for",
    "WATCH_CANCEL": "the watch id that was cancelled",
    "WATCH_PAUSE": "the watch id and its state after pausing",
    "WATCH_RESUME": "the watch id and its state after resuming",
    "FLIGHT_WATCH_CREATE": "the watch id and the route and date being watched",

    # --- memory ---------------------------------------------------------- #
    "REMEMBER_FACT": "the fact as it was stored, which is not always the fact "
                     "as it was given",
    "CORRECT_FACT": "the fact before and after the correction",
    "FORGET_FACT": "what was removed and how many nodes it touched",
    "LEARN_KNOWLEDGE": "how many items were stored and under what topic",
    "SET_LIFE_PREFERENCE": "the preference as stored",
    "ADD_LIFE_GOAL": "the goal as stored and its id",
    "KEEP_DATASET_PREVIEW": "the destination path and the row count",

    # --- media ------------------------------------------------------------ #
    "SPOTIFY_QUEUE": "the track that was queued, as Spotify named it back",

    # --- the editor -------------------------------------------------------- #
    "IDE_OPEN_PROJECT": "the project path the editor reported opening",
    "IDE_OPEN_FILE": "the file and line the editor moved to",
    "IDE_SHOW_DIFF": "the two paths being compared",
    "CODING_MODE": "the project the surface came up on and its file count, "
                   "read back from the surface's own state",

    # --- the browser --------------------------------------------------------- #
    "OPEN_URL": "the URL that was opened and which browser route handled it",
}

#: Held to their contract at run time, not merely documented. Each one here
#: has been checked against its handler, so a downgrade means the handler
#: genuinely stopped citing evidence rather than that the contract was
#: optimistic.
VERIFIED_RECEIPTS = frozenset({
    "ADD_CALENDAR_EVENT",
    "UPDATE_CALENDAR_EVENT",
    "DELETE_CALENDAR_EVENT",
    "DRIVE_UPLOAD",
    "DRIVE_CREATE_FOLDER",
    "WRITE_PROJECT_FILE",
    "EDIT_PROJECT_FILE",
    "DELETE_PROJECT_FILE",
    "RUN_COMMAND",
    "KILL_PROCESS",
    "SET_TIMER",
    "WATCH_CREATE",
    "CODING_MODE",
    "OPEN_URL",
    "SEND_EMAIL",
    "REPLY_EMAIL",
    "SEND_TELEGRAM_FILE",
    "RUNTIME_APPLY",
    "RUNTIME_STATUS",
    "RUNTIME_RESUME",
    "RUNTIME_PRESENT",
})

_CONTRACT_REPORT = action_bus.BUS.apply_contracts(RECEIPT_CONTRACTS,
                                                  VERIFIED_RECEIPTS)
if _CONTRACT_REPORT["unknown"]:
    print(f"[Actions] Receipt contracts name actions that are not registered: "
          f"{_CONTRACT_REPORT['unknown']}")


def _run_envelope_sequence(envelopes, response_text, element_map, ui_queue,
                           base_dir, origin, interrupt_event, turn_id):
    """One call at a time, including legacy file/alarm passes, in source order.

    A blocked or uncertain prerequisite leaves later calls unexecuted. The
    model receives each receipt and can choose a different recovery step.
    """
    results, stopped = [], False
    for envelope in envelopes:
        name = action_attrs.parse(envelope.attributes).get('type', '').upper()
        if stopped or (interrupt_event is not None and interrupt_event.is_set()):
            receipt = f'[Not Run] {name}: an earlier action is unresolved or the batch was cancelled.'
            results.append(dict(type=name, ok=False, output=receipt, not_run=True))
            continue
        execute_actions(envelope.text, element_map, ui_queue, base_dir,
                        origin=origin, interrupt_event=interrupt_event,
                        turn_id=turn_id)
        current = list(getattr(execute_actions, '_results', []))
        if not current:
            current = [dict(type=name, ok=False,
                            output=f'[Unverified] {name}: no execution receipt was recorded.')]
        results.extend(current)
        stopped = (any(not row.get('ok') for row in current)
                   or getattr(execute_actions, '_stop_batch', False))
    execute_actions._results = results
    execute_actions._last_output = '\n'.join(
        f"{row['type']}: {row['output']}" for row in results)
    clean = str(response_text)
    for envelope in reversed(envelopes):
        clean = clean[:envelope.start] + clean[envelope.end:]
    return clean.strip()


def execute_actions(response_text, element_map, ui_queue, base_dir, origin="user", interrupt_event=None, turn_id=None):
    """
    Parses <ACTION ... /> tags and executes them. Sets
    execute_actions._blocked_reason (str or None) if any action this
    call was refused by the self-protection guard, so the caller
    (agent.py) can report it back to the model. Also sets
    execute_actions._last_click_check to (intended_label, actual_label)
    whenever a CLICK_ID resolves to a verifiable UIA element, so the
    caller can flag a mismatch between what the model meant to click
    and what it actually hit. Returns clean text for TTS.
    """
    execute_actions._blocked_reason = None
    execute_actions._stop_batch = False
    execute_actions._last_click_check = None
    execute_actions._last_output = "[Error] No executable action was parsed."
    execute_actions._last_command_result = None
    execute_actions._offline_completion = ''
    execute_actions._direct_user_delivery = None
    execute_actions._direct_user_result = None
    execute_actions._direct_user_spoken = None
    execute_actions._results = []
    execute_actions._turn_id = str(turn_id or "")
    action_outputs = []
    post_ready = []
    try:
        import authentication_gate
        if authentication_gate.awaiting():
            blocked = "[Blocked] VANTA is waiting for lifecycle authentication."
            execute_actions._last_output = blocked
            execute_actions._results = [
                {"type": "IDENTITY_GATE", "ok": False, "output": blocked}]
            return ""
    except Exception:
        pass

    # Parse top-level envelopes once. Code, documents and mail may contain
    # action examples; those remain part of their enclosing payload.
    from action_protocol import extract
    from autonomy import KNOWN_ACTIONS
    batch_response = response_text
    envelopes = extract(response_text, bare_names=KNOWN_ACTIONS)
    if len(envelopes) > 1:
        return _run_envelope_sequence(
            envelopes, response_text, element_map, ui_queue, base_dir,
            origin, interrupt_event, turn_id)
    actions = [envelope.attributes for envelope in envelopes]
    action_fragments = [envelope.text for envelope in envelopes]

    tool_calls = re.findall(r'<function=([A-Z_]+)>\s*(.*?)\s*</function>', response_text, re.IGNORECASE | re.DOTALL)
    for func_type, func_args in tool_calls:
        param_matches = re.findall(r'<parameter=([a-zA-Z0-9_]+)>\s*(.*?)\s*</parameter>', func_args, re.IGNORECASE | re.DOTALL)
        if param_matches:
            formatted_args = " ".join([f'{k}="{v.strip()}"' for k, v in param_matches])
            actions.append(f'type="{func_type.upper()}" {formatted_args}')
            action_fragments.append(response_text)
        else:
            func_args = func_args.replace('\n', ' ').strip()
            actions.append(f'type="{func_type.upper()}" {func_args}')
            action_fragments.append(response_text)

    for action_index, action_str in enumerate(actions):
        # Handlers and the duplicate ledger see this call's body, not the
        # first body of another call with the same action name.
        response_text = action_fragments[action_index]
        previous_output = str(getattr(execute_actions, '_last_output', '') or '')
        try:
            import authentication_gate
            if authentication_gate.awaiting():
                execute_actions._last_output = (
                    "[Blocked] Lifecycle authentication became required before "
                    "the next action.")
                break
        except Exception:
            pass
        if interrupt_event is not None and interrupt_event.is_set():
            execute_actions._last_output = "[Interrupted] Paused before the next action to hear Himanshu."
            break
        attrs = action_attrs.parse(action_str)

        # Unescape HTML entities in attributes (like &quot; to ")
        for k in attrs:
            attrs[k] = html.unescape(attrs[k])

        action_type = attrs.get('type', '').upper()
        # Older names that survived in the catalogue and in transcripts but have
        # no handler. Left unmapped they are refused as unknown actions, which
        # is honest but unhelpful when a working equivalent exists.
        _ALIASES = {
            'MOUSE_MOVE': 'MOVE_MOUSE', 'MOUSE_DRAG': 'DRAG',
            'DOUBLECLICK': 'DOUBLE_CLICK', 'RIGHTCLICK': 'RIGHT_CLICK',
        }
        if action_type in _ALIASES:
            action_type = _ALIASES[action_type]
            attrs['type'] = action_type
        if action_type in {"SEND_EMAIL", "REPLY_EMAIL"} and not str(
                attrs.get("body") or "").strip():
            # Preserve the canonical block form as ordinary action data before
            # schema validation and approval. Previously a separate post-loop
            # parser owned the body, so the approval queued by the main
            # pipeline did not contain the text being approved.
            block_body = _tag_body(response_text, action_type)
            if block_body:
                attrs["body"] = block_body
        # A retired name is answered from a table, so adding another one is a
        # dictionary entry rather than another test in this pipeline.
        guidance = RETIRED_ACTIONS.get(action_type, "")
        if guidance:
            guidance = guidance.format(action_type=action_type)
            execute_actions._last_output = guidance
            execute_actions._results.append(
                {"type": action_type, "ok": False, "output": guidance})
            action_outputs.append(f"{action_type}: {guidance}")
            ui_queue.put(("info", guidance))
            continue
        # File writes are owned by the post-loop pass further down, which has
        # the content (from the body or from content="...") and now enforces
        # confirmation itself. Letting the main scan also see them meant one
        # action queued an approval here and was written there, so the receipt
        # announced it was waiting while the file already existed.
        if action_type in DEFERRED_ACTIONS:
            continue
        allowed, policy_error = _action_allowed(action_type, origin)
        if not allowed:
            execute_actions._last_output = policy_error
            execute_actions._results.append(
                {"type": action_type, "ok": False, "output": policy_error})
            action_outputs.append(f"{action_type}: {policy_error}")
            ui_queue.put(("info", policy_error))
            continue

        # The same side effect must not happen twice because a repeated model
        # step re-issued it. Chrome and the music player have each opened
        # twice from one request. Restricted to side-effecting actions, and
        # overridable with repeat="true" so that "play it again" still works:
        # the aim is to stop the accidental repeat, not the deliberate one.
        try:
            import execution_reliability as _reliability
            _runtime = getattr(execute_actions, "_outcome_runtime", None)
            if (_runtime is not None
                    and action_type in _reliability.DEDUPLICATED_ACTIONS
                    and str(attrs.get(_reliability.REPEAT_ATTR, "")).lower()
                    not in ("true", "yes", "1")):
                if _runtime.already_dispatched(action_type, attrs, turn_id):
                    notice = (
                        f"[Error] {action_type} with these exact arguments "
                        f"already ran in this turn and was not repeated. If a "
                        f"second one is genuinely wanted, reissue it with "
                        f"repeat=\"true\"; otherwise continue to the next step.")
                    execute_actions._last_output = notice
                    execute_actions._results.append(
                        {"type": action_type, "ok": False, "output": notice})
                    action_outputs.append(f"{action_type}: {notice}")
                    ui_queue.put(("info", notice))
                    continue
                _runtime.note_dispatch(action_type, attrs, turn_id)
        except Exception as _dedupe_exc:
            print(f"[Reliability] Duplicate check unavailable: {_dedupe_exc}")

        # Catch an unusable action before it reaches a handler. Without this a
        # wrong attribute name simply falls through the handler's guard, does
        # nothing, and reports whatever the previous action returned.
        import action_schema
        # A self-closing tag followed by normal response text is not a block.
        # The previous ``>\s*\S`` check counted the next ``</ACTION>`` or log
        # token as body content and let an empty RUNTIME_APPLY pass validation.
        has_body = bool(_tag_body(response_text, action_type).strip())
        schema_error = action_schema.validate(action_type, attrs, has_body=has_body)
        if schema_error:
            execute_actions._last_output = schema_error
            execute_actions._results.append(
                {"type": action_type, "ok": False, "output": schema_error})
            action_outputs.append(f"{action_type}: {schema_error}")
            print(f"[Schema] {schema_error}")
            ui_queue.put(("info", schema_error))
            continue

        observer_block = _observer_mode_block(action_type, attrs)
        if observer_block:
            execute_actions._last_output = observer_block
            execute_actions._results.append(
                {"type": action_type, "ok": False, "output": observer_block})
            ui_queue.put(("info", observer_block))
            continue

        # Starting media is non-idempotent even when the query changes: a stale
        # verification signal must not launch several tracks over one another.
        # One start attempt per user task; subsequent steps may only inspect the
        # current player or report the blocker.
        if action_type in ONCE_PER_TASK_ACTIONS:
            if action_type in _task_attempts:
                blocked = ONCE_PER_TASK_ACTIONS[action_type].format(
                    action_type=action_type)
                execute_actions._last_output = blocked
                execute_actions._results.append(
                    {"type": action_type, "ok": False, "output": blocked})
                action_outputs.append(f"{action_type}: {blocked}")
                print(f"[Ledger] Suppressed repeated {action_type} attempt.")
                ui_queue.put(("info", blocked))
                continue
            _task_attempts.add(action_type)

        # Replay guard: an identical side-effecting action already completed
        # in this task returns its original receipt instead of running twice.
        action_body = (_tag_body(response_text, action_type)
                       if action_type in BLOCK_BODY_ACTIONS else "")
        ledger_key = _ledger_key(action_type, attrs, action_body)
        if action_type in SIDE_EFFECT_ACTIONS and ledger_key in _task_ledger:
            repeat = (
                f"[Already Done] {action_type} was already completed earlier in this task "
                f"and was not run again. Original result: {_task_ledger[ledger_key]}"
            )
            execute_actions._last_output = repeat
            execute_actions._results.append(
                {"type": action_type, "ok": True, "output": repeat, "replayed": True})
            action_outputs.append(f"{action_type}: {repeat}")
            print(f"[Ledger] Suppressed duplicate {action_type}.")
            ui_queue.put(("info", repeat))
            continue

        # Desktop control now has a real dispatcher (see desktop.py), so this
        # blanket refusal only applies when observer mode is deliberately on.
        # _observer_mode_block above already handles that case.

        # --- PHASE 2: Approvals Intercept ---
        import uuid
        import json

        # An action replayed after Himanshu approved it must not be queued again.
        already_approved = attrs.get('approved', '').strip().lower() == 'true'
        is_consequential = _policy_confirmation_required(action_type) and not already_approved
        if action_type == "RUN_COMMAND" and not already_approved:
            # Shell use itself is not consequential. Builds, installs,
            # diagnostics and defensive security commands run directly; only
            # the concrete high-impact command earns a checkpoint.
            import command_policy
            is_consequential = bool(command_policy.confirmation_reason(
                attrs.get("cmd", "")))
        if is_consequential:
            # A corrected retry of something he already approved inherits that
            # approval, once, briefly. Without this the fix queued for approval
            # all over again and he approved the same intent twice -- once for
            # the broken command, once for the one that worked.
            import approval_grants
            if approval_grants.consume(action_type):
                is_consequential = False
        if action_type == 'ADD_ALARM' and origin != 'user' and not already_approved:
            is_consequential = True

        payload_json = json.dumps(attrs)

        if is_consequential:
            # There were two copies of this. RUN_COMMAND took this one, which
            # never emitted the on-screen prompt -- so the buttons existed and
            # the action that most needed them could not reach them. One
            # implementation now, so a fix to approval applies to every action.
            runtime = getattr(execute_actions, "_outcome_runtime", None)
            outcome_id = (runtime.outcome_for_action(action_type)
                          if runtime is not None else "")
            pending = _queue_for_approval(
                action_type, attrs, ui_queue, origin,
                turn_id=turn_id or "", outcome_id=outcome_id)
            execute_actions._last_output = pending
            execute_actions._results.append(
                {"type": action_type, "ok": False, "output": pending})
            action_outputs.append(f"{action_type}: {pending}")
            continue

        # These legacy actions need their complete tag body in a dedicated
        # pass below.  They are still announced here, after policy and approval
        # gates, but must not visit the bus's honest "unregistered" handler on
        # the way there.
        if action_type in POST_LOOP_ACTIONS:
            announce_action(attrs, action_type)
            if interrupt_event is not None and interrupt_event.is_set():
                interrupted = (
                    f"[Interrupted] {action_type} was stopped before it ran.")
                execute_actions._last_output = interrupted
                execute_actions._results.append(
                    {"type": action_type, "ok": False,
                     "output": interrupted})
                action_outputs.append(f"{action_type}: {interrupted}")
                break
            post_ready.append((action_type, dict(attrs)))
            continue

        try:
            # One lookup. Which handler runs for this name is a
            # dictionary entry, not a rung on a ladder -- see
            # action_bus.py and the handlers above.
            announce_action(attrs, action_type)
            # Barge-in may have arrived during the (blocking) two-to-three-word
            # announcement.  In that case "abort" must stop the operation,
            # not merely stop its pre-action speech.
            if interrupt_event is not None and interrupt_event.is_set():
                interrupted = (
                    f"[Interrupted] {action_type} was stopped before it ran.")
                execute_actions._last_output = interrupted
                execute_actions._results.append(
                    {"type": action_type, "ok": False,
                     "output": interrupted})
                action_outputs.append(f"{action_type}: {interrupted}")
                break
            _action_background = str(
                attrs.get("background", "")).strip().lower() in {
                    "true", "yes", "1", "on"}
            _action_ui_queue = (_ExecutionUIQueue(ui_queue)
                                if (_turn_runs_in_background()
                                    or _action_background) else ui_queue)
            _ctx = action_bus.ActionContext(
                action_type, attrs, _action_ui_queue, base_dir, origin=origin,
                turn_id=turn_id, interrupt_event=interrupt_event,
                element_map=element_map, response_text=response_text,
                previous_output=previous_output,
                legacy=execute_actions)
            # A fresh sentinel distinguishes an unwritten receipt from two
            # successful reads returning the same text.
            execute_actions._last_output = None
            from browser_contracts import execution_scope
            with execution_scope(interrupt_event):
                _flow = action_bus.BUS.dispatch(_ctx)
            if _ctx.receipt:
                execute_actions._last_output = _ctx.receipt
            if _flow == action_bus.FLOW_RECORDED:
                continue
            if _flow == action_bus.FLOW_STOP_BATCH:
                execute_actions._stop_batch = True
                break

            if action_type in DESKTOP_SETTLE_ACTIONS:
                if interrupt_event is not None:
                    interrupt_event.wait(SETTLE_DELAY_SECONDS)
                else:
                    time.sleep(SETTLE_DELAY_SECONDS)
        except Exception as e:
            execute_actions._last_output = f"[Error] {action_type} failed: {e}"
            print(f"[Action Error] {e} on {action_str}")

        current_output = str(getattr(execute_actions, '_last_output', '') or '')
        if action_type in POST_LOOP_ACTIONS and current_output == previous_output:
            # Its handler runs after this loop and records its own receipt.
            # Saying "no result" here would be reporting on work that has not
            # been attempted yet.
            continue
        if not current_output:
            # The handler never wrote a receipt — almost always a missing or
            # misspelled attribute that fell through its guard clause. Say so,
            # rather than letting the previous action's output stand in for it.
            current_output = (
                f"[Error] {action_type} produced no result. Check that its required "
                f"attributes were supplied and spelled correctly."
            )
            execute_actions._last_output = current_output

        ok = _classify_output(current_output)
        if ok:
            _surface_action_result(ui_queue, action_type, current_output, origin)
        execute_actions._results.append(
            {"type": action_type, "ok": ok, "output": current_output})
        if ok and action_type in SIDE_EFFECT_ACTIONS:
            _task_ledger[ledger_key] = current_output
        action_outputs.append(f"{action_type}: {current_output}")

    response_text = batch_response
    if interrupt_event is not None and interrupt_event.is_set():
        return re.sub(r'<[^>]+>', '', response_text).strip()

    # --- Alarm actions ---
    # Parsed by attribute rather than by position. The old pattern demanded
    # exactly time= then label= then />, so the recurrence= the catalogue
    # openly advertises made the tag match nothing at all -- and the receipt
    # said "SET_ALARM produced no result. Check that its required attributes
    # were supplied and spelled correctly", which sent the model checking
    # spelling that was already right. It retried, dropped the attribute by
    # luck, and only then worked.
    # ADD_ALARM as well as SET_ALARM. ADD_ALARM has been authorized, given
    # retrieval vocabulary ("remind reminder alarm wake nudge alert") and a
    # confirmation rule of its own, and has never had an implementation
    # anywhere -- it was not a rung on the old ladder either. A model asking
    # for it got "ADD_ALARM produced no result. Check that its required
    # attributes were supplied and spelled correctly" about an action that
    # could not have worked however it was spelled. The action bus surfaced
    # it: the old check for "has a dispatch path" was a regex that matched
    # the confirmation rule and read it as a handler.
    alarm_set = [dict(time=attrs.get('time', ''), label=attrs.get('label', 'Alarm'),
                      recurrence=attrs.get('recurrence', 'none'))
                 for name, attrs in post_ready if name in {'SET_ALARM', 'ADD_ALARM'}]
    for _alarm in alarm_set:
        alarm_time = _alarm['time']
        alarm_label = _alarm['label']
        try:
            import alarms
            res = alarms.set_alarm(alarm_time, alarm_label,
                                   recurrence=_alarm.get('recurrence', 'none'),
                                   base_dir=base_dir)
            execute_actions._last_output = res
            action_outputs.append(f"SET_ALARM: {res}")
            # Its own receipt, the way SEND_EMAIL already does. Without one the
            # turn ended with no record that the alarm existed, so the critic
            # had nothing to verify and the model was left guessing about work
            # it had in fact completed.
            _record("SET_ALARM", res)
            print(f"[Action] SET_ALARM: {res}")
            ui_queue.put(("info", f"> SET_ALARM\n\n{res}"))
        except Exception as e:
            err = f"[Error] SET_ALARM failed: {e}"
            execute_actions._last_output = err
            _record("SET_ALARM", err)
            print(f"[Action Error] SET_ALARM failed: {e}")

    if any(name == 'LIST_ALARMS' for name, attrs in post_ready):
        try:
            import alarms
            res = alarms.list_alarms(base_dir)
            execute_actions._last_output = res
            _record("LIST_ALARMS", res)
            print(f"[Action] LIST_ALARMS:\n{res}")
            ui_queue.put(("info", f"> LIST_ALARMS\n\n{res}"))
        except Exception as e:
            err = f"[Error] LIST_ALARMS failed: {e}"
            execute_actions._last_output = err
            _record("LIST_ALARMS", err)
            print(f"[Action Error] LIST_ALARMS failed: {e}")

    alarm_del = [attrs['id'] for name, attrs in post_ready
                 if name == 'DELETE_ALARM' and attrs.get('id')]
    for alarm_id in alarm_del:
        allowed, err = _action_allowed("DELETE_ALARM", origin)
        if not allowed:
            execute_actions._last_output = err
            ui_queue.put(("info", err))
            continue
        try:
            import alarms
            res = alarms.delete_alarm(alarm_id, base_dir)
            execute_actions._last_output = res
            _record('DELETE_ALARM', res)
            action_outputs.append(f'DELETE_ALARM: {res}')
            print(f"[Action] DELETE_ALARM: {res}")
            ui_queue.put(("info", f"> DELETE_ALARM\n\n{res}"))
        except Exception as e:
            err = f'[Error] DELETE_ALARM failed: {e}'
            execute_actions._last_output = err
            _record('DELETE_ALARM', err)
            action_outputs.append(f'DELETE_ALARM: {err}')
            print(f"[Action Error] DELETE_ALARM failed: {e}")

    # Two forms, because the model reaches for both and only one had a
    # handler. The attribute form fell through the entire dispatch chain and
    # came back as "produced no result ... check that its required attributes
    # were supplied and spelled correctly", which was misleading: they were
    # spelled correctly. A whole job-search document was composed and then
    # never written, twice, and the second attempt consumed a real approval.
    save_actions = [(envelope.attributes, envelope.body or
                     action_attrs.parse(envelope.attributes).get('content', ''))
                    for envelope in envelopes
                    if action_attrs.parse(envelope.attributes).get('type', '').upper()
                    in DEFERRED_ACTIONS]
    for attr_str, content in save_actions:
        allowed, err = _action_allowed("WRITE_FILE", origin)
        if not allowed:
            execute_actions._last_output = err
            ui_queue.put(("info", err))
            continue
        file_attrs = action_attrs.parse(attr_str)
        _save_type = (file_attrs.get('type') or 'WRITE_FILE').upper()
        import action_schema
        input_error = action_schema.validate(_save_type, file_attrs, has_body=bool(content.strip()))
        if input_error:
            execute_actions._last_output = input_error
            _record(_save_type, input_error)
            action_outputs.append(f'{_save_type}: {input_error}')
            continue

        # Writing a file is in CONFIRMATION_REQUIRED_ACTIONS, and this pass
        # never consulted that. The block form therefore wrote to disk with no
        # approval and no receipt at all, and once the attribute form reached
        # here too it was worse: the main scan queued an approval while this
        # pass wrote the file regardless, so the receipt said "waiting on
        # Himanshu" about a file that already existed.
        _save_type = (file_attrs.get("type") or "WRITE_FILE").upper()
        _already_approved = (
            str(file_attrs.get("approved", "")).lower() == "true"
            or str(file_attrs.get("confirmed", "")).lower() == "true")
        if _policy_confirmation_required(_save_type) and not _already_approved:
            pending = dict(file_attrs)
            pending["type"] = _save_type
            pending["body"] = content
            queued = _queue_for_approval(_save_type, pending, ui_queue, origin,
                                         turn_id)
            execute_actions._last_output = queued
            execute_actions._results.append(
                {"type": _save_type, "ok": False, "output": queued})
            action_outputs.append(f"{_save_type}: {queued}")
            continue
        announce_action(file_attrs)
        if interrupt_event is not None and interrupt_event.is_set():
            interrupted = (
                f"[Interrupted] {_save_type} was stopped before it ran.")
            execute_actions._last_output = interrupted
            execute_actions._results.append(
                {"type": _save_type, "ok": False, "output": interrupted})
            action_outputs.append(f"{_save_type}: {interrupted}")
            break
        filename = file_attrs.get("path") or file_attrs.get("filename") or ""
        confirmed = file_attrs.get("confirmed", "").strip().lower() == "true"
        try:
            if not filename.strip():
                raise ValueError("WRITE_FILE requires a path or filename")
            if not str(content or "").strip():
                raise ValueError(
                    "there is nothing to write. Supply the text either as "
                    'content="..." or between <ACTION ...> and </ACTION>')

            # An absolute path is an explicit user destination (for example,
            # D:\\form_details.txt). Relative names remain contained in VANTA's
            # data directory for backwards compatibility.
            if os.path.isabs(filename):
                save_path = os.path.abspath(filename)
            else:
                data_dir = os.path.abspath(os.path.join(base_dir, "data"))
                save_path = os.path.abspath(os.path.join(data_dir, filename))
                if os.path.commonpath((data_dir, save_path)) != data_dir:
                    raise ValueError("relative file paths must stay inside VANTA's data directory")

            if os.path.isdir(save_path):
                raise ValueError("the requested path is a directory, not a file")
            if os.path.exists(save_path) and not confirmed:
                execute_actions._last_output = (
                    f'[Confirmation Required] "{save_path}" already exists. '
                    'It was not overwritten. Reissue WRITE_FILE with confirmed="true" '
                    'only after Himanshu approves replacing it.'
                )
                ui_queue.put(("info", execute_actions._last_output))
                continue

            parent_dir = os.path.dirname(save_path)
            if parent_dir:
                os.makedirs(parent_dir, exist_ok=True)

            if save_path.lower().endswith(".html"):
                html_template = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>VANTA Insights</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;600;800&display=swap" rel="stylesheet">
<style>
    :root {{
        --bg-color: #0f172a;
        --card-bg: rgba(30, 41, 59, 0.7);
        --text-main: #f8fafc;
        --text-muted: #94a3b8;
        --accent: #3b82f6;
        --accent-glow: rgba(59, 130, 246, 0.5);
    }}
    body {{
        margin: 0;
        min-height: 100vh;
        background: linear-gradient(135deg, #020617 0%, #0f172a 100%);
        color: var(--text-main);
        font-family: 'Inter', sans-serif;
        display: flex;
        justify-content: center;
        padding: 40px 20px;
        box-sizing: border-box;
    }}
    .container {{
        max-width: 900px;
        width: 100%;
        background: var(--card-bg);
        backdrop-filter: blur(16px);
        -webkit-backdrop-filter: blur(16px);
        border: 1px solid rgba(255, 255, 255, 0.1);
        border-radius: 24px;
        padding: 40px 50px;
        box-shadow: 0 25px 50px -12px rgba(0, 0, 0, 0.5);
        animation: fadeUp 0.8s cubic-bezier(0.16, 1, 0.3, 1) forwards;
    }}
    @keyframes fadeUp {{
        0% {{ opacity: 0; transform: translateY(30px); }}
        100% {{ opacity: 1; transform: translateY(0); }}
    }}
    h1, h2, h3 {{ color: #ffffff; letter-spacing: -0.02em; margin-top: 1.5em; }}
    h1 {{ font-size: 2.5rem; font-weight: 800; text-align: center; margin-top: 0; margin-bottom: 30px; background: linear-gradient(to right, #60a5fa, #a78bfa); -webkit-background-clip: text; -webkit-text-fill-color: transparent; }}
    p {{ line-height: 1.7; color: var(--text-muted); font-size: 1.05rem; }}
    strong {{ color: #cbd5e1; }}
    ul, ol {{ line-height: 1.7; color: var(--text-muted); }}
    li {{ margin-bottom: 10px; }}
    table {{ width: 100%; border-collapse: collapse; margin-top: 20px; border-radius: 12px; overflow: hidden; }}
    th, td {{ padding: 15px; text-align: left; border-bottom: 1px solid rgba(255, 255, 255, 0.05); }}
    th {{ background: rgba(255,255,255,0.03); font-weight: 600; color: #cbd5e1; }}
    tr:hover td {{ background: rgba(255, 255, 255, 0.02); }}
</style>
</head>
<body>
<div class="container">
    {content.strip()}
</div>
</body>
</html>"""
                content = html_template

            with open(save_path, "w", encoding="utf-8") as f:
                f.write(content)
            # Read back rather than restate the request. A receipt that merely
            # repeats what was asked for cannot be wrong, and this pass
            # recorded no receipt at all -- so the critic kept failing a turn
            # for want of evidence the write had genuinely produced, and the
            # model had nothing to show it.
            written = os.path.getsize(save_path)
            saved = (f"[Success] Wrote {written} bytes to {save_path}"
                     if written else
                     f"[Error] {save_path} exists but is empty after writing.")
            execute_actions._last_output = saved
            execute_actions._results.append(
                {"type": _save_type, "ok": written > 0, "output": saved})
            action_outputs.append(f"{_save_type}: {saved}")
            print(f"[Action] Saved file to {save_path}")
            ui_queue.put(("info", f"> WROTE FILE\n\n{save_path}"))

        except Exception as e:
            execute_actions._last_output = f"Failed to write file: {e}"
            execute_actions._results.append(
                {"type": _save_type, "ok": False,
                 "output": f"[Error] Could not write {filename}: {e}"})
            action_outputs.append(f"{_save_type}: [Error] {e}")
            print(f"[Action Error] Could not save file {filename}: {e}")
            ui_queue.put(("info", f"> WROTE FILE\n\nError: {e}"))

        # --- PHASE 2: Action Log (Executed) ---
        try:
            from memory.state_db import execute_write
            import uuid
            import json
            outcome_str = str(getattr(execute_actions, '_last_output', 'Executed'))
            verified = '[Action Error]' not in outcome_str and 'Failed' not in outcome_str
            execute_write(
                """INSERT INTO action_log (id, action_type, payload, inference_source, outcome, verified_flag)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (str(uuid.uuid4()), "WRITE_FILE", json.dumps(file_attrs), origin, outcome_str, verified)
            )
        except Exception as log_e:
            print(f"[Audit Error] Failed to log action WRITE_FILE: {log_e}")

    if action_outputs:
        execute_actions._last_output = "\n".join(action_outputs)

    clean_text = re.sub(r'<[A-Za-z0-9_]+(?:\s+[^>]*)?/>', '', batch_response)
    clean_text = re.sub(r'<ACTION[^>]+>.*?</ACTION>', '', clean_text, flags=re.DOTALL)
    clean_text = re.sub(r'<think>.*?</think>', '', clean_text, flags=re.DOTALL)
    return clean_text.strip()
