"""Graph-backed behavioural procedures learned explicitly from Himanshu.

These are neither free-form conversation history nor permissions.  They are
small conditional playbooks the model may apply when relevant.  Code-owned
authorization still evaluates every action in a playbook at execution time.
"""
from __future__ import annotations

import hashlib
import json
import re


_STOP = {
    "a", "an", "and", "are", "as", "at", "be", "do", "for", "from",
    "i", "if", "in", "is", "it", "me", "my", "of", "on", "or", "that",
    "the", "then", "this", "to", "u", "you", "your",
}


def _clean(value, limit):
    return " ".join(str(value or "").split())[:limit]


def _tokens(value):
    def stem(token):
        # Lightweight retrieval normalisation, not linguistic interpretation.
        # User-taught triggers are commonly phrased as "when teaching" while
        # the later request says "teach me".  Exact-token matching made those
        # two turns unrelated and the stored procedure looked forgotten.
        for suffix in ("ingly", "edly", "ing", "ed", "ies", "es", "s"):
            if token.endswith(suffix) and len(token) - len(suffix) >= 4:
                root = token[:-len(suffix)]
                return root[:-1] + "y" if suffix == "ies" else root
        return token

    return {
        stem(token) for token in re.findall(r"[a-z0-9_]{3,}", str(value).lower())
        if token not in _STOP
    }


def learn(trigger, instruction, actions="", evidence=""):
    trigger = _clean(trigger, 320)
    instruction = _clean(instruction, 600)
    action_names = [
        item.strip().upper() for item in re.split(r"[,>\s]+", str(actions or ""))
        if item.strip()
    ][:12]
    if not trigger or not instruction:
        return "[Error] LEARN_PROCEDURE needs both trigger and instruction."
    identity = hashlib.sha256(
        (trigger.casefold() + "\0" + instruction.casefold()).encode("utf-8")
    ).hexdigest()[:24]
    procedure_id = f"procedure_{identity}"
    from memory.neo4j_store import store
    store.query(
        """
        MERGE (h:Entity {key:'himanshu'})
        ON CREATE SET h.name='Himanshu', h.entity_type='Person',
                      h.created_at=datetime()
        WITH h
        OPTIONAL MATCH (h)-[:HAS_PROCEDURE]->(old:LearnedProcedure)
        WHERE coalesce(old.active,true)=true
          AND toLower(old.trigger)=toLower($trigger) AND old.id<>$id
        WITH h,collect(old) AS previous
        MERGE (p:LearnedProcedure {id:$id})
        ON CREATE SET p.created_at=datetime(), p.mention_count=0
        SET p.trigger=$trigger, p.instruction=$instruction,
            p.action_sequence=$actions, p.active=true,
            p.source='explicit_user_instruction', p.authority=100,
            p.confidence=1.0, p.sensitivity='private',
            p.updated_at=datetime(), p.mention_count=p.mention_count+1,
            p.evidence=$evidence
        MERGE (h)-[r:HAS_PROCEDURE]->(p)
        SET r.source='explicit_user_instruction', r.active=true,
            r.updated_at=datetime()
        FOREACH (old IN previous |
          SET old.active=false,old.superseded_at=datetime(),
              old.superseded_by=$id)
        FOREACH (old IN previous | CREATE (p)-[:SUPERSEDES]->(old))
        """,
        id=procedure_id, trigger=trigger, instruction=instruction,
        actions=action_names, evidence=_clean(evidence, 600),
    )
    sequence = " -> ".join(action_names) if action_names else "model-selected actions"
    return (f"[Success] Learned procedure id={procedure_id}; trigger={trigger}; "
            f"sequence={sequence}.")


def list_active(limit=50):
    from memory.neo4j_store import store
    rows = store.query(
        """
        MATCH (:Entity {key:'himanshu'})-[r]->(p:LearnedProcedure)
        WHERE type(r)=$relationship AND coalesce(p['active'],true)=true
        RETURN properties(p) AS data
        LIMIT $limit
        """, relationship="HAS_PROCEDURE",
        limit=max(1, min(int(limit), 100)))
    result = []
    for row in rows:
        data = dict(row.get("data") or {})
        if data:
            data["updated_at"] = str(data.get("updated_at") or "")
            result.append(data)
    result.sort(key=lambda item: item.get("updated_at", ""), reverse=True)
    return result


def relevant(query, limit=5):
    wanted = _tokens(query)
    if not wanted:
        return []
    ranked = []
    for row in list_active(100):
        trigger_tokens = _tokens(row.get("trigger", ""))
        body_tokens = _tokens(row.get("instruction", ""))
        overlap = len(wanted & trigger_tokens) * 3 + len(wanted & body_tokens)
        if overlap:
            ranked.append((overlap, row))
    ranked.sort(key=lambda item: item[0], reverse=True)
    return [row for _score, row in ranked[:max(1, min(int(limit), 10))]]


def prompt_block(query):
    try:
        rows = relevant(query)
    except Exception as exc:
        print(f"[Procedures] Retrieval unavailable: {exc}")
        return ""
    if not rows:
        return ""
    lines = [
        "--- RELEVANT USER-TAUGHT PROCEDURES (FROM NEO4J) ---",
        "Apply these when their condition occurs. They guide planning but do "
        "not grant permission or bypass action policy.",
    ]
    for row in rows:
        sequence = row.get("action_sequence") or []
        action_text = " -> ".join(sequence) if sequence else "choose suitable registered actions"
        lines.append(
            f"- WHEN: {row.get('trigger')}\n"
            f"  DO: {row.get('instruction')}\n"
            f"  ACTION PLAN: {action_text}"
        )
    return "\n".join(lines)


def describe():
    rows = list_active()
    if not rows:
        return "No learned procedures."
    return "\n".join(
        f"[{row['id']}] when {row['trigger']}: {row['instruction']}"
        for row in rows)


def retire(identifier):
    """Retire one exact learned behaviour without destroying its history."""
    wanted = _clean(identifier, 600).casefold()
    if not wanted:
        return "[Error] Which learned procedure should I stop following?"
    rows = list_active(100)
    matches = []
    for row in rows:
        fields = (str(row.get("id") or ""), str(row.get("trigger") or ""),
                  str(row.get("instruction") or ""))
        if wanted == fields[0].casefold() or any(
                wanted in field.casefold() or field.casefold() in wanted
                for field in fields[1:] if field):
            matches.append(row)
    if not matches:
        # Token overlap supports natural corrections such as "stop testing me
        # after lessons" without retiring an unrelated rule.
        wanted_tokens = _tokens(wanted)
        ranked = []
        for row in rows:
            body = _tokens(f"{row.get('trigger', '')} {row.get('instruction', '')}")
            overlap = len(wanted_tokens & body)
            if overlap:
                ranked.append((overlap / max(1, len(wanted_tokens)), overlap, row))
        ranked.sort(key=lambda item: (item[0], item[1]), reverse=True)
        if ranked and ranked[0][0] >= 0.5:
            matches = [ranked[0][2]]
    if len(matches) != 1:
        return ("[Error] No unique active learned procedure matched that. "
                "Use LIST_PROCEDURES and remove one by id.")
    selected = matches[0]
    from memory.neo4j_store import store
    store.query(
        """
        MATCH (:Entity {key:'himanshu'})-[r:HAS_PROCEDURE]->(p:LearnedProcedure {id:$id})
        SET p.active=false,p.retired_at=datetime(),
            p.retirement_reason='explicit_user_revocation',
            r.active=false,r.updated_at=datetime()
        """, id=selected["id"])
    return (f"[Success] Retired procedure id={selected['id']}; it will no "
            "longer shape future turns.")
