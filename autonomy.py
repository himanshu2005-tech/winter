# -*- coding: utf-8 -*-
"""VANTA's policy boundary between requested and proactive work.

The model may recommend anything, but this module controls which class of action
can run without a person explicitly initiating the task.  Keep this enforcement
in Python rather than relying on instructions in an LLM prompt.
"""

import json
import os
import re
from datetime import date, datetime


PROACTIVE_ORIGIN = "proactive"
ROUTINE_ORIGIN = "routine"

# A scheduled routine is not an unsolicited inference.  Himanshu explicitly
# authored it, but that delegation still has a narrower boundary than a live
# request.  These operations can inspect a web destination without submitting,
# typing, downloading, purchasing, or communicating.  Everything else keeps
# the normal proactive restrictions below.
ROUTINE_READ_ONLY_EXCEPTIONS = frozenset({
    "BROWSER_START", "BROWSER_TABS", "BROWSER_GOTO",
    "BROWSER_SNAPSHOT", "BROWSER_READ", "BROWSER_WAIT",
})

# Initiative mode may organise private, reversible commitments (alarms, calendar
# blocks and tracked commitments) when the evidence is clear. A system event
# must never become an unsupervised message, purchase, deletion, or desktop action.
PROACTIVE_BLOCKED_ACTIONS = {
    "SEND_EMAIL", "SEND_WHATSAPP", "DELETE_FILE", "MOVE_FILE",
    "DELETE_CALENDAR_EVENT", "DELETE_ALARM",
    "COMPLETE_COMMITMENT", "ADD_LIFE_GOAL", "SET_LIFE_PREFERENCE", "WRITE_FILE",
    "SAVE_FILE", "RUN_COMMAND", "RUN_PYTHON", "OPEN_URL", "CLICK",
    "CLICK_ID", "DOUBLE_CLICK", "DOUBLE_CLICK_ID", "RIGHT_CLICK",
    "RIGHT_CLICK_ID", "TYPE", "PRESS", "HOTKEY", "MOUSE_MOVE",
    "MOUSE_DRAG", "BROWSER_GOTO", "BROWSER_CLICK", "BROWSER_TYPE",
    "BROWSER_DIALOG",
    "BEGIN_VISIBLE_SESSION", "KEEP_DATASET_PREVIEW",
    # Opening Uber with a trip filled in is a user-initiated act. VANTA may
    # say "rain from 16:30, leave earlier" on its own -- that is the useful
    # part -- but launching the app unasked is not, and OPEN_URL is blocked
    # here for the same reason.
    "BOOK_RIDE", "REAUTHORIZE_GOOGLE", "SYNC_GOOGLE_CONTACTS",
    "DRIVE_UPLOAD", "DRIVE_CREATE_FOLDER", "DRIVE_RENAME", "DRIVE_MOVE",
    "DRIVE_COPY", "DRIVE_TRASH", "DRIVE_SHARE",
    "DRIVE_RESTORE", "PEOPLE_CREATE", "PEOPLE_UPDATE", "PEOPLE_DELETE",
    "CREATE_PRESENTATION", "CREATE_DOCUMENT", "CREATE_SPREADSHEET", "CREATE_PDF", "OPEN_PATH", "DOWNLOAD_FILE", "ZIP_PATH", "UNZIP_FILE", "NOTE_ADD", "NOTE_DELETE", "PDF_MERGE", "PDF_EXTRACT",
    "GET_SELECTION", "OPEN_BRAIN_MAP", "DASHBOARD_SHOW_TEXT", "SHOW_CODE_PANEL",
    "RESULT_WINDOW",
    "WATCH_CREATE", "WATCH_PAUSE", "WATCH_RESUME", "WATCH_CANCEL", "FLIGHT_WATCH_CREATE",
    "SHUTDOWN", "RESTART", "DRAW_SCENE", "SCREEN_LIGHT", "SCREEN_LIGHT_OFF",
    "CREATE_WORKFLOW", "DEACTIVATE_WORKFLOW",
    "CREATE_ACTION", "REMOVE_ACTION", "ADD_ROUTINE", "DELETE_ROUTINE",
    "PAUSE_ROUTINE", "RESUME_ROUTINE",
    "IDE_OPEN_PROJECT", "IDE_OPEN_FILE", "IDE_SHOW_DIFF",
    "CODING_TASK_START", "CODING_TASK_CANCEL",
    "RUNTIME_APPLY", "RUNTIME_STATUS", "RUNTIME_RESUME", "RUNTIME_PRESENT", "RUN_BLENDER_SCRIPT",
}

CONFIRMATION_REQUIRED_ACTIONS = {
    # Communications leave the machine and may affect another person. Even a
    # direct request is drafted first and gets one final confirmation.
    "SEND_EMAIL", "SEND_WHATSAPP", "SEND_TELEGRAM",
    # Keep second-step confirmation for destructive/system-level operations.
    "DELETE_PROJECT_FILE",
    "DELETE_FILE", "WRITE_FILE", "SAVE_FILE",
    "DRIVE_UPLOAD", "DRIVE_TRASH", "DRIVE_SHARE", "PEOPLE_DELETE",
    "SHUTDOWN", "RESTART",
}

KNOWN_ACTIONS = {
    "READ_EMAILS","LOOKUP_CONTACTS","REAUTHORIZE_GOOGLE","CHECK_GOOGLE_CONNECTION","SYNC_GOOGLE_CONTACTS","LIST_RECENT_EVENTS","LIST_FOLLOWUPS","READ_CALENDAR","LIST_CALENDAR_EVENTS","SEARCH_VAULT",
    "SEARCH_RECALL","SEARCH_WEB","KNOWLEDGE_SEARCH","TRENDING_NEWS","GITHUB_SEARCH","GITHUB_READ_REPO","GITHUB_READ_FILE",
    "DRIVE_LIST", "DRIVE_SEARCH", "DRIVE_INFO", "DRIVE_DOWNLOAD",
    "DRIVE_READ", "DRIVE_RECENT", "DRIVE_RESTORE", "PEOPLE_SEARCH", "PEOPLE_GET",
    "CREATE_PRESENTATION", "CREATE_DOCUMENT", "CREATE_SPREADSHEET", "CREATE_PDF", "OPEN_PATH", "DOWNLOAD_FILE", "ZIP_PATH", "UNZIP_FILE", "NOTE_ADD", "NOTE_DELETE", "PDF_MERGE", "PDF_EXTRACT", "EXTRACT_TEXT", "CALCULATE", "TIME_IN", "NOTES_LIST",
    "GET_SELECTION", "OPEN_BRAIN_MAP", "DASHBOARD_SHOW_TEXT", "SHOW_CODE_PANEL",
    "RESULT_WINDOW",
    "PEOPLE_CREATE", "PEOPLE_UPDATE", "PEOPLE_DELETE",
    "DRIVE_UPLOAD", "DRIVE_CREATE_FOLDER", "DRIVE_RENAME", "DRIVE_MOVE",
    "DRIVE_COPY", "DRIVE_TRASH", "DRIVE_SHARE",
    "SET_ALARM","LIST_ALARMS","ADD_ALARM","ADD_CALENDAR_EVENT","TRACK_COMMITMENT",
    "QUIET_FOR","SHOW_ON_SCREEN","TICK_ON_SCREEN","CLEAR_SCREEN","SHOW_ROUTE",
    "SPOTIFY_PLAY","SPOTIFY_PAUSE","SPOTIFY_SKIP","NOTIFY","WAIT","OPEN_URL",
    "BROWSER_START","BROWSER_TABS","BROWSER_GOTO","BROWSER_SNAPSHOT","BROWSER_READ",
    "BROWSER_CLICK","BROWSER_TYPE","BROWSER_PRESS","BROWSER_DIALOG","BROWSER_WAIT","BROWSER_CLOSE",
    "SEND_TELEGRAM","SEND_EMAIL","SEND_WHATSAPP","RUN_COMMAND","RUN_PYTHON",
    "DELETE_FILE","MOVE_FILE","DELETE_CALENDAR_EVENT","DELETE_ALARM","WRITE_FILE",
    "SAVE_FILE","COMPLETE_COMMITMENT","ADD_LIFE_GOAL","SET_LIFE_PREFERENCE",
    "LIST_PROJECTS","OPEN_PROJECT","CREATE_PROJECT","PROJECT_TREE",
    "READ_PROJECT_FILE","WRITE_PROJECT_FILE","EDIT_PROJECT_FILE",
    "DELETE_PROJECT_FILE","SEARCH_PROJECT","FIND_PROJECT_FILES","RUN_IN_PROJECT",
    "IDE_STATUS", "IDE_OPEN_PROJECT", "IDE_OPEN_FILE", "IDE_SHOW_DIFF",
    "CODING_AGENT_STATUS", "CODING_TASK_START", "CODING_TASK_STATUS",
    "CODING_TASK_LIST", "CODING_TASK_CANCEL",
    "SET_PERSONA","ADD_DIRECTIVE","REMOVE_DIRECTIVE","LIST_DIRECTIVES",
    "CREATE_ACTION","LIST_ACTIONS","REMOVE_ACTION","READ_ACTION",
    "CREATE_WORKFLOW","LIST_WORKFLOWS","READ_WORKFLOW","DEACTIVATE_WORKFLOW",
    "DISCOVER_ACTIONS",
    "BOARD_GOTO","BOARD_PAGES","BOARD_EDIT_PAGE","BOARD_ANIMATE",
    "BOARD_PLOT3D","BOARD_MATRIX","BOARD_FIELD","BOARD_CONTOUR",
    "BOARD_CAPTURE","ANNOTATE_TEXT",
    "BOARD_MATH","BOARD_PLOT","BOARD_CHART","BOARD_TABLE","BOARD_RULE",
    "BOARD_SECTION","CONTROL_CENTER","ADD_PANEL","UPDATE_PANEL","REMOVE_PANEL",
    "LIST_PANELS","SET_PANEL_VALUE","RESEARCH_START","RESEARCH_STOP",
    "RESEARCH_STATUS",
    "BEGIN_VISIBLE_SESSION","END_VISIBLE_SESSION","KEEP_DATASET_PREVIEW",
    "PROFILE_DATASET","CREATE_DATASET_CLEANING_PREVIEW","LEARN_KNOWLEDGE","DEEP_RESEARCH",
    "LIST_COMMITMENTS","COPY_FILE","WAIT_FOR","GET_TRAVEL_TIME", "FLIGHT_SEARCH", "BOOK_RIDE", "FIND_API", "API_CATALOG_STATUS", "API_CHECK", "GEOCODE",
    "RUN_BLENDER_SCRIPT","RUNTIME_APPLY","RUNTIME_STATUS","RUNTIME_RESUME","RUNTIME_PRESENT",
    "READ_FILE","LIST_TASKS","ADD_TASK","ADD_TASKS","GET_PASSWORD",
    "REMEMBER_FACT","CORRECT_FACT","FORGET_FACT","RECALL_FACTS",
    "COMPLETE_TASK","UPDATE_TASK","LIST_TASK_LISTS",
    "REVIEW_INSIGHTS","CONFIRM_INSIGHT","REJECT_INSIGHT",
    # Native desktop control
    "CLICK_TEXT","MOVE_MOUSE","DRAG","LIST_CONTROLS","LIST_WINDOWS",
    "FOCUS_WINDOW","WINDOW_STATE","CLOSE_WINDOW","LAUNCH_APP","MEDIA","VOLUME",
    "SET_BRIGHTNESS","LOCK_SCREEN","CLIPBOARD_READ","CLIPBOARD_WRITE",
    "LIST_PROCESSES","SCREEN_LIGHT","SCREEN_LIGHT_OFF",
    "SHUTDOWN", "RESTART",
    # Google lifecycle
    "UPDATE_CALENDAR_EVENT","FIND_FREE_SLOTS","MARK_EMAIL_READ","ARCHIVE_EMAIL",
    # Tutoring, annotation and code
    "READ_DOCUMENT","LIST_DOCUMENTS","DRAW_ON_SCREEN","DRAW_SCENE","DRAW_CLEAR",
    "WRITE_CODE","RUN_SANDBOX",
    "BOARD_WRITE","BOARD_DIAGRAM","BOARD_CLEAR","BOARD_HIDE",
    # Routines, document search, notifications
    "ADD_ROUTINE","LIST_ROUTINES","DELETE_ROUTINE","PAUSE_ROUTINE","RESUME_ROUTINE",
    "SEARCH_DOCS","INDEX_FOLDER","INGEST_PATH",
    "WHY_FACT","NOTE_GAP","LIST_GAPS","FILL_GAP","IMPORT_HEALTH",
    "AUDIT_DATASET","SPLIT_DATASET",
    "FIND_FILES","LIST_DIRECTORY","GET_WEATHER","SET_TIMER","SYSTEM_STATUS","KILL_PROCESS",
    "TAKE_SCREENSHOT","READ_SCREEN_TEXT","SPOTIFY_NOW_PLAYING","SPOTIFY_SEARCH",
    "SPOTIFY_VOLUME","SPOTIFY_QUEUE",
    "YOUTUBE_SEARCH","YOUTUBE_PLAY","YOUTUBE_FULLSCREEN",
    "MUSIC_PLAY","MUSIC_PAUSE","MUSIC_NEXT","MUSIC_PREVIOUS","MUSIC_SEARCH","MUSIC_NOW_PLAYING",
    "INSPECT_POINT","LOCATE_TEXT","CLICK_SCREEN_TEXT",
    "WATCH_CREATE","WATCH_LIST","WATCH_STATUS","WATCH_PAUSE","WATCH_RESUME",
    "WATCH_CANCEL","WATCH_CHECK","WATCH_HISTORY","FLIGHT_WATCH_CREATE",
    # Handled directly in the agent loop rather than the dispatcher.
    "CAPTURE_CAMERA","ZOOM",
    # Form filling, native and web
    "SET_FIELD","TOGGLE_FIELD","SELECT_OPTION","READ_FORM","FILL_FORM",
    "BROWSER_SELECT","BROWSER_CHECK","BROWSER_UPLOAD","BROWSER_FILL_FORM","BROWSER_SUBMIT",
    "CLICK","CLICK_ID","DOUBLE_CLICK","DOUBLE_CLICK_ID","RIGHT_CLICK","RIGHT_CLICK_ID",
    "TYPE","PRESS","HOTKEY","MOUSE_MOVE","MOUSE_DRAG","SCROLL",
}


# Declared capabilities authorize themselves. CAPTURE_CAMERA and ZOOM were
# already listed above by hand; this makes the listing a consequence of the
# declaration rather than a step someone has to remember, which is the step
# that was missed for the catalogue.
try:
    import capabilities as _capabilities
    KNOWN_ACTIONS |= _capabilities.authorized_names()
    PROACTIVE_BLOCKED_ACTIONS |= _capabilities.proactive_blocked_names()
    CONFIRMATION_REQUIRED_ACTIONS |= _capabilities.confirmation_names()
except Exception as _capability_exc:  # pragma: no cover - startup diagnostic
    print(f"[Autonomy] Could not authorize declared capabilities: "
          f"{_capability_exc}")


def is_proactive(origin: str) -> bool:
    return origin == PROACTIVE_ORIGIN


def is_routine(origin: str) -> bool:
    return origin == ROUTINE_ORIGIN


def blocked_actions(origin: str) -> frozenset:
    """Code-owned action boundary for a task origin.

    The model may choose any capability it can see; this remains the final
    authority.  A routine only gains the small read-only browser exception
    above, never the ability to widen its own permissions from memory.
    """
    if is_routine(origin):
        return frozenset(PROACTIVE_BLOCKED_ACTIONS) - ROUTINE_READ_ONLY_EXCEPTIONS
    if is_proactive(origin):
        return frozenset(PROACTIVE_BLOCKED_ACTIONS)
    return frozenset()


def _self_written_actions() -> set:
    """Names of actions VANTA wrote for itself.

    Kept out of KNOWN_ACTIONS on purpose: that set is the fixed contract the
    consistency tests check against the dispatcher, and an extension has no
    entry there. Policy still has to recognise them, or a self-written action
    would be refused the moment it was created.
    """
    try:
        import os as _os
        import extensions
        return extensions.names(_os.path.dirname(_os.path.abspath(__file__)))
    except Exception:
        return set()


#: Actions that read or change anything personal: mail, calendar, tasks,
#: contacts, files, accounts, memory, the screen and the camera. Everything else
#: (weather, a timer, music, a general question) stays available to a guest.
PERSONAL_ACTION_PREFIXES = (
    "DRIVE_", "PEOPLE_", "GITHUB_", "BROWSER_", "CALENDAR", "GOOGLE_", "TASK",
    "WATCH_", "COMMITMENT", "ROUTINE", "PROJECT", "IDE_", "CODING_", "FACE",
)
# REMEMBER_FACE and FORGET_FACE are deliberately not here: teaching VANTA a face
# is how it comes to recognise anyone, so refusing it to someone it does not yet
# recognise is a lock with no key. (Before the owner is enrolled every turn is a
# guest turn, and the owner could never enrol.)
PERSONAL_ACTIONS = {
    "WINDOWS_RADIO",
    "READ_EMAILS", "SEND_EMAIL", "REPLY_EMAIL", "ARCHIVE_EMAIL", "MARK_EMAIL_READ", "READ_CALENDAR",
    "LIST_CALENDAR_EVENTS", "LIST_RECENT_EVENTS", "ADD_CALENDAR_EVENT",
    "UPDATE_CALENDAR_EVENT", "DELETE_CALENDAR_EVENT", "FIND_FREE_SLOTS",
    "ADD_TASK", "LIST_TASKS", "COMPLETE_TASK", "UPDATE_TASK", "LIST_FOLLOWUPS",
    "LOOKUP_CONTACTS", "SYNC_GOOGLE_CONTACTS", "SEND_WHATSAPP", "SEND_TELEGRAM",
    "SEND_TELEGRAM_FILE", "LIST_TELEGRAM_FILES",
    "SEARCH_VAULT", "SEARCH_RECALL", "UPDATE_CORE_MEMORY", "GET_PASSWORD",
    "LIST_COMMITMENTS", "TRACK_COMMITMENT", "COMPLETE_COMMITMENT",
    "ADD_LIFE_GOAL", "SET_LIFE_PREFERENCE", "TAKE_SCREENSHOT", "READ_SCREEN_TEXT",
    "CAPTURE_CAMERA", "CLIPBOARD_READ", "GET_SELECTION", "OPEN_BRAIN_MAP",
    "READ_FILE", "WRITE_FILE", "SAVE_FILE",
    "DELETE_FILE", "MOVE_FILE", "COPY_FILE", "REAUTHORIZE_GOOGLE",
    "CHECK_GOOGLE_CONNECTION", "SYNC_BROWSER_LOGIN", "RUN_COMMAND", "RUN_PYTHON",
    "LIST_CONTROLS", "OPEN_URL", "GET_HEALTH",
    "CREATE_PRESENTATION", "CREATE_DOCUMENT", "CREATE_SPREADSHEET", "CREATE_PDF", "EXTRACT_TEXT", "OPEN_PATH", "DOWNLOAD_FILE", "ZIP_PATH", "UNZIP_FILE", "NOTE_ADD", "NOTES_LIST", "NOTE_DELETE", "PDF_MERGE", "PDF_EXTRACT",
}


def is_personal_action(name):
    normalized = str(name or "").upper()
    return (normalized in PERSONAL_ACTIONS
            or normalized.startswith(PERSONAL_ACTION_PREFIXES))


def action_allowed(action_type: str, origin: str, memory_permits: bool = False) -> tuple[bool, str | None]:
    """Return whether an action may run for this task origin."""
    normalized = (action_type or "").upper()
    try:
        from mode_manager import get_mode_manager
        mode_allowed, mode_error = get_mode_manager().action_allowed(normalized)
        if not mode_allowed:
            return False, mode_error
    except Exception as exc:
        print(f"[Mode Policy] Could not evaluate active mode: {exc}")

    # A turn answering someone not confirmed as him may not touch anything
    # personal, whatever the model decides to attempt. The guest prompt says
    # so, but a prompt is a request; this is the enforcement.
    try:
        import turn_context
        if turn_context.is_guest() and is_personal_action(normalized):
            return False, (
                # Deliberately says nothing about how to lift it. This text
                # goes to the model, the screen and the speaker, i.e. to
                # whoever is at the desk -- the one audience the lock exists
                # against -- so a hint here is a hint for the intruder.
                f"[Privacy] {normalized} is withheld: it is not confirmed "
                "that Himanshu is the one asking, so personal data, "
                "accounts and the screen stay private. Do not suggest ways "
                "around this; say only that it is private right now.")
    except Exception as exc:
        print(f"[Privacy] Could not evaluate the guest policy: {exc}")

    self_written = normalized in _self_written_actions()
    if normalized not in KNOWN_ACTIONS and not self_written:
        return False, f"[Authorization Policy] Unknown action {normalized or '(empty)'} is blocked by default."
    if (is_proactive(origin) or is_routine(origin)) and self_written:
        return False, (
            f"[Autonomy Policy] Self-written action {normalized} is blocked for "
            "background work. It may run only from a user-initiated task."
        )
    if normalized in blocked_actions(origin):
        return False, (
            f"[Autonomy Policy] {normalized} was blocked for {origin or 'background'} work. "
            "Notify Himanshu and ask before taking that action."
        )
    return True, None


def confirmation_required(action_type: str) -> bool:
    return (action_type or "").upper() in CONFIRMATION_REQUIRED_ACTIONS


def proactive_prompt_guard() -> str:
    return """\
AUTONOMOUS EVENT POLICY:
This task was triggered by VANTA, not directly by Himanshu. You may inspect
calendar, unread mail metadata, alarms, commitments, contacts, and local memory;
compute a plan; create a concise briefing; and use NOTIFY. You may also take
initiative on private, reversible organisation: SET_ALARM, TRACK_COMMITMENT, and
ADD_CALENDAR_EVENT. Before creating one, check for an existing equivalent and
act only when the title and exact time are supported by real context. You must
NOT send a message/email, delete or move anything, make a purchase, write files,
open or control a visible application, or take any other external action. Never
manufacture approval or use a confirmation attribute as a substitute for one.
When the event passes triage, end with one concise <SAY>...</SAY> briefing. It
is spoken aloud and retained in the conversation; it does not open the
dashboard. Keep it to 2-3 short sentences unless an urgent deadline requires
a more direct alert. When
the event does not justify interrupting Himanshu, output <DONE /> with no SAY,
ASK, REPORT, or free-form prose.
Use the recent-briefings context to avoid repeating unchanged information. Report
what is new, what changed, or what you did; if nothing has changed, remain quiet.
"""


def proactive_plan_requests_silence(plan) -> bool:
    """Return the planner's non-interruption decision as an enforceable result.

    The planner and executor are separate model calls.  Previously the planner
    could correctly decide "remain quiet", only for the executor to improvise a
    spoken status update.  A model decision about *whether* to interrupt must
    survive that boundary just like an action approval decision does.
    """
    if plan is None or getattr(plan, "question", None):
        return False
    if list(getattr(plan, "steps", None) or []):
        return False
    # The goal is usually the original event text. It may legitimately ask
    # for background work to happen "silently" while still requesting a
    # briefing afterwards. Only the planner's decision fields are authority
    # for whether the event itself should be suppressed.
    fields = [
        getattr(plan, "reasoning", ""),
        getattr(plan, "deliverable", ""),
        *(getattr(plan, "success_criteria", None) or []),
        *(getattr(plan, "stop_conditions", None) or []),
    ]
    decision = " ".join(str(value or "") for value in fields).casefold()
    return bool(re.search(
        r"\b(?:remain|stay|keep)\s+(?:completely\s+)?quiet\b|"
        r"\bsilent(?:ly|\s+triage|\s+handling)?\b|"
        r"\bno\s+(?:spoken\s+)?(?:output|briefing|interruption|notification|say)\b|"
        r"\bdo\s+not\s+(?:speak|interrupt|notify)\b",
        decision,
    ))


def routine_prompt_guard() -> str:
    return """\
SCHEDULED ROUTINE POLICY:
This task is a standing instruction Himanshu explicitly created. Carry out its
read-only investigation and report only verified changes. You may open VANTA's
managed browser, navigate to the URL named by the routine, wait for it, and
read its current snapshot. Navigation is not completion: verify the resulting
URL and page state before reporting. You must not type into forms, click a
submission or purchase control, send communications, modify files or accounts,
or broaden the routine beyond its stored instruction. If authentication or a
human challenge blocks access, state that exact blocker once. If nothing has
changed, remain quiet.
"""


def proactive_planning_rules() -> str:
    """Rules supplied before the planner chooses whether an alert needs attention."""
    return """\
--- PROACTIVE TRIAGE ---
You are the decision-maker for incoming alerts. Rank them by consequence, time
sensitivity, evidence, and reversibility. Do not ask Himanshu to choose between
alerts that you can independently assess. Handle the highest-consequence alert
first; collapse routine/job/social notifications into the background unless they
create a real deadline or opportunity that needs action now. For a security or
account-integrity alert, briefly state the risk and the safest next step. Ask a
question only when a missing fact genuinely changes a high-consequence action.
When a scheduled routine names durable watch IDs, inspect and run those watches
with WATCH_STATUS, WATCH_CHECK, and WATCH_HISTORY. Their adapters are the
authoritative evidence path. Do not replace them with SEARCH_WEB, recreate them,
or require visible browser control. A routine expiry is enforced by the routine
scheduler itself; LIST_ROUTINES is enough to inspect it.
"""


def _state_path(base_dir: str) -> str:
    return os.path.join(base_dir, "vault", "system", "proactive_state.json")


def should_run_daily_plan(base_dir: str) -> bool:
    """One automatic day-plan prompt per local calendar day."""
    path = _state_path(base_dir)
    today = date.today().isoformat()
    try:
        with open(path, "r", encoding="utf-8") as f:
            state = json.load(f)
    except Exception:
        state = {}
    return state.get("daily_plan_date") != today


def daily_plan_due(base_dir: str, now=None) -> bool:
    """Whether the daily planning review should be queued now.

    It is intentionally independent from the six-hour preference-question
    timer.  A missed morning startup may still receive a useful remaining-day
    review, but evening rest is not interrupted by a stale plan.
    """
    moment = now or datetime.now()
    return 6 <= moment.hour < 20 and should_run_daily_plan(base_dir)


def mark_daily_plan_run(base_dir: str) -> None:
    path = _state_path(base_dir)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    try:
        with open(path, "r", encoding="utf-8") as f:
            state = json.load(f)
        if not isinstance(state, dict):
            state = {}
    except Exception:
        state = {}
    state["daily_plan_date"] = date.today().isoformat()
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f)


def _briefing_path(base_dir: str) -> str:
    return os.path.join(base_dir, "vault", "system", "briefing_ledger.json")


def recent_briefings(base_dir: str, limit: int = 6) -> str:
    """Compact history that lets proactive reasoning suppress stale repetition."""
    try:
        with open(_briefing_path(base_dir), "r", encoding="utf-8") as f:
            items = json.load(f)
        items = items[-limit:]
    except Exception:
        items = []
    if not items:
        return ""
    # The old header read as "these are established facts", and the raw ISO
    # timestamp gave the model nothing to judge staleness by. These are things
    # VANTA said, at a time, not things that are currently true.
    lines = ["--- WHAT YOU SAID IN RECENT BRIEFINGS ---",
             "These are your own past statements, each true only as of when it "
             "was said. Do not repeat information that has not changed, and do "
             "not treat a claim here as current: check it against the live "
             "context above, which is authoritative where the two disagree."]
    for item in items:
        stamp = item.get("time", "")
        try:
            import when
            moment = when.parse(stamp)
            said = when.describe(stamp) if moment else stamp
        except Exception:
            said = stamp
        lines.append(f"- (said {said}) {item.get('text', '')}")
    return "\n".join(lines)


def record_briefing(base_dir: str, text: str) -> None:
    cleaned = " ".join((text or "").split())[:500]
    if not cleaned:
        return
    # A briefing is replayed into every later proactive turn, so a relative
    # day word in it outlives the day it was true. "The reminder is set for
    # 7:30 AM tomorrow", said on the 21st, was still being repeated on the
    # 22nd -- and each repetition was saved and fed back, so the error
    # reinforced itself until the one correct briefing was outnumbered.
    try:
        import when
        cleaned = when.anchor(cleaned)
    except Exception:
        pass
    path = _briefing_path(base_dir)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    try:
        with open(path, "r", encoding="utf-8") as f:
            items = json.load(f)
    except Exception:
        items = []
    if items and items[-1].get("text") == cleaned:
        return
    items.append({"time": datetime.now().isoformat(timespec="seconds"), "text": cleaned})
    with open(path, "w", encoding="utf-8") as f:
        json.dump(items[-30:], f, indent=2, ensure_ascii=False)
