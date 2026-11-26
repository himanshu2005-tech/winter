"""Typed retention policy for user-authored memory candidates.

The language model decides *what* a statement means; this module owns the
boring but important boundary between remembering information and changing
runtime behaviour.  A graph fact is cheap and reversible.  A directive,
procedure, commitment, or recurring routine affects future turns, so it needs
stronger evidence and a typed, explicit decision from the extractor.

Nothing here grants action permission.  Learned preferences and procedures
remain subordinate to the code-owned action policy.
"""
from __future__ import annotations

import datetime as _dt
from pathlib import Path


KINDS = frozenset({
    "identity", "fact", "personal_preference", "assistant_preference",
    "relationship", "resource", "goal", "project", "commitment",
    "self_reported_pattern", "procedure", "routine",
})

_KIND_PREDICATES = {
    "personal_preference": "HAS_PREFERENCE",
    "assistant_preference": "HAS_PREFERENCE",
    "commitment": "HAS_COMMITMENT",
    "routine": "HAS_ROUTINE",
    "procedure": "HAS_PROCEDURE",
}


def _clean(value, limit=600):
    return " ".join(str(value or "").split())[:limit]


def _bool(value):
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes"}


def _minutes(value):
    try:
        return max(0, min(int(value or 0), 24 * 60))
    except (TypeError, ValueError):
        return 0


def _local_iso(value):
    """Validate an ISO timestamp and return a local, timezone-naive value.

    Commitments use local wall-clock arithmetic.  The extractor may include an
    offset, so convert it before removing timezone information rather than
    silently shifting the appointment.
    """
    text = _clean(value, 80)
    if not text:
        return ""
    try:
        parsed = _dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return ""
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone().replace(tzinfo=None)
    return parsed.isoformat(timespec="seconds")


def normalize_candidate(candidate, user_text):
    """Return a validated candidate or ``None``.

    Exact evidence is mandatory for operational memory.  Older extractors did
    not emit ``memory_kind``; those candidates remain valid graph facts, which
    keeps this an additive migration.
    """
    if not isinstance(candidate, dict):
        return None
    item = dict(candidate)
    try:
        confidence = float(item.get("confidence", 1.0))
    except (TypeError, ValueError):
        return None
    if not 0.0 <= confidence <= 1.0:
        return None
    item["confidence"] = confidence

    kind = _clean(item.get("memory_kind") or "fact", 40).lower()
    if kind not in KINDS:
        kind = "fact"
    item["memory_kind"] = kind

    quote = _clean(item.get("quote"), 400)
    source = ' '.join(str(user_text or "").split())
    if quote and quote.casefold() not in source.casefold():
        quote = ""
    item["quote"] = quote

    # A model may label a low-certainty interpretation as durable.  It can be
    # retained as ordinary dialogue evidence, but it must not become a fact.
    if confidence < 0.75:
        return None
    if kind in _KIND_PREDICATES:
        item["predicate"] = _KIND_PREDICATES[kind]

    item["persistent"] = _bool(item.get("persistent"))
    item["commitment_status"] = _clean(
        item.get("commitment_status"), 30).lower()
    item["hard_time"] = _local_iso(item.get("hard_time"))
    item["schedule"] = _clean(item.get("schedule"), 80)
    item["instruction"] = _clean(item.get("instruction"), 600)
    item["trigger"] = _clean(item.get("trigger"), 320)
    return item


def operational_effect(candidate):
    """Describe the permitted operational memory effect, without executing it."""
    item = candidate or {}
    kind = item.get("memory_kind", "fact")
    # Operational memory needs verbatim evidence and a high-confidence,
    # persistent/confirmed classification.  Ordinary graph facts do not.
    if not item.get("quote") or float(item.get("confidence", 0.0)) < 0.90:
        return "graph_only"
    operation = str(item.get("operation") or "upsert").lower()
    if kind == "assistant_preference" and operation == "remove":
        return "remove_directive"
    if kind == "procedure" and operation == "remove":
        return "remove_procedure"
    if kind == "assistant_preference" and item.get("persistent"):
        return "directive"
    if kind == "procedure" and item.get("persistent"):
        return "procedure"
    if (kind == "commitment" and item.get("commitment_status") == "confirmed"
            and item.get("hard_time")):
        return "commitment"
    if (kind == "routine" and item.get("persistent") and item.get("schedule")
            and item.get("instruction")):
        return "routine"
    return "graph_only"


def apply_operational_memory(candidate, source_id, base_dir=None):
    """Apply a strongly evidenced typed memory, idempotently.

    This deliberately does not create alarms.  An alarm is an interrupting
    action and belongs to an explicit reminder request handled by the normal
    action pipeline, not to background fact extraction.
    """
    effect = operational_effect(candidate)
    if effect == "graph_only":
        return ""
    root = str(base_dir or Path(__file__).resolve().parents[1])
    quote = candidate.get("quote", "")

    if effect == "directive":
        import persona
        instruction = (candidate.get("instruction")
                       or candidate.get("object") or quote)
        return persona.add_directive(
            root, instruction, source=f"implicit_user_preference:{source_id}")

    if effect == "remove_directive":
        import persona
        identifier = (candidate.get("instruction")
                      or candidate.get("object") or quote)
        return persona.remove_directive(root, identifier)

    if effect == "procedure":
        import learned_procedures
        return learned_procedures.learn(
            candidate.get("trigger", ""),
            candidate.get("instruction") or candidate.get("object", ""),
            actions=candidate.get("actions", ""), evidence=quote)

    if effect == "remove_procedure":
        import learned_procedures
        identifier = (candidate.get("instruction") or candidate.get("object")
                      or candidate.get("trigger") or quote)
        return learned_procedures.retire(identifier)

    if effect == "commitment":
        import commitments
        return commitments.add(root, {
            "what": _clean(candidate.get("object"), 300),
            "hard_time": candidate["hard_time"],
            "location": _clean(candidate.get("location"), 200),
            "prep_minutes": _minutes(candidate.get("prep_minutes")),
            "travel_minutes": _minutes(candidate.get("travel_minutes")),
            "source_id": str(source_id),
            "evidence": quote,
        })

    if effect == "routine":
        import routines
        name = (_clean(candidate.get("name"), 80)
                or _clean(candidate.get("object"), 80)
                or "Learned routine")
        return routines.add(
            root, name, candidate["instruction"], candidate["schedule"],
            expires_on=_clean(candidate.get("expires_on"), 10))
    return ""
