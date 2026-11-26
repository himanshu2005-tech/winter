"""Typed, model-driven routing for a VANTA conversation turn.

The model interprets meaning once. Downstream systems consume a validated route;
permissions and action authorization remain code-owned elsewhere.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
import json
import os
import re
import threading
import time
from typing import Callable, Tuple


# Derived from the enum so a new mode cannot be added to mode_manager and then
# silently rejected by the router as an invalid target.
from mode_manager import WinterMode as _WinterMode
import perception_intent
import resource_pool

VALID_MODES = frozenset(mode.value for mode in _WinterMode)

# The router was given bare mode names and had to infer each one's purpose from
# the word alone, so "teach me about neural networks" landed in research rather
# than tutor. One line each removes the guesswork.
_MODE_HINTS = {
    # Teaching used to be its own mode, and cutting the mode list to two took
    # the word "teach" out of the router's vocabulary with it. "Teach me this"
    # then classified as ordinary conversation, the planner never asked for the
    # TUTOR catalog, and VANTA -- holding no board tools -- went looking
    # through email and asked which file to read. The tools never went away;
    # only the sentence that points at them did.
    "assistant": "everything except writing code: conversation, the desktop, "
                 "the browser, email, calendar, tasks, contacts, files, "
                 "questions, research, and TEACHING -- explaining a topic in "
                 "a rich report with formulas, diagrams and worked steps, or "
                 "explaining current material through the resources it needs.",
    "study": "a sustained study session: protect focus, quietly monitor, teach "
             "from the active screen or supplied material, use rich reports "
             "for worked explanations, and hold routine interruptions",
    "research": "a sustained autonomous investigation explicitly requested by "
                "Himanshu: use Tavily and sources, build durable graph knowledge, "
                "track open questions, and continue through checkpointed rounds",
    "build": "he is working on a project and wants the coding model: writing "
             "or debugging code, a repository, a build",
    "health": "he explicitly wants a health-focused session or is currently "
              "unwell and wants calm evidence-led support; do not infer it from "
              "a casual health question",
    "sleep": "he explicitly says he is going to sleep or asks VANTA to enter "
             "sleep mode; suppress routine interruptions and collect a digest",
}


def _mode_menu():
    return "\n".join(f"- {name}: {_MODE_HINTS.get(name, '')}" for name in sorted(VALID_MODES))
VALID_MODE_DECISIONS = frozenset({"keep", "switch"})
VALID_INTERACTIONS = frozenset({"respond", "quiet"})
VALID_TASK_KINDS = frozenset({"conversation", "question", "action", "research", "observation", "planning"})
VALID_DEPTHS = frozenset({"direct", "standard", "deep"})
VALID_SENSITIVITIES = frozenset({"public", "private", "sensitive", "restricted"})
VALID_VISION_STRATEGIES = frozenset({"none", "auto", "objects", "vlm", "hybrid"})
VALID_PRESENTATIONS = frozenset({"inline", "report"})
VALID_EXECUTION_VISIBILITIES = frozenset({"foreground", "background"})


# Presentation is a user-interface contract, not a creative model decision.
# The semantic router still decides when a substantial answer benefits from a
# report, but an explicit request for a report must survive a bad router sample.
# Keep this deliberately about the requested *surface*, not about domains such
# as news, research, health, or study, so future report types inherit it.
_EXPLICIT_REPORT_PATTERNS = tuple(re.compile(pattern, re.IGNORECASE) for pattern in (
    r"\b(?:show|open|display|give|prepare|create|generate|render|write|present)\b"
    r"[^.!?\n]{0,80}\breport\b",
    r"\b(?:as|into)\s+(?:an?\s+)?(?:visual\s+|detailed\s+|rich\s+)?report\b",
    r"^\s*(?:please\s+)?report\b",
    r"\b(?:news|research|analysis|comparison|findings|results?|status)\s+report\b",
))


def explicit_report_requested(text: str) -> bool:
    """Whether the utterance explicitly requests the durable report surface.

    This intentionally does not trigger on a passive mention such as "the
    report says...".  The model remains free to choose a report for complex
    material; this function only protects direct presentation instructions.
    """
    compact = " ".join(str(text or "").split())
    return any(pattern.search(compact) for pattern in _EXPLICIT_REPORT_PATTERNS)


def _apply_presentation_contract(text: str, route: CognitiveRoute) -> CognitiveRoute:
    if explicit_report_requested(text) and route.presentation != "report":
        return replace(route, presentation="report")
    return route


@dataclass(frozen=True)
class RetrievalRoute:
    graph: bool = True
    episodes: bool = False
    conversations: bool = False
    include_unconfirmed_inferences: bool = False
    entities: Tuple[str, ...] = ("Himanshu",)
    predicates: Tuple[str, ...] = ()
    sensitivities: Tuple[str, ...] = ("public", "private")
    fact_limit: int = 10
    memory_limit: int = 0
    graph_hops: int = 1


@dataclass(frozen=True)
class CognitiveRoute:
    mode_decision: str
    target_mode: str
    interaction: str
    task_kind: str
    planning_depth: str
    needs_screen: bool
    confidence: float
    retrieval: RetrievalRoute
    # The model chooses the kind of evidence it needs; vision_pipeline then
    # validates that choice and owns fallback. "objects" is local YOLO26s,
    # "vlm" is pixels to the configured vision LLM, and "hybrid" is both.
    vision_strategy: str = "auto"
    acknowledgement: str = ""
    rationale: str = ""
    protocol_decision: str = "none"
    target_protocol: str = ""
    protocol_severity: str = "routine"
    protocol_reason: str = ""
    # Semantic control intents. The model may identify what Himanshu meant;
    # code still owns the state transition and permission boundary.
    explicit_mode_change: bool = False
    approval_decision: str = "none"
    continue_after_control: bool = True
    presentation: str = "inline"
    # Whether intermediate execution should occupy the visible desktop.  This
    # is presentation state, not authority: background work passes through the
    # same schemas, approval gates and verification as foreground work.
    execution_visibility: str = "foreground"
    # Operation-level retrieval queries authored by the model. These describe
    # desired outcomes (never payload text or action names) and let the action
    # registry scale without an ever-growing phrase/regex router.
    action_intents: Tuple[str, ...] = ()
    # Evidence and execution surfaces selected from resource_pool.  These are
    # not permissions and do not name concrete actions; downstream code uses
    # them to fetch only relevant context and retrieve a bounded capability set.
    resource_requests: Tuple[resource_pool.ResourceRequest, ...] = ()

    @property
    def should_switch_mode(self) -> bool:
        return self.mode_decision == "switch"

    @property
    def should_be_quiet(self) -> bool:
        return self.interaction == "quiet"

    @property
    def resource_names(self) -> frozenset[str]:
        return resource_pool.names(self.resource_requests)


_CACHE = {}
_CACHE_LOCK = threading.RLock()
_CACHE_TTL_SECONDS = 8.0


def _extract_json(raw: str):
    cleaned = re.sub(r"<think>.*?</think>", "", str(raw or ""), flags=re.DOTALL).strip()
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned, flags=re.IGNORECASE).strip()
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start < 0 or end < start:
        return None
    try:
        return json.loads(cleaned[start:end + 1])
    except (TypeError, ValueError, json.JSONDecodeError):
        return None


def _words(values, *, limit=10, length=80):
    if not isinstance(values, list):
        return ()
    result = []
    for value in values:
        item = " ".join(str(value or "").split())[:length]
        if item and item not in result:
            result.append(item)
    return tuple(result[:limit])


def _json_bool(value, default=False):
    """Strict boolean coercion for model-produced JSON."""
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, (int, float)) and value in {0, 1}:
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().casefold()
        if lowered in {"true", "1", "yes", "on"}:
            return True
        if lowered in {"false", "0", "no", "off", ""}:
            return False
    return default


def _fallback(current_mode: str) -> CognitiveRoute:
    return CognitiveRoute(
        mode_decision="keep", target_mode=current_mode, interaction="respond",
        task_kind="conversation", planning_depth="standard", needs_screen=False,
        confidence=0.0, retrieval=RetrievalRoute(), rationale="router fallback",
    )


def _model_first():
    """Whether semantic decisions always go through the configured model.

    The old economy routes remain available as an operational rollback, but
    model-first is the product default: resources provide evidence and the
    model decides relevance rather than phrase tables deciding behavior.
    """
    return os.getenv("VANTA_MODEL_FIRST_REASONING", "1").strip().lower() not in {
        "0", "false", "no", "off"
    }


def model_first_enabled():
    """Public read-only view used by downstream retrieval."""
    return _model_first()


def _apply_perception_contract(text: str, route: CognitiveRoute) -> CognitiveRoute:
    """Keep camera and desktop pixels in separate routing lanes.

    The cognitive model still decides task depth, mode, retrieval and vision
    strategy.  It does not get to reinterpret an unqualified physical visual
    request as permission to capture the desktop.  That source contract is
    derived from the same semantic evidence classifier used by outcome
    compilation, so every pipeline stage agrees.
    """
    decision = perception_intent.infer(text)
    if not decision.visual_request:
        return route
    needs_screen = decision.source == perception_intent.SCREEN
    strategy = route.vision_strategy
    if needs_screen and strategy == "none":
        strategy = "auto"
    if not needs_screen:
        strategy = "none"
    return replace(
        route,
        needs_screen=needs_screen,
        task_kind="observation",
        vision_strategy=strategy,
    )


_FAST_SOCIAL = frozenset({
    "hi", "hello", "hey", "thanks", "thank you", "okay", "ok", "got it",
    "good morning", "good afternoon", "good evening", "good night",
    "how are you", "nice", "great",
})
_FAST_QUESTION_PREFIXES = (
    "what is ", "what are ", "define ", "explain ", "can you explain ",
    "how does ", "how do ", "why does ", "why do ",
    "what is the difference between ",
)
_CONTEXT_DEPENDENT_MARKERS = (
    " this", " that", " these", " those", " it ", " my ", " me ",
    "we ", "our ", "you said", "i said", "earlier", "before", "last time",
    "remember", "screen", "window", "file", "folder", "project", "vanta",
    "winter", "email", "mail", "calendar", "notification", "system",
    "today", "tomorrow", "yesterday", "current", "latest", "now", "price",
    "weather", "news", "schedule",
)

# The semantic router is useful when a turn can change global behaviour, needs
# a protocol decision, or genuinely needs a multi-step plan. It is wasteful for
# the common case: one question or one reversible action. Those turns already
# go through the executor model, which can understand the request and select a
# lazily exposed tool. This gate is deliberately structural rather than a list
# of product-specific commands, so adding actions does not add router tokens.
_MODE_OR_SAFETY_MARKERS = (
    "mode", "study time", "lets study", "let's study", "lets build",
    "let's build", "just monitor", "stay quiet", "do not disturb",
    "don't disturb", "go to sleep", "going to sleep", "let me sleep", "i am unwell",
    "i feel sick", "accident", "emergency", "danger", "unsafe", "fraud",
    "hacked", "suicide", "kill myself",
)
_DEEP_WORK_MARKERS = (
    "deep research", "research mode", "build a plan", "make a plan",
    "plan my", "investigate", "compare and recommend", "end to end",
    "step by step and", "autonomously", "over the next", "monitor until",
)
_SCREEN_MARKERS = (
    "on my screen", "this screen", "active window", "what i'm looking at",
    "what i am looking at", "look at this", "read this diagram",
    "look at the screen", "see my screen", "read my screen", "this diagram",
    "this image", "image.png", "screenshot",
)
_PERSONAL_CONTEXT_MARKERS = (
    " my ", " me ", " i ", "we ", "our ", "remember", "earlier",
    "before", "history", "know about", "tell me about", "who is ",
    "who's ", " her ", " him ", " them ", "preference", "usually", "schedule", "calendar", "email",
    "mail", "contact", "project", "exam", "internship", "vanta", "winter",
)


def _economy_route(text: str, current_mode: str):
    """Return a safe one-model route when a separate router adds no value.

    Knowledge is not disabled here. Personal or continuity-dependent turns
    still retrieve from the graph; only the *model call that decides whether to
    retrieve* is removed. Ambiguous, safety-sensitive and explicitly complex
    turns continue through the semantic router.
    """
    normalized = " ".join(str(text or "").casefold().split())
    padded = f" {normalized} "
    if not normalized or len(normalized) > 700:
        return None
    if any(marker in normalized for marker in _MODE_OR_SAFETY_MARKERS):
        return None
    if any(marker in normalized for marker in _DEEP_WORK_MARKERS):
        return None
    # First-person present-state declarations often change the interruption
    # contract without containing an explicit command: being in a meeting,
    # driving, studying, watching something, resting, and countless future
    # situations all look like ordinary conversation lexically. Sending them
    # through the economy route forced interaction="respond" and prevented the
    # semantic router from deciding whether VANTA should become quiet.
    present_state = re.match(
        r"^(?:i\s*(?:am|'m)|we\s*(?:are|'re))\s+\S+", normalized)
    if present_state:
        return None

    needs_screen = any(marker in normalized for marker in _SCREEN_MARKERS)
    personal = any(marker in padded for marker in _PERSONAL_CONTEXT_MARKERS)
    # Questions about live/current external state should be grounded by the
    # executor's tools, not by old graph facts. The graph can still contribute
    # preferences when the turn is personal.
    return CognitiveRoute(
        mode_decision="keep", target_mode=current_mode,
        interaction="respond", task_kind="observation" if needs_screen else "conversation",
        planning_depth="direct", needs_screen=needs_screen, confidence=0.92,
        retrieval=RetrievalRoute(
            graph=personal, episodes=False, conversations=personal,
            entities=("Himanshu",) if personal else (),
            fact_limit=6 if personal else 0,
            memory_limit=2 if personal else 0,
        ),
        rationale="economy route: one executor call is sufficient",
    )


def _fast_route(text: str, current_mode: str):
    """Route only turns whose context needs are structurally unambiguous.

    This does not answer the question or infer intent with keywords. It merely
    removes a redundant model call when no screen, memory, live data, action,
    or planning decision can be relevant. Everything uncertain goes through
    the cognitive model as before.
    """
    lowered = f" {' '.join(str(text or '').casefold().split())} "
    compact = lowered.strip().rstrip(".!?")
    social = compact in _FAST_SOCIAL
    self_contained_question = (
        any(compact.startswith(prefix) for prefix in _FAST_QUESTION_PREFIXES)
        and not any(marker in lowered for marker in _CONTEXT_DEPENDENT_MARKERS)
        and len(compact) <= 240
    )
    if not (social or self_contained_question):
        return None
    return CognitiveRoute(
        mode_decision="keep", target_mode=current_mode,
        interaction="respond",
        task_kind="conversation" if social else "question",
        planning_depth="direct", needs_screen=False, confidence=1.0,
        retrieval=RetrievalRoute(
            graph=False, episodes=False, conversations=False,
            entities=(), fact_limit=0, memory_limit=0),
        rationale="deterministic fast route: no external context required",
    )


def _validate(data, current_mode: str) -> CognitiveRoute:
    if not isinstance(data, dict):
        return _fallback(current_mode)
    confidence = max(0.0, min(1.0, float(data.get("confidence", 0.0))))
    mode_decision = str(data.get("mode_decision", "keep")).lower()
    target_mode = str(data.get("target_mode", current_mode)).lower()
    interaction = str(data.get("interaction", "respond")).lower()
    task_kind = str(data.get("task_kind", "conversation")).lower()
    planning_depth = str(data.get("planning_depth", "standard")).lower()
    presentation = str(data.get("presentation", "inline")).lower()
    execution_visibility = str(
        data.get("execution_visibility", "foreground")).lower()
    vision_strategy = str(data.get("vision_strategy", "auto")).lower()
    if mode_decision not in VALID_MODE_DECISIONS or target_mode not in VALID_MODES:
        return _fallback(current_mode)
    if interaction not in VALID_INTERACTIONS or task_kind not in VALID_TASK_KINDS or planning_depth not in VALID_DEPTHS:
        return _fallback(current_mode)
    if vision_strategy not in VALID_VISION_STRATEGIES:
        vision_strategy = "auto"
    if presentation not in VALID_PRESENTATIONS:
        presentation = "inline"
    if execution_visibility not in VALID_EXECUTION_VISIBILITIES:
        execution_visibility = "foreground"
    resource_field_present = "resources" in data
    resource_requests = resource_pool.parse_requests(data.get("resources"))
    selected_resources = resource_pool.names(resource_requests)
    needs_screen = ("screen" in selected_resources if resource_field_present
                    else _json_bool(data.get("needs_screen", False)))
    if not needs_screen:
        vision_strategy = "none"
    elif vision_strategy == "none":
        vision_strategy = "auto"
    if confidence < 0.78:
        mode_decision, target_mode, interaction = "keep", current_mode, "respond"
    explicit_mode_change = _json_bool(data.get("explicit_mode_change", False))
    approval_decision = str(data.get("approval_decision", "none")).lower()
    if approval_decision not in {"none", "approve", "reject"}:
        approval_decision = "none"
    # Low-confidence interpretation can never alter runtime state or settle a
    # permission request, even if the model happened to emit those fields.
    if confidence < 0.78:
        explicit_mode_change = False
        approval_decision = "none"
    # A single reversible action can execute directly. Only an actual planning
    # request is promoted; promoting every action caused simple music playback
    # to generate a multi-step planner loop and duplicate browser surfaces.
    if task_kind == "planning" and planning_depth == "direct":
        planning_depth = "standard"
    if task_kind == "research":
        planning_depth = "deep"

    retrieval = data.get("retrieval") if isinstance(data.get("retrieval"), dict) else {}
    if resource_field_present:
        graph = "graph_memory" in selected_resources
        episodes = "semantic_memory" in selected_resources
        conversations = "historical_conversations" in selected_resources
    else:
        # Backward-compatible parsing for cached responses and older providers.
        graph = _json_bool(retrieval.get("graph", True), True)
        episodes = _json_bool(retrieval.get("episodes", False))
        conversations = _json_bool(retrieval.get("conversations", False))
    sensitivities = tuple(
        value for value in _words(retrieval.get("sensitivities"), limit=4, length=16)
        if value in VALID_SENSITIVITIES
    ) or ("public", "private")
    route = RetrievalRoute(
        graph=graph,
        episodes=episodes,
        conversations=conversations,
        include_unconfirmed_inferences=_json_bool(
            retrieval.get("include_unconfirmed_inferences", False)),
        entities=((_words(retrieval.get("entities"), limit=12)
                   or (("Himanshu",) if graph else ()))),
        predicates=_words(retrieval.get("predicates"), limit=12),
        sensitivities=sensitivities,
        fact_limit=(max(0, min(20, int(retrieval.get("fact_limit", 10))))
                    if graph else 0),
        memory_limit=(max(0, min(6, int(retrieval.get("memory_limit", 0))))
                      if episodes or conversations else 0),
        graph_hops=max(1, min(3, int(retrieval.get("graph_hops", 1)))),
    )
    action_intents = _words(data.get("action_intents"), limit=8, length=160)
    # Mode/approval/protocol controls already have typed fields in this route.
    # Some models also repeat a mode switch as an action intent (for example,
    # "activate health mode").  Sending that duplicate into capability
    # retrieval lets the executor perform/narrate the same control a second
    # time.  Remove only intents that describe the *same typed transition*;
    # independent work in a compound turn is preserved.
    if explicit_mode_change and mode_decision == "switch":
        target_tokens = set(target_mode.replace("_", " ").split())
        control_verbs = {
            "activate", "change", "deactivate", "disable", "enable",
            "enter", "leave", "set", "start", "stop", "switch",
        }

        def duplicates_mode_control(intent):
            tokens = set(re.findall(r"[a-z0-9]+", str(intent).casefold()))
            return bool(target_tokens and target_tokens.issubset(tokens)
                        and "mode" in tokens and tokens.intersection(control_verbs))

        action_intents = tuple(
            intent for intent in action_intents
            if not duplicates_mode_control(intent)
        )
    if confidence < 0.55:
        # An uncertain semantic parse may answer conversationally, but it may
        # not silently acquire sensors or propose operations.
        resource_requests = ()
        action_intents = ()
        needs_screen = False
        vision_strategy = "none"
        route = RetrievalRoute(
            graph=False, episodes=False, conversations=False,
            include_unconfirmed_inferences=False, entities=(), predicates=(),
            sensitivities=sensitivities, fact_limit=0, memory_limit=0,
            graph_hops=1,
        )
    acknowledgement = " ".join(str(data.get("acknowledgement", "")).split())[:220]
    if "?" in acknowledgement:
        acknowledgement = acknowledgement.replace("?", ".")
    valid_protocols = {"medical_emergency", "personal_safety", "cyber_incident",
                       "financial_fraud", "data_recovery", "crisis_support",
                       "owner_override", "lockdown", "public_space"}
    protocol_decision = str(data.get("protocol_decision", "none")).lower()
    target_protocol = str(data.get("target_protocol", "")).lower()
    severity = str(data.get("protocol_severity", "routine")).lower()
    operational_protocols = {"owner_override", "lockdown", "public_space"}
    allowed_severities = ({"routine", "serious", "critical"}
                          if target_protocol in operational_protocols
                          else {"serious", "critical"})
    if (protocol_decision != "recommend" or target_protocol not in valid_protocols
            or severity not in allowed_severities):
        protocol_decision, target_protocol, severity = "none", "", "routine"
    continue_after_control = _json_bool(
        data.get("continue_after_control", True), True)
    # A pure, explicit mode transition is complete at the control boundary.
    # Do not ask the executor to answer the same request again merely because
    # the router emitted the wrong boolean.  A compound turn remains live when
    # it has another operation, resource, approval, protocol, or screen task.
    pure_mode_control = bool(
        explicit_mode_change and mode_decision == "switch"
        and not action_intents and not resource_requests and not needs_screen
        and approval_decision == "none" and protocol_decision == "none"
    )
    if pure_mode_control:
        continue_after_control = False

    return CognitiveRoute(
        mode_decision=mode_decision,
        target_mode=target_mode if mode_decision == "switch" else current_mode,
        interaction=interaction,
        task_kind=task_kind,
        planning_depth=planning_depth,
        needs_screen=needs_screen,
        confidence=confidence,
        retrieval=route,
        vision_strategy=vision_strategy,
        acknowledgement=acknowledgement,
        rationale=" ".join(str(data.get("rationale", "")).split())[:240],
        protocol_decision=protocol_decision, target_protocol=target_protocol,
        protocol_severity=severity,
        protocol_reason=" ".join(str(data.get("protocol_reason", "")).split())[:240],
        explicit_mode_change=explicit_mode_change,
        approval_decision=approval_decision,
        continue_after_control=continue_after_control,
        presentation=presentation,
        execution_visibility=execution_visibility,
        action_intents=action_intents,
        resource_requests=resource_requests,
    )


def apply_resource_policy(route: CognitiveRoute, *, origin: str,
                          guest: bool = False,
                          remote: bool = False) -> CognitiveRoute:
    """Intersect model resource choices with code-owned channel policy.

    The model remains the semantic chooser. This boundary neither adds a
    resource nor substitutes one source for another; it can only remove access
    that is unsafe for the current origin, confidence, or identity state.
    """
    if route is None or not route.resource_requests:
        return route
    allowed = resource_pool.authorize_requests(
        route.resource_requests, origin=origin, confidence=route.confidence,
        guest=guest, remote=remote)
    selected = resource_pool.names(allowed)
    graph = "graph_memory" in selected
    episodes = "semantic_memory" in selected
    conversations = "historical_conversations" in selected
    old = route.retrieval
    retrieval = replace(
        old,
        graph=graph,
        episodes=episodes,
        conversations=conversations,
        include_unconfirmed_inferences=(
            old.include_unconfirmed_inferences and graph),
        entities=(old.entities if graph else ()),
        fact_limit=(old.fact_limit if graph else 0),
        memory_limit=(old.memory_limit if episodes or conversations else 0),
    )
    needs_screen = "screen" in selected
    vision_strategy = route.vision_strategy if needs_screen else "none"
    return replace(
        route,
        resource_requests=allowed,
        retrieval=retrieval,
        needs_screen=needs_screen,
        vision_strategy=vision_strategy,
    )


# Entering one of these restricts what VANTA may do, or asserts something
# about Himanshu's physical state. He declares them; they are not inferred from
# the shape of a task.
_DECLARED_STATE_MODES = frozenset({"sleep", "health"})


def should_apply_mode_switch(route: CognitiveRoute, explicit: bool = False) -> bool:
    """Whether this turn may change the global operating mode on its own.

    It almost never may. Himanshu asked to be the one who decides, and the
    reason is in the logs: "Open my placements portal and search for the
    latest jobs" was routed to Research with 0.9 confidence, which switched
    VANTA into Research mode, resumed an autonomous study session on graph
    theory, and left that session streaming into the same console as the
    portal task until the two outputs were spliced through each other
    mid-sentence.

    An explicit request is obeyed -- those arrive through
    mode_manager.parse_mode_command and never reach here. An emergency is
    taken without asking, because asking costs exactly the time that matters.
    Everything else is put to him instead; see mode_switch_needs_confirmation.
    """
    if not (route and route.should_switch_mode):
        return False
    # Explicit only. There used to be an exception for emergency mode, which
    # was worth taking without asking; that mode no longer exists, and with
    # two modes left there is nothing urgent enough to switch to unasked.
    return bool(explicit or route.explicit_mode_change)


def should_begin_contextual_quiet(route: CognitiveRoute,
                                  origin: str = "user") -> bool:
    """Whether this turn declares a durable attention-protection window.

    Routers sometimes use ``quiet`` to mean "acknowledge this control command
    concisely".  An explicit mode switch is still a control command, not a
    request for three hours without proactive help.
    """
    return bool(origin == "user" and route and route.should_be_quiet
                and not route.explicit_mode_change)


def mode_switch_needs_confirmation(route: CognitiveRoute) -> bool:
    """Whether to ask before changing mode, rather than just declining to.

    Silence would be worse than the old behaviour: the router would keep
    being right about tutor or build mode and nothing would ever come of it.
    So a plausible switch becomes a question.
    """
    if not (route and route.should_switch_mode):
        return False
    # Work he asked for this turn is not a reason to change mode, and asking
    # about it is just noise in front of the thing he wanted done. A screen or
    # browser action proposing Research is the specific misread above; the
    # current mode handles that turn perfectly well.
    if route.needs_screen or route.task_kind in {"action", "observation"}:
        return False
    # Below this the router is guessing, and a guess is not worth a question.
    return route.confidence >= 0.75


def route_turn(text: str, current_mode: str, call_llm_fn: Callable,
               pending_approval: bool = False) -> CognitiveRoute:
    """Create one semantic route for the turn, cached across pipeline stages."""
    complete = " ".join(str(text or "").split())
    # Long pasted text is evidence too. Preserve both its beginning and its
    # latest/request-bearing end rather than letting a fixed prefix determine
    # routing while the executor later sees a different, complete input.
    normalized = (complete if len(complete) <= 6000 else
                  complete[:3000] + " … [middle omitted for routing] … "
                  + complete[-3000:])
    current = current_mode if current_mode in VALID_MODES else "assistant"
    # A pending permission makes even a short "yes/okay/do it" contextual.
    # It must be interpreted against the pending request, never economy-routed
    # as social conversation.
    model_first = _model_first()
    fast = None if (pending_approval or model_first) else _fast_route(normalized, current)
    if fast is not None:
        return _apply_presentation_contract(
            normalized, _apply_perception_contract(normalized, fast))
    economy = None if (pending_approval or model_first) else _economy_route(normalized, current)
    if economy is not None:
        return _apply_presentation_contract(
            normalized, _apply_perception_contract(normalized, economy))
    try:
        import session_context
        recent_context = session_context.get_recent_chat()[-2600:]
    except Exception:
        recent_context = "No recent chat history."
    # Identical words can mean different operations after a different exchange
    # ("do it", "send that", "open it"). Context is therefore part of the
    # semantic decision's identity, not merely material added to its prompt.
    key = (current, normalized.casefold(), bool(pending_approval),
           hash(recent_context))
    now = time.monotonic()
    with _CACHE_LOCK:
        cached = _CACHE.get(key)
        if cached and now - cached[0] <= _CACHE_TTL_SECONDS:
            cached_route = (cached[1] if model_first and cached[1].confidence > 0 else
                            _apply_perception_contract(normalized, cached[1]))
            return _apply_presentation_contract(normalized, cached_route)

    schema = {
        "mode_decision": "keep|switch", "target_mode": "available mode",
        "interaction": "respond|quiet", "task_kind": "conversation|question|action|research|observation|planning",
        "planning_depth": "direct|standard|deep", "needs_screen": False,
        "presentation": "inline|report",
        "execution_visibility": "foreground|background",
        "vision_strategy": "none|auto|objects|vlm|hybrid",
        "confidence": 0.0, "acknowledgement": "one brief sentence or empty",
        "rationale": "short reason",
        "explicit_mode_change": False,
        "approval_decision": "none|approve|reject",
        "continue_after_control": True,
        "protocol_decision": "none|recommend",
        "target_protocol": "medical_emergency|personal_safety|cyber_incident|financial_fraud|data_recovery|crisis_support|owner_override|lockdown|public_space|empty",
        "protocol_severity": "routine|serious|critical",
        "protocol_reason": "short evidence-based reason",
        "action_intents": ["operation-level outcome, excluding user payload"],
        "resources": [{"name": "one registered resource", "purpose": "why its evidence or surface is needed", "required": False}],
        "retrieval": {
            "graph": True, "episodes": False, "conversations": False,
            "include_unconfirmed_inferences": False, "entities": ["Himanshu"],
            "predicates": [], "sensitivities": ["public", "private"],
            "fact_limit": 10, "memory_limit": 0, "graph_hops": 1,
        },
    }
    prompt = (
        "Interpret this turn for VANTA as a semantic control decision. Return exactly one JSON object matching this schema:\n"
        + json.dumps(schema, separators=(",", ":"))
        + f"\nCurrent mode: {current}\nAvailable modes:\n{_mode_menu()}\n"
        "Available resources (choose by affordance, not by memorised wording):\n"
        + resource_pool.router_catalog() + "\n"
        + f"A human approval is currently pending: {bool(pending_approval)}\n"
        "Infer meaning from the utterance, including paraphrases and mistakes. Do not match keywords. "
        "Treat resources as evidence or execution surfaces, never as authority. Select only resources that can materially change the answer or enable an intended outcome. An empty resource list means no unrelated state should be fetched. "
        "For action_intents, emit one short operation-level intent per independent outcome. Describe what must be achieved without copying message bodies, filenames used as content, quoted text, or action names. Mode changes, approval decisions, and protocol recommendations belong only in their typed control fields; never repeat them as action_intents. A requested lifecycle operation such as standby, resume, quiet, mute, or abort is an action intent even when it is wrapped in an apology, thanks, or other social wording. Use an empty list for conversation with no operation. "
        "Choose presentation=inline for ordinary answers, web lookups, action receipts, links, status, and basic information. Choose presentation=report when the user asks to be taught or needs worked mathematics, a durable visual explanation, a substantial comparison, table, diagram, chart, code walkthrough, or detailed structured material on screen. Decide from meaning, not phrases. Never open a report merely because a tool returned many lines. "
        "Choose execution_visibility=background when the user wants the work carried out without showing progress, taking focus, opening visible work surfaces, or narrating intermediate steps. Background changes only visibility: it must still execute, verify, preserve approvals, and return one concise completion or blocker. Otherwise choose foreground. "
        "Set explicit_mode_change only when the user directly requests a mode transition, including as one part of a compound request. Deactivating a named active mode means switching to assistant; do not treat discussion about modes as a command. "
        "Set approval_decision only when an approval is pending and the user clearly accepts or rejects that pending operation. The model identifies intent but never grants permission itself. Set continue_after_control=false only when the turn contains no request beyond those control decisions. "
        "Use switch only for a clear present intention or state; references to the past, hypotheticals, and questions normally keep the mode. "
        "Use quiet when the user's current situation implies that VANTA should "
        "acknowledge once and then protect their attention, even if they state "
        "the situation naturally instead of issuing a command. Quiet does not "
        "mean no acknowledgement; it means no unprompted follow-up afterward. "
        "Retrieve only context that can materially change the answer. Current chat outranks graph facts; graph facts outrank historical episodes. "
        "A request concerning a user-owned resource (for example their portal, account, booking, project, repository, or saved destination) requires graph_memory unless its exact locator is already present in the recent exchange. Public web search is not a substitute for a remembered authoritative locator. "
        "Request sensitive or restricted facts only when directly necessary. Unconfirmed inferences are only for questions about possible patterns. "
        "The resources list is authoritative for evidence selection. Set needs_screen to mirror whether the screen resource was selected. For screen evidence, choose vision_strategy=objects for local object geometry, vlm for text or visual meaning, hybrid when both materially help, or auto when uncertain. "
        "The acknowledgement must not ask a question or claim that an action happened.\n"
        "Recommend an emergency protocol only for a credible serious or critical situation, never for casual discussion, hypotheticals, or resolved past events. Owner Override is a routine, time-limited conversation-only identity exception when Himanshu explicitly needs to talk in darkness, while moving, or without a usable camera; it never authorizes actions. Lockdown is for protecting an unattended or exposed local machine and routes personal output remotely. Public Space keeps general local help available but routes personal details remotely. Recommend these operational protocols from their meaning, not exact phrases. "
        "Use planning_depth=direct for a single reversible action, a direct answer, a short read, or an ordinary conversational turn. "
        "Use standard only when ordering, ambiguity, or multiple dependent operations materially matter. "
        "Use deep only for genuinely complex, high-consequence, or open-ended work. "
        "Do not request screen, graph, episodes, conversations, or personal context merely because they are available; each must change the answer to this turn.\n"
        "The recent exchange below is continuity evidence. Resolve pronouns and "
        "short follow-ups from it, but never revive completed work or treat an "
        "old assistant statement as a new instruction.\n"
        f"Recent exchange:\n{recent_context}\n"
        f"User turn: {normalized}"
    )
    try:
        raw = call_llm_fn(
            [{"role": "system", "content": "You are VANTA's typed cognitive router. Output JSON only."},
             {"role": "user", "content": prompt}],
            task_level="cognitive_router",
        )
        result = _validate(_extract_json(raw), current)
    except Exception as exc:
        result = _fallback(current)
        object.__setattr__(result, "rationale", f"router error: {type(exc).__name__}")
    # In model-first mode the semantic model owns source interpretation too.
    # The deterministic perception classifier is retained only as a rollback
    # for deployments that explicitly disable model-first reasoning.
    if not model_first or result.confidence <= 0:
        result = _apply_perception_contract(normalized, result)
    result = _apply_presentation_contract(normalized, result)
    with _CACHE_LOCK:
        _CACHE.clear()
        _CACHE[key] = (now, result)
    return result
