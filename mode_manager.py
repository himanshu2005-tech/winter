"""Single-active-mode runtime policy for VANTA.

Modes are operational state, not memory-based permissions.  Neo4j stores one
singleton RuntimeMode plus immutable transition audit nodes.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
import os
import re
import time
import threading
import uuid


class WinterMode(str, Enum):
    """Six distinct operating policies; exactly one is active."""

    ASSISTANT = "assistant"
    STUDY = "study"
    RESEARCH = "research"
    BUILD = "build"
    HEALTH = "health"
    SLEEP = "sleep"


@dataclass(frozen=True)
class ModePolicy:
    mode: WinterMode
    label: str
    prompt: str
    acknowledgement: str
    blocked_actions: frozenset[str] = frozenset()
    notification_level: str = "normal"
    proactive_enabled: bool = True
    speech_speed: float = 1.0
    dashboard_dimmed: bool = False
    # A hard block list says what is forbidden; it never says what to reach for
    # first. Without this the agent behaves identically in every mode that does
    # not happen to block a tool, which is most of them.
    tool_policy: str = ""
    # Catalogs always injected for this mode, so the right tools are in front of
    # the model even on a direct turn where no planner runs to request them.
    catalogs: frozenset[str] = frozenset()
    # The model this mode wants, when the general-purpose one is the wrong tool.
    # Build mode wants a coding model; research wants a long-context one. Empty
    # means "whatever WINTER_MODEL says". Only applied on the preferred
    # provider, because a model id is not portable between providers.
    model_env: str = ""
    model_default: str = ""


@dataclass(frozen=True)
class ModeTurnDecision:
    """A deterministic response that bypasses retrieval and the LLM planner."""
    response: str
    speak: bool = True
    reason: str = "mode_guard"


_NETWORK_ACTIONS = frozenset({
    "READ_EMAILS", "SEND_EMAIL", "SEARCH_WEB", "DEEP_RESEARCH",
    "GITHUB_SEARCH", "GITHUB_READ_REPO", "GITHUB_READ_FILE",
    "READ_CALENDAR", "LIST_CALENDAR_EVENTS", "ADD_CALENDAR_EVENT",
    "DELETE_CALENDAR_EVENT", "GET_TRAVEL_TIME", "GEOCODE",
    "SEND_TELEGRAM", "SEND_WHATSAPP", "BROWSER_START", "BROWSER_TABS",
    "BROWSER_GOTO", "BROWSER_SNAPSHOT", "BROWSER_READ", "BROWSER_CLICK",
    "BROWSER_TYPE", "BROWSER_PRESS", "BROWSER_WAIT", "BROWSER_CLOSE",
})
# Native desktop control. Any mode that suppresses machine activity has to
# block all of these, not just the handful that existed when it was written.
_DESKTOP_ACTIONS = frozenset({
    "CLICK", "CLICK_ID", "CLICK_TEXT", "DOUBLE_CLICK", "DOUBLE_CLICK_ID",
    "RIGHT_CLICK", "RIGHT_CLICK_ID", "TYPE", "PRESS", "HOTKEY", "MOUSE_MOVE",
    "MOVE_MOUSE", "MOUSE_DRAG", "DRAG", "SCROLL", "FOCUS_WINDOW",
    "WINDOW_STATE", "CLOSE_WINDOW", "LAUNCH_APP", "BEGIN_VISIBLE_SESSION",
    "KILL_PROCESS", "RUNTIME_RESUME",
    "SET_FIELD", "TOGGLE_FIELD", "SELECT_OPTION", "FILL_FORM",
})
# Anything that reads what is on the machine or on screen.
_OBSERVATION_ACTIONS = frozenset({
    "SEARCH_RECALL", "CAPTURE_CAMERA", "LIST_WINDOWS", "LIST_CONTROLS",
    "LIST_PROCESSES", "CLIPBOARD_READ", "DRAW_ON_SCREEN", "READ_DOCUMENT",
    "RESULT_WINDOW",
    "TAKE_SCREENSHOT", "READ_SCREEN_TEXT", "SYSTEM_STATUS",
})
_CODE_ACTIONS = frozenset({
    "RUN_COMMAND", "RUN_PYTHON", "RUN_BLENDER_SCRIPT", "RUN_SANDBOX",
    "RUNTIME_APPLY", "RUNTIME_STATUS", "RUNTIME_RESUME",
    "PROFILE_DATASET", "CREATE_DATASET_CLEANING_PREVIEW", "KEEP_DATASET_PREVIEW",
    "ML_BUILD", "ML_BUILD_CANCEL",
})
_FILE_WRITE_ACTIONS = frozenset({
    "WRITE_FILE", "SAVE_FILE", "WRITE_CODE", "COPY_FILE", "MOVE_FILE", "DELETE_FILE",
    "RUNTIME_APPLY", "CREATE_PRESENTATION", "CREATE_DOCUMENT", "CREATE_SPREADSHEET",
    "CREATE_PDF", "DOWNLOAD_FILE", "ZIP_PATH", "UNZIP_FILE", "PDF_MERGE", "PDF_EXTRACT",
    "OPEN_PATH",
})

# Sleep is a deliberate quiet-state declaration. It survives restarts until
# Himanshu speaks, types, or explicitly changes mode.
STICKY_MODES = frozenset({WinterMode.SLEEP})

# Every other mode describes a situation. After this long with no change, the
# situation is over and the mode is just stale state.
MODE_STALE_HOURS = float(os.getenv("WINTER_MODE_STALE_HOURS", "6"))

POLICIES = {
    WinterMode.ASSISTANT: ModePolicy(WinterMode.ASSISTANT, "ASSISTANT", """
ASSISTANT MODE: Be a capable, warm general-purpose copilot. Plan, execute verified
actions, and take bounded initiative. Ask only questions that materially change
the result; prefer sensible assumptions for reversible work. In ordinary
conversation, be relaxed and socially present: match Himanshu's energy and use
occasional context-aware wit without turning every response into a performance.
""".strip(), "Assistant mode active.",
        tool_policy=(
            "PREFER: whatever directly settles the request, then stop. "
            "Read before you write; verify with a receipt before you claim. "
            "AVOID: opening apps, browsing, or researching when the answer is "
            "already in memory or on screen."
        ), catalogs=frozenset({"WORKSPACE", "MEMORY"})),
    WinterMode.STUDY: ModePolicy(WinterMode.STUDY, "STUDY", """
STUDY MODE: Protect concentration. Teach from the active screen or supplied
material when asked, connect explanations to what is visible, and maintain
continuity across questions. Do not invent a study plan when Himanshu says to
monitor quietly. Hold routine mail and notifications for one catch-up when the
mode ends; alarms and genuinely critical alerts may still interrupt. Keep the
tone encouraging and human; a brief playful analogy is useful when it improves
understanding, but banter must never break focus.
""".strip(), "Study mode active. I will stay quiet and help from what is in front of you.",
        notification_level="critical", proactive_enabled=False,
        tool_policy=(
            "PREFER: active-window evidence, TUTOR rich reports, worked examples, "
            "short checks for understanding, and the exact study material. "
            "AVOID: unrelated project scans, generic web research, unsolicited "
            "plans, and routine notifications."
        ), catalogs=frozenset({"TUTOR", "MEMORY", "WORKSPACE"})),
    WinterMode.RESEARCH: ModePolicy(WinterMode.RESEARCH, "RESEARCH", """
RESEARCH MODE: Build durable, source-grounded knowledge on the requested topic.
Use Tavily and primary sources where possible, preserve links and provenance,
extract relationships into the graph, track open questions, and distinguish
evidence from inference. Continue through checkpointed rounds until stopped,
the evidence plateaus, or the session passes its graph-grounded examination.
Sound engaged rather than academic: make the interesting implication clear,
while keeping jokes away from uncertain or consequential claims.
""".strip(), "Research mode active. I will build and verify the knowledge graph.",
        notification_level="important", proactive_enabled=False,
        tool_policy=(
            "PREFER: DEEP_RESEARCH, SEARCH_WEB, authoritative sources, WHY_FACT, "
            "and graph-backed synthesis. A search snippet is a lead, not a fact. "
            "AVOID: unrelated browsing, unsupported conclusions, and visible "
            "desktop control."
        ), catalogs=frozenset({"MEMORY", "BROWSER", "WORKSPACE"})),
    WinterMode.BUILD: ModePolicy(WinterMode.BUILD, "BUILD", """
BUILD MODE: Optimize for completing the current project. Track the deliverable,
errors, decisions, verification, and the next concrete step. Resist feature
expansion unless it is necessary for the stated outcome.

Keep the collaboration lively without narrating every keystroke. Celebrate a
real passing test or elegant fix; when something breaks, name the failure
plainly and fix it before reaching for humour.

Use the measured BUILD PROFILE and structural map before choosing a workflow.
For a large codebase, navigate by symbols and dependencies instead of reading
whole files. Use AST evidence where the profile says an AST exists; never
pretend JavaScript or another unparsed language has been AST-mapped. React web
and React Native are different artifacts and must use their own package scripts.
Blender is a persistent executable artifact: inspect its runtime, apply one
bounded revision, and trust only its process/artifact/scene receipt. A timeout
is not success and a crashed revision must not be repeated unchanged.

For a requested machine-learning or neural-network project, use ML_BUILD so
Kaggle discovery, bounded download, profiling, cleaning, training, evaluation
and artifacts form one checkpointed background job. Do not expose credentials,
overwrite raw data, or claim training succeeded before metrics and a model file exist.
""".strip(), "Build mode active. I'll keep us moving toward the deliverable.",
        tool_policy=(
            "PREFER: project structural context before reading, READ_FILE before any edit, "
            "WRITE_CODE for new scripts, RUN_SANDBOX to check a snippet, RUN_COMMAND or "
            "RUN_IN_PROJECT for declared build/test commands, GITHUB_* for reference. "
            "Reproduce a failure before fixing it. "
            "Use RUNTIME_* rather than shell-driving Blender. Keep visual coding surfaces "
            "closed unless Himanshu explicitly asks to see code. AVOID: rewriting files "
            "you have not read, guessed commands, and unrelated research."
        ), catalogs=frozenset({"CODE", "DATA", "MEMORY", "PROJECT"}),
        model_env="WINTER_MODEL_CODER", model_default="alibaba/qwen3-coder"),
    WinterMode.HEALTH: ModePolicy(WinterMode.HEALTH, "HEALTH", """
HEALTH MODE: Be calm, conservative, and evidence-led. Use imported health data
and current symptoms only when relevant, separate observation from inference,
and prioritize immediate safety. Do not diagnose or overwhelm Himanshu with
questions; ask only what changes the safe next step.
""".strip(), "Health mode active. I will keep this calm and practical.",
        notification_level="important", proactive_enabled=False,
        speech_speed=0.92,
        tool_policy=(
            "PREFER: sensitive health facts only when directly relevant, simple "
            "support, trends with dates, and escalation for urgent red flags. "
            "AVOID: diagnosis, certainty from sparse data, and unrelated tasks."
        ), catalogs=frozenset({"MEMORY", "WORKSPACE"})),
    WinterMode.SLEEP: ModePolicy(WinterMode.SLEEP, "SLEEP", """
SLEEP MODE: Do not converse, ask questions, plan, browse, or create tasks.
Continue collecting new mail metadata and notifications into the durable sleep
inbox. Speak only for alarms or genuinely critical safety events. Any real
utterance or typed user message ends Sleep mode; deliver one concise catch-up
with every new email sender and subject, mirror the full digest on the
dashboard, then handle what Himanshu said.
""".strip(), "Sleep mode active. I will stay quiet and keep everything for when you wake.",
        blocked_actions=(_DESKTOP_ACTIONS | _FILE_WRITE_ACTIONS | _CODE_ACTIONS),
        notification_level="critical", proactive_enabled=False,
        speech_speed=0.88, dashboard_dimmed=True,
        tool_policy=(
            "PREFER: no action. Record routine events silently. "
            "AVOID: questions, browsing, desktop changes, research, and filler."
        ), catalogs=frozenset({"WORKSPACE"})),
}


MODE_SPEECH_LIMITS = {
    WinterMode.ASSISTANT: 45,
    WinterMode.STUDY: 70,
    WinterMode.RESEARCH: 45,
    WinterMode.BUILD: 50,
    WinterMode.HEALTH: 55,
    WinterMode.SLEEP: 30,
}


def speech_word_limit(mode=None):
    selected = WinterMode(mode) if mode is not None else current_mode()
    return MODE_SPEECH_LIMITS[selected]

MODE_QUALITY_CONTRACTS = {
    WinterMode.ASSISTANT: "Prioritize the current outcome, relevant context, minimal interruption, and verified completion. Keep routine answers concise; expand only when the decision benefits.",
    WinterMode.STUDY: "Protect focus, ground teaching in the current material, preserve lesson continuity, explain with worked structure, and remain quiet unless asked or something is critical.",
    WinterMode.RESEARCH: "Build durable source-grounded knowledge with provenance, open questions, contradiction handling, checkpointed progress, and explicit uncertainty.",
    WinterMode.BUILD: "Anchor on a concrete artifact and acceptance test. Inspect before editing, preserve existing work, isolate failures, use reversible changes, test proportionally, and report the exact remaining risk.",
    WinterMode.HEALTH: "Prioritize safety, calm language, relevant sensitive evidence, uncertainty, minimal questioning, and a clear next step without pretending to diagnose.",
    WinterMode.SLEEP: "Preserve sleep: remain silent except for alarms or critical safety events, retain routine items durably, and provide one non-duplicated wake digest.",
}


def quality_contract(mode=None):
    selected = WinterMode(mode) if mode is not None else current_mode()
    return MODE_QUALITY_CONTRACTS[selected]

_ALIASES = {
    "normal": WinterMode.ASSISTANT, "default": WinterMode.ASSISTANT,
    "chat": WinterMode.ASSISTANT, "general": WinterMode.ASSISTANT,
    "coding": WinterMode.BUILD, "code": WinterMode.BUILD,
    "coder": WinterMode.BUILD, "dev": WinterMode.BUILD,
    "developer": WinterMode.BUILD, "programming": WinterMode.BUILD,
    "project": WinterMode.BUILD, "work": WinterMode.BUILD,
    "focus": WinterMode.STUDY, "learning": WinterMode.STUDY,
    "learn": WinterMode.STUDY,
    "investigate": WinterMode.RESEARCH,
    "wellness": WinterMode.HEALTH, "unwell": WinterMode.HEALTH,
    "bed": WinterMode.SLEEP, "night": WinterMode.SLEEP,
}


# --------------------------------------------------------------------- #
# A proposed mode change, waiting for an answer
# --------------------------------------------------------------------- #
# VANTA may suggest a mode but not take it. The suggestion is held here
# until Himanshu answers, and forgotten shortly after so that a "yes" to
# something else entirely is never read as consent to this.
PROPOSAL_TTL_SECONDS = 180.0
_proposal = {"mode": None, "at": 0.0, "reason": ""}

_AFFIRMATIVE = {
    "yes", "yeah", "yep", "yup", "sure", "ok", "okay", "go ahead", "do it",
    "please do", "go for it", "sounds good", "fine", "alright", "y",
    "yes please", "switch", "do", "affirmative",
}
_NEGATIVE = {
    "no", "nope", "nah", "don't", "dont", "no thanks", "not now", "later",
    "stay", "keep it", "negative", "n", "no thank you",
}


def propose_mode(target, reason=""):
    """Record that VANTA would like to switch, pending his answer."""
    try:
        mode = WinterMode(target)
    except ValueError:
        return None
    _proposal.update({"mode": mode, "at": time.time(), "reason": reason})
    return mode


def pending_mode_proposal():
    """The mode awaiting an answer, or None once it has gone stale."""
    mode = _proposal.get("mode")
    if not mode:
        return None
    if time.time() - _proposal.get("at", 0.0) > PROPOSAL_TTL_SECONDS:
        clear_mode_proposal()
        return None
    return mode


def clear_mode_proposal():
    _proposal.update({"mode": None, "at": 0.0, "reason": ""})


def answer_to_proposal(text):
    """Read a reply as an answer to the pending proposal.

    Returns ("accept", mode), ("decline", mode), or None when there is no
    proposal outstanding or the reply is about something else. Only a short
    reply counts: "yes" is an answer, a paragraph that happens to start with
    "sure" is a new instruction.
    """
    mode = pending_mode_proposal()
    if not mode:
        return None
    cleaned = " ".join(str(text or "").lower().strip().rstrip(".!?").split())
    if not cleaned or len(cleaned.split()) > 4:
        return None
    if cleaned in _AFFIRMATIVE:
        clear_mode_proposal()
        return ("accept", mode)
    if cleaned in _NEGATIVE:
        clear_mode_proposal()
        return ("decline", mode)
    return None


def parse_mode_command(text: str):
    """Parse only explicit mode commands; natural intent is LLM-routed."""
    cleaned = " ".join(str(text or "").lower().strip().rstrip(".!?").split())
    direct_states = {
        "sleep": WinterMode.SLEEP,
        "go to sleep": WinterMode.SLEEP,
        "sleep mode": WinterMode.SLEEP,
        "activate sleep mode": WinterMode.SLEEP,
        "i want to sleep": WinterMode.SLEEP,
        "i am going to sleep": WinterMode.SLEEP,
        "i'm going to sleep": WinterMode.SLEEP,
        "let me sleep": WinterMode.SLEEP,
        "going to bed": WinterMode.SLEEP,
        "study time": WinterMode.STUDY,
        "time to study": WinterMode.STUDY,
        "let's study": WinterMode.STUDY,
        "lets study": WinterMode.STUDY,
    }
    if cleaned in direct_states:
        return ("set", direct_states[cleaned])
    if cleaned in {"what mode", "what mode are you in", "current mode", "which mode"}:
        return ("status", None)
    if cleaned in {"exit mode", "leave mode", "back to normal", "normal mode", "assistant mode"}:
        return ("set", WinterMode.ASSISTANT)
    match = re.fullmatch(r"(?:enter|start|switch to|activate|use)?\s*([a-z]+)\s+mode", cleaned)
    if not match:
        return None
    name = match.group(1)
    try:
        return ("set", WinterMode(name))
    except ValueError:
        aliased = _ALIASES.get(name)
        return ("set", aliased) if aliased else None


@dataclass(frozen=True)
class SemanticModeDecision:
    decision: str
    mode: WinterMode
    confidence: float
    reply: str = ""
    reason: str = ""


def infer_mode_intent(text: str, current_mode, call_llm_fn):
    """Compatibility adapter over the shared typed cognitive router."""
    from cognitive_pipeline import route_turn
    current = WinterMode(current_mode)
    route = route_turn(text, current.value, call_llm_fn)
    decision = "switch" if route.should_switch_mode else ("quiet" if route.should_be_quiet else "keep")
    return SemanticModeDecision(
        decision=decision,
        mode=WinterMode(route.target_mode),
        confidence=route.confidence,
        reply=route.acknowledgement,
        reason=route.rationale,
    )


class ModeManager:
    def __init__(self, graph_store=None, initial=WinterMode.ASSISTANT):
        self._store = graph_store
        self._active = WinterMode(initial)
        self._loaded = False
        self._lock = threading.RLock()
        self._observers = []

    def _graph(self):
        if self._store is not None:
            return self._store
        from memory.neo4j_store import store
        self._store = store
        return self._store

    def active_mode(self) -> WinterMode:
        with self._lock:
            if not self._loaded:
                try:
                    rows = self._graph().query(
                        "MATCH (m:RuntimeMode {id:'vanta'}) RETURN m.active_mode AS mode, "
                        "m.updated_at AS updated, m.source AS source")
                    if rows and rows[0].get("mode"):
                        stored = WinterMode(rows[0]["mode"])
                        self._active = self._unexpired(
                            stored, rows[0].get("updated"), rows[0].get("source"))
                except Exception as exc:
                    print(f"[Mode] Could not load persisted mode; using assistant: {exc}")
                    self._active = WinterMode.ASSISTANT
                self._loaded = True
            return self._active

    def _unexpired(self, stored, updated_at, source=None):
        """A mode should not outlive the reason it was entered.

        Three cases, and the distinction is what Himanshu actually did:

        Sleep was turned on deliberately to withhold ordinary interruptions. A
        restart must preserve that state until the user returns.

        A mode he *asked* for is a declaration, and survives until it goes
        stale.

        A mode VANTA *inferred* from the shape of a task is a guess about what
        he was doing at the time. Booting hours later still in health mode --
        restrictive tools, proactivity disabled -- because of a question about a
        health export is the guess outliving its usefulness.
        """
        if stored in STICKY_MODES or stored == WinterMode.ASSISTANT:
            return stored

        inferred = str(source or "") == "semantic_route"
        if inferred:
            print(f"[Mode] {stored.value} was inferred, not requested; "
                  "starting in assistant.")
            self._clear_stale()
            return WinterMode.ASSISTANT

        try:
            when = datetime.fromisoformat(str(updated_at))
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
            age_hours = (datetime.now(timezone.utc) - when).total_seconds() / 3600
        except Exception:
            return stored
        if age_hours < MODE_STALE_HOURS:
            return stored
        print(f"[Mode] {stored.value} was set {age_hours:.0f}h ago; "
              "returning to assistant.")
        self._clear_stale()
        return WinterMode.ASSISTANT

    def _clear_stale(self):
        try:
            self._graph().query(
                "MATCH (m:RuntimeMode {id:'vanta'}) SET m.active_mode='assistant',"
                "m.updated_at=$now, m.source='stale_expiry'",
                now=datetime.now(timezone.utc).isoformat())
        except Exception as exc:
            print(f"[Mode] Could not clear the stale mode: {exc}")

    def policy(self) -> ModePolicy:
        return POLICIES[self.active_mode()]

    def add_observer(self, callback):
        """Register something to run whenever the mode actually changes.

        There are three separate call sites that switch modes -- an explicit
        command, the semantic router, and startup restore -- so hooking them
        individually guarantees one gets missed. Observers hang off the single
        place the change is real.
        """
        self._observers.append(callback)

    def _notify(self, previous, active):
        for callback in list(self._observers):
            try:
                callback(previous, active)
            except Exception as exc:
                print(f"[Mode] Observer failed: {exc}")

    def switch(self, target, source="user", reason="explicit request"):
        target = WinterMode(target)
        with self._lock:
            previous = self.active_mode()
            if previous == target:
                return {"changed": False, "previous": previous.value, "active": target.value, "policy": POLICIES[target]}
            transition_id = f"mode_{uuid.uuid4().hex}"
            now = datetime.now(timezone.utc).isoformat()
            try:
                self._graph().query("""
                    MERGE (m:RuntimeMode {id:'vanta'})
                    ON CREATE SET m.active_mode='assistant',m.created_at=$now
                    WITH m,m.active_mode AS previous
                    SET m.active_mode=$target,m.updated_at=$now,m.source=$source
                    CREATE (t:ModeTransition {id:$transition_id,previous_mode:previous,
                        active_mode:$target,source:$source,reason:$reason,created_at:$now})
                    RETURN previous,m.active_mode AS active
                """, target=target.value, source=source, reason=reason,
                     transition_id=transition_id, now=now)
            except Exception as exc:
                # Runtime switching remains available if Neo4j is temporarily down;
                # persistence failure is explicit and never changes permissions upward.
                print(f"[Mode] Persistence warning: {exc}")
            self._active = target
            self._loaded = True
        # Deliberately outside the lock: an observer that starts a long-running
        # worker must not be able to deadlock the next mode change.
        self._notify(previous, target)
        return {"changed": True, "previous": previous.value, "active": target.value, "policy": POLICIES[target]}

    def action_allowed(self, action_type: str):
        normalized = str(action_type or "").upper()
        policy = self.policy()
        if normalized in policy.blocked_actions:
            return False, f"[Mode Policy] {normalized} is disabled while {policy.label} mode is active."
        # A declared capability names the modes it may run in, so the mode it
        # must not run in follows from the declaration. The hand-built groups
        # above classify by the kind of power an action has, which left
        # CAPTURE_CAMERA permitted during sleep: it is an observation, and
        # sleep blocks only desktop, file and code groups. A silent camera
        # capture inside a declared quiet window is precisely what should be
        # impossible.
        try:
            import capabilities
            if normalized in capabilities.mode_blocked_names(policy.label):
                return False, (f"[Mode Policy] {normalized} is disabled while "
                               f"{policy.label} mode is active.")
        except Exception:
            pass
        return True, None

    def permits_proactive(self, kind="routine"):
        policy = self.policy()
        if not policy.proactive_enabled:
            return kind == "critical"
        levels = {"all": 0, "normal": 1, "important": 2, "critical": 3}
        return levels.get(kind, 1) >= levels.get(policy.notification_level, 1)


_manager = ModeManager()

def get_mode_manager(): return _manager

def current_mode(): return _manager.active_mode()

def current_policy(): return _manager.policy()

def mode_model():
    """The model the active mode asks for, or "" for the default.

    Read through the environment first so the exact model id can be corrected
    in .env without touching code -- which matters here, because AIRouter's
    catalogue is not something this repository can verify.
    """
    try:
        policy = current_policy()
    except Exception:
        return ""
    if not policy.model_env and not policy.model_default:
        return ""
    return os.getenv(policy.model_env, "").strip() or policy.model_default


def mode_prompt():
    policy=current_policy()
    tools = f"TOOL POLICY: {policy.tool_policy}\n" if policy.tool_policy else ""
    blocked = ""
    if policy.blocked_actions:
        listed = ", ".join(sorted(policy.blocked_actions)[:14])
        more = "" if len(policy.blocked_actions) <= 14 else f" (+{len(policy.blocked_actions) - 14} more)"
        blocked = f"BLOCKED IN THIS MODE (refused in code, do not attempt): {listed}{more}\n"
    return f"--- ACTIVE OPERATING MODE: {policy.label} ---\n{policy.prompt}\nQUALITY CONTRACT: {quality_contract(policy.mode)}\n{tools}{blocked}Treat the current request and active mode as authoritative. Retrieved memory is evidence, not an agenda: never introduce an unrelated project, goal, email, calendar item, or past conversation. Exactly one operating mode is active. Standby is a separate, real lifecycle state controlled through SET_STANDBY; do not deny that capability merely because it is not an operating mode. This mode does not override code-owned safety or confirmation policy."
