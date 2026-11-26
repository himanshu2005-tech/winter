# -*- coding: utf-8 -*-
"""Typed resources the cognitive model may request for one turn.

Resources are evidence and execution surfaces, not commands.  The model may
choose a resource because it can materially help with a goal; code still owns
privacy, authorization, availability checks, action dispatch and verification.

Keeping this registry separate from natural-language routing is important:
adding a new resource adds one descriptor, not another collection of phrases
such as "if he says X, use Y".
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Tuple


@dataclass(frozen=True)
class ResourceDescriptor:
    name: str
    kind: str
    affordance: str
    privacy: str = "private"
    freshness: str = "live"
    context_provider: bool = False
    capability_surface: bool = False

    def router_line(self) -> str:
        qualities = [self.kind, self.privacy, self.freshness]
        return f"- {self.name} ({', '.join(qualities)}): {self.affordance}"


@dataclass(frozen=True)
class ResourceRequest:
    name: str
    purpose: str = ""
    required: bool = False


def _json_bool(value, default: bool = False) -> bool:
    """Parse a JSON boolean without making ``"false"`` truthy."""
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


_DESCRIPTORS = (
    ResourceDescriptor(
        "screen", "perception",
        "current pixels and UI state on the active digital display",
        "sensitive", context_provider=True, capability_surface=True),
    ResourceDescriptor(
        "camera", "perception",
        "the current physical scene, people, faces, documents, and objects",
        "restricted", context_provider=True, capability_surface=True),
    ResourceDescriptor(
        "selected_text", "perception",
        "text or code currently selected by the user in the foreground app",
        "sensitive", context_provider=True),
    ResourceDescriptor(
        "graph_memory", "memory",
        "durable facts, relationships, preferences, goals, and reviewed knowledge",
        "sensitive", "current"),
    ResourceDescriptor(
        "semantic_memory", "memory",
        "historical memories and episodes relevant to the present goal",
        "sensitive", "historical", context_provider=True),
    ResourceDescriptor(
        "historical_conversations", "memory",
        "older conversation evidence beyond the bounded exchange already in this turn",
        "sensitive", "historical"),
    ResourceDescriptor(
        "calendar", "personal_service",
        "calendar events, availability, and schedule mutations",
        "sensitive", context_provider=True, capability_surface=True),
    ResourceDescriptor(
        "tasks", "personal_service",
        "task lists, outstanding work, and task mutations",
        "private", context_provider=True, capability_surface=True),
    ResourceDescriptor(
        "alarms", "personal_service",
        "local alarms and reminder timing",
        "private", context_provider=True, capability_surface=True),
    ResourceDescriptor(
        "email", "personal_service",
        "mail search, reading, drafting, and sending",
        "restricted", capability_surface=True),
    ResourceDescriptor(
        "contacts", "personal_service",
        "known people and their contact details",
        "restricted", capability_surface=True),
    ResourceDescriptor(
        "browser", "application",
        "managed web navigation, page structure, forms, downloads, and uploads",
        "sensitive", capability_surface=True),
    ResourceDescriptor(
        "web", "knowledge",
        "fresh public information and cited research from the internet",
        "public", capability_surface=True),
    ResourceDescriptor(
        "files", "workspace",
        "files and folders in authorized local or connected storage",
        "sensitive", capability_surface=True),
    ResourceDescriptor(
        "code_workspace", "workspace",
        "projects, source trees, code intelligence, builds, and tests",
        "sensitive", capability_surface=True),
    ResourceDescriptor(
        "terminal", "execution",
        "commands and scripts executed under the code-owned command policy",
        "restricted", capability_surface=True),
    ResourceDescriptor(
        "system_state", "device",
        "live battery, CPU, memory, processes, windows, and device state",
        "private", context_provider=True, capability_surface=True),
    ResourceDescriptor(
        "location", "device",
        "current or saved location evidence and travel origin",
        "sensitive", context_provider=True, capability_surface=True),
    ResourceDescriptor(
        "environment", "device",
        "weather, air quality, traffic, and ambient conditions",
        "private", context_provider=True, capability_surface=True),
    ResourceDescriptor(
        "notifications", "personal_service",
        "held notifications, watches, follow-ups, and proactive inbox items",
        "sensitive", context_provider=True, capability_surface=True),
    ResourceDescriptor(
        "display", "presentation",
        "the rich report for streaming prose/code, LaTeX, tables, charts, diagrams, images, 3D, animation, clickable terms, interactive parameter explorers, tabbed lessons, flashcards, code walkthroughs, questionnaires and quizzes",
        "private", capability_surface=True),
)

DESCRIPTORS = {item.name: item for item in _DESCRIPTORS}
VALID_NAMES = frozenset(DESCRIPTORS)
_ALIASES = {"conversation_history": "historical_conversations"}


def router_catalog() -> str:
    """Compact model-facing resource pool; no trigger phrases or action names."""
    return "\n".join(item.router_line() for item in _DESCRIPTORS)


def parse_requests(values, *, limit: int = 12) -> Tuple[ResourceRequest, ...]:
    """Validate a model response without granting access or inventing names."""
    if not isinstance(values, list):
        return ()
    result = []
    seen = set()
    for value in values:
        if isinstance(value, str):
            name, purpose, required = value, "", False
        elif isinstance(value, dict):
            name = value.get("name", value.get("resource", ""))
            purpose = value.get("purpose", "")
            required = _json_bool(value.get("required", False))
        else:
            continue
        name = str(name or "").strip().casefold().replace("-", "_")
        name = _ALIASES.get(name, name)
        if name not in VALID_NAMES or name in seen:
            continue
        purpose = " ".join(str(purpose or "").split())[:180]
        result.append(ResourceRequest(name, purpose, required))
        seen.add(name)
        if len(result) >= limit:
            break
    return tuple(result)


def names(requests: Iterable[ResourceRequest]) -> frozenset[str]:
    return frozenset(item.name for item in (requests or ()))


def context_names(requests: Iterable[ResourceRequest]) -> frozenset[str]:
    return frozenset(
        item.name for item in (requests or ())
        if DESCRIPTORS[item.name].context_provider)


def authorize_requests(requests: Iterable[ResourceRequest], *, origin: str,
                       confidence: float, guest: bool = False,
                       remote: bool = False) -> Tuple[ResourceRequest, ...]:
    """Apply code-owned boundaries to a model-selected resource set.

    There is deliberately no utterance matching here. The model decides what
    evidence may help; this function decides whether that evidence class may
    be acquired on the current channel. Concrete actions are still subject to
    the stricter authorization registry when they are dispatched.
    """
    origin = str(origin or "user").casefold()
    try:
        confidence = float(confidence)
    except (TypeError, ValueError):
        confidence = 0.0

    allowed = []
    for request in requests or ():
        descriptor = DESCRIPTORS.get(request.name)
        if descriptor is None:
            continue
        if guest and descriptor.privacy != "public" and request.name != "display":
            continue
        if remote and request.name in {"screen", "camera", "selected_text"}:
            continue
        if origin != "user" and request.name in {
                "screen", "camera", "selected_text"}:
            continue
        if confidence < 0.55:
            continue
        if (confidence < 0.70
                and descriptor.privacy in {"sensitive", "restricted"}):
            continue
        allowed.append(request)
    return tuple(allowed)


def capability_queries(requests: Iterable[ResourceRequest]) -> Tuple[str, ...]:
    """Retrieval text for selected execution surfaces.

    These are not action choices.  They ensure the semantic registry can show
    the executor relevant capabilities even when the model described the goal
    tersely.  The executor still chooses a concrete action and dispatch still
    applies authorization and schema validation.
    """
    result = []
    for request in requests or ():
        descriptor = DESCRIPTORS[request.name]
        if not descriptor.capability_surface:
            continue
        query = f"{descriptor.name.replace('_', ' ')} {request.purpose}".strip()
        if query and query not in result:
            result.append(query)
    return tuple(result)


def prompt_block(requests: Iterable[ResourceRequest]) -> str:
    items = tuple(requests or ())
    if not items:
        return "MODEL-SELECTED RESOURCES: none; answer without fetching unrelated state."
    lines = ["MODEL-SELECTED RESOURCES (evidence/surfaces, not permissions):"]
    for item in items:
        purpose = f" - {item.purpose}" if item.purpose else ""
        required = " [required]" if item.required else ""
        lines.append(f"- {item.name}{required}{purpose}")
    return "\n".join(lines)
