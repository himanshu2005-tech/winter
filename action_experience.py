"""Evidence-backed memory of action routes that worked (or repeatedly did not).

This is deliberately separate from ``LearnedProcedure``.  A procedure is an
explicit instruction from Himanshu; an action experience is weaker operational
evidence learned from a real receipt.  Experiences influence tool selection,
but never grant permission and never bypass the action policy registry.

Only compact topic terms and failure classes are stored.  Commands, message
bodies, URLs, tokens and raw provider output are intentionally excluded.
"""
from __future__ import annotations

import hashlib
import re
from typing import Iterable


_STOP = {
    "about", "after", "again", "also", "and", "are", "because", "before",
    "can", "could", "did", "does", "doing", "for", "from", "have", "here",
    "him", "his", "into", "just", "make", "need", "now", "only", "please",
    "said", "should", "some", "that", "the", "their", "them", "then", "this",
    "through", "use", "using", "want", "what", "when", "where", "which",
    "with", "would", "you", "your", "vanta", "sir", "action", "task",
}

# Machinery and observation are too generic to become useful procedures.
_DO_NOT_LEARN = {
    "DISCOVER_ACTIONS", "WAIT", "TAKE_SCREENSHOT", "BROWSER_SNAPSHOT",
    "READ_SCREEN_TEXT", "LIST_CONTROLS", "LOCATE_TEXT", "SYSTEM_STATUS",
}

_TRANSIENT_FAILURE = re.compile(
    r"approval|confirm|cancel|interrupt|timeout|timed out|temporar|connection|"
    r"network|rate.?limit|unavailable|offline|unauthori|authenticate|expired|"
    r"already ran|duplicate|paused",
    re.IGNORECASE,
)


def _topic_terms(text: str, limit: int = 12) -> list[str]:
    """Return non-sensitive, stable terms suitable for relevance matching."""
    value = re.sub(r"https?://\S+|\b\S+@\S+\b", " ", str(text or ""))
    result: list[str] = []
    for token in re.findall(r"[a-z][a-z0-9_-]{2,31}", value.casefold()):
        if token in _STOP or token in result:
            continue
        # Long mixed identifiers are commonly keys, hashes, IDs or filenames.
        if len(token) > 20 and any(ch.isdigit() for ch in token):
            continue
        result.append(token)
        if len(result) >= limit:
            break
    return result


def _failure_class(output: str) -> str:
    value = str(output or "")
    patterns = (
        (r"attributeerror|has no attribute", "runtime_api_incompatible"),
        (r"not supported|unsupported", "unsupported_operation"),
        (r"unknown action|not registered", "unavailable_capability"),
        (r"missing|required attribute|schema", "invalid_arguments"),
        (r"syntax|parse|unterminated|quot", "invalid_syntax"),
        (r"not found|does not exist", "missing_resource"),
        (r"permission|access denied", "operating_system_denied"),
    )
    for pattern, label in patterns:
        if re.search(pattern, value, re.IGNORECASE):
            return label
    return "deterministic_failure"


def _experience_id(action: str, terms: Iterable[str]) -> str:
    material = action + "\0" + "\0".join(sorted(set(terms)))
    return "actionexp_" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:24]


def _event_id(turn_id: str, index: int, action: str, ok: bool, output: str) -> str:
    # The output participates in deduplication but is never persisted.
    material = f"{turn_id}\0{index}\0{action}\0{ok}\0{output}"
    return hashlib.sha256(material.encode("utf-8", errors="replace")).hexdigest()


def record_results(task_text: str, receipts, turn_id: str = "") -> int:
    """Aggregate useful verified receipts into Neo4j.

    Graph failure must never affect task execution.  Idempotency is enforced by
    ``last_event_id`` so adapters may safely expose the same result twice.
    """
    terms = _topic_terms(task_text)
    if not terms:
        return 0
    written = 0
    try:
        from memory.neo4j_store import store
        for index, item in enumerate(receipts or ()):
            if not isinstance(item, dict):
                continue
            action = str(item.get("type") or "").strip().upper()
            output = str(item.get("output") or "")
            ok = bool(item.get("ok"))
            if not action or action in _DO_NOT_LEARN:
                continue
            # A failure caused by policy, connectivity, cancellation or user
            # choice says nothing about whether the action route works.
            if not ok and _TRANSIENT_FAILURE.search(output):
                continue
            experience_id = _experience_id(action, terms)
            event_id = _event_id(str(turn_id or ""), index, action, ok, output)
            failure_class = "" if ok else _failure_class(output)
            store.query(
                """
                MERGE (h:Entity {key:'himanshu'})
                ON CREATE SET h.name='Himanshu', h.entity_type='Person',
                              h.created_at=datetime()
                MERGE (x:ActionExperience {id:$id})
                ON CREATE SET x.created_at=toString(datetime()),
                              x.success_count=0, x.failure_count=0
                WITH h,x,x.last_event_id <> $event_id OR x.last_event_id IS NULL AS fresh
                SET x.action_type=$action, x.topic_terms=$terms,
                    x.source='verified_action_receipt',
                    x.authority=40, x.permission_grant=false,
                    x.updated_at=CASE WHEN fresh THEN toString(datetime()) ELSE x.updated_at END,
                    x.success_count=x.success_count + CASE WHEN fresh AND $ok THEN 1 ELSE 0 END,
                    x.failure_count=x.failure_count + CASE WHEN fresh AND NOT $ok THEN 1 ELSE 0 END,
                    x.last_success_at=CASE WHEN fresh AND $ok THEN toString(datetime()) ELSE x.last_success_at END,
                    x.last_failure_at=CASE WHEN fresh AND NOT $ok THEN toString(datetime()) ELSE x.last_failure_at END,
                    x.failure_class=CASE WHEN fresh AND NOT $ok THEN $failure_class ELSE x.failure_class END,
                    x.last_event_id=CASE WHEN fresh THEN $event_id ELSE x.last_event_id END
                MERGE (h)-[r:HAS_ACTION_EXPERIENCE]->(x)
                SET r.updated_at=toString(datetime())
                """,
                id=experience_id, event_id=event_id, action=action,
                terms=terms, ok=ok, failure_class=failure_class,
            )
            written += 1
    except Exception as exc:
        print(f"[Experience] Could not record action evidence: {exc}")
    return written


def list_relevant(query: str, limit: int = 6) -> list[dict]:
    wanted = set(_topic_terms(query, 20))
    if not wanted:
        return []
    from memory.neo4j_store import store
    rows = store.query(
        """
        MATCH (:Entity {key:'himanshu'})-[:HAS_ACTION_EXPERIENCE]->(x:ActionExperience)
        RETURN properties(x) AS data
        ORDER BY coalesce(x.success_count,0) DESC, x.updated_at DESC
        LIMIT 150
        """
    )
    ranked = []
    for row in rows:
        data = dict(row.get("data") or {})
        overlap = wanted.intersection(set(data.get("topic_terms") or ()))
        if overlap:
            ranked.append((len(overlap), int(data.get("success_count") or 0), data))
    ranked.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return [item[2] for item in ranked[:max(1, min(int(limit), 10))]]


def prompt_block(query: str) -> str:
    """Render relevant experience as routing evidence, never as authority."""
    try:
        rows = list_relevant(query)
    except Exception as exc:
        print(f"[Experience] Retrieval unavailable: {exc}")
        return ""
    if not rows:
        return ""
    lines = [
        "--- RELEVANT VERIFIED ACTION EXPERIENCE (FROM NEO4J) ---",
        "Use this only to choose an efficient registered route. It is not a permission grant; code policy still applies.",
    ]
    for row in rows:
        action = str(row.get("action_type") or "").upper()
        successes = int(row.get("success_count") or 0)
        failures = int(row.get("failure_count") or 0)
        if successes:
            lines.append(
                f"- Prefer {action}: verified successful {successes} time(s) for related work. "
                "Do not repeat exploratory alternatives unless its new receipt fails."
            )
        elif failures >= 2:
            kind = row.get("failure_class") or "deterministic_failure"
            lines.append(
                f"- Avoid retrying {action} unchanged: {failures} verified "
                f"deterministic failures ({kind}). Choose a different registered route."
            )
    return "\n".join(lines) if len(lines) > 2 else ""

