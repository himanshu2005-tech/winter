# -*- coding: utf-8 -*-
"""
inference.py — VANTA forming its own ideas about Himanshu.

Everything in the graph until now arrived because somebody said it. This module
is the missing producer for the `derived_inference` tier the schema already
anticipated: it reads what is known and proposes claims that no single turn
ever contained.

Three kinds of proposal:

  CONCEPT   two or more entities are really one idea, and deserve a shared name
  PATTERN   a regularity across facts that nobody has stated outright
  LINK      a relationship implied by two others

Two rules keep this from turning into self-delusion:

1. It reads ONLY explicitly stated facts. Never its own output. An engine that
   infers from its inferences compounds confidence on nothing, and after a few
   passes it is certain of things that were always guesses.
2. Nothing it writes is retrievable as a fact. Proposals land as
   `pending_confirmation` and stay invisible to normal recall until Himanshu
   confirms them. A rejected proposal is tombstoned, not deleted, so the same
   idea is not raised twice.

Proposals surface only when asked for (REVIEW_INSIGHTS). VANTA does not
interrupt to share a hunch.
"""

import json
import os
import re
import threading
import time

from memory.neo4j_store import store

MIN_FACTS_TO_BOTHER = 25
MAX_PROPOSALS_PER_PASS = 6

INFERENCE_PROMPT = """\
Below is everything VANTA knows about Himanshu, stated as facts he or his
accounts provided. Your task is to notice what these facts imply that none of
them says outright.

KNOWN FACTS
{facts}

ALREADY PROPOSED (do not repeat any of these, in any wording)
{already}

Propose at most {limit} genuinely new claims. Prefer few and good over many.

Three kinds are useful:
  CONCEPT - several entities are the same underlying idea and should share one
            name. Example shape: subject="relationships", predicate="SAME_CONCEPT_AS",
            object="good relationships".
  PATTERN - a regularity across several facts. The subject is normally Himanshu.
  LINK    - a relationship implied by two others, e.g. two people who share an
            institution.

Return exactly one JSON object:
{{
  "proposals": [
    {{
      "kind": "CONCEPT|PATTERN|LINK",
      "subject": "...",
      "predicate": "SCREAMING_SNAKE_CASE",
      "object": "...",
      "rationale": "which specific facts above imply this, in one sentence",
      "confidence": 0.0
    }}
  ]
}}

Rules:
- subject, predicate and object must ALL be non-empty strings. A claim with a
  null object says nothing and will be discarded.
- The predicate is the relationship; the object is the thing. Write
  predicate="CARES_ABOUT", object="coding" — never predicate="CARES_ABOUT_CODING"
  with an empty object, which cannot be queried or corrected later.
- Output strict JSON: every value quoted unless it is a number, true, false or
  null. A bare word like SAME_CONCEPT_AS is not valid JSON.
- Every proposal must be supported by facts listed above. Cite them in the
  rationale. If you cannot point at the evidence, do not propose it.
- Do not restate a known fact in different words. That is not an inference.
- Do not infer anything about health, money, or relationships with named people
  unless the facts state it directly. A wrong guess there is worse than silence.
- confidence is your honest probability the claim is true, 0.0 to 1.0. Use
  values below 0.5 freely; a hesitant proposal is more useful than a confident
  wrong one.
- Return {{"proposals": []}} if nothing new is genuinely implied. This is a
  perfectly good answer and is better than inventing something.
"""


def _repair_json(fragment):
    """Fix the JSON mistakes models actually make.

    The engine once produced three sound proposals and discarded all of them
    because one value came back as `"predicate": SAME_CONCEPT_AS` — a bare
    identifier where a quoted string belongs. Re-asking costs a second model
    call and usually reproduces the same habit, so the cheap repairs are done
    here instead.
    """
    repaired = fragment
    # Python spellings first: quoting bare identifiers before this would turn
    # None into the string "None" rather than a null.
    repaired = re.sub(r':\s*None\s*(?=[,}\]])', ': null', repaired)
    repaired = re.sub(r':\s*True\s*(?=[,}\]])', ': true', repaired)
    repaired = re.sub(r':\s*False\s*(?=[,}\]])', ': false', repaired)
    # Bare identifiers used as values: SAME_CONCEPT_AS -> "SAME_CONCEPT_AS".
    # Real JSON literals must survive untouched.
    repaired = re.sub(
        r'(:\s*)(?!true\b|false\b|null\b)([A-Za-z_][A-Za-z0-9_]*)\s*(?=[,}\]])',
        r'\1"\2"', repaired)
    # Trailing commas before a close.
    repaired = re.sub(r',\s*(?=[}\]])', '', repaired)
    return repaired


def _extract_json(text):
    text = re.sub(r"```(?:json)?", "", text or "").strip()
    start = text.find("{")
    if start == -1:
        return None
    depth, in_str, esc = 0, False, False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                fragment = text[start:i + 1]
                try:
                    return json.loads(fragment)
                except Exception:
                    pass
                try:
                    return json.loads(_repair_json(fragment))
                except Exception:
                    return None
    return None


def _format_facts(rows):
    from memory.orchestrator import _render_fact
    return "\n".join(_render_fact(r) for r in rows)


def run_inference_pass(call_llm_fn, limit=MAX_PROPOSALS_PER_PASS):
    """One pass over the graph. Returns the proposals actually recorded."""
    try:
        facts = store.explicit_facts(limit=400)
    except Exception as exc:
        print(f"[Inference] Could not read the graph: {exc}")
        return []
    if len(facts) < MIN_FACTS_TO_BOTHER:
        print(f"[Inference] Only {len(facts)} facts known; too early to infer anything.")
        return []

    try:
        already = store.proposed_claims()
    except Exception:
        already = set()
    already_text = "\n".join(
        f"- {s} {p} {o}" for s, p, o in list(already)[:60]
    ) or "(nothing proposed yet)"

    messages = [
        {"role": "system", "content": "You infer carefully and output only JSON."},
        {"role": "user", "content": INFERENCE_PROMPT.format(
            facts=_format_facts(facts), already=already_text, limit=int(limit))},
    ]
    try:
        # Its own stage, not the critic's. Sharing the critic's 1200-token
        # budget meant this pass truncated mid-reasoning every time and
        # returned nothing usable.
        raw = call_llm_fn(messages, task_level="inference")
    except TypeError:
        raw = call_llm_fn(messages)
    except Exception as exc:
        print(f"[Inference] Model call failed: {exc}")
        return []

    data = _extract_json(raw)
    if not data:
        print("[Inference] No usable JSON returned.")
        return []

    from memory.neo4j_store import _slug, canonical_predicate

    recorded = []
    for item in (data.get("proposals") or [])[:limit]:
        if not isinstance(item, dict):
            continue
        subject = str(item.get("subject", "")).strip()
        predicate = str(item.get("predicate", "")).strip()
        obj = str(item.get("object", "")).strip()
        rationale = str(item.get("rationale", "")).strip()
        if not (subject and predicate and obj and rationale):
            continue
        # The prompt asks the model not to repeat a proposal, but a prompt is
        # not an enforcement mechanism. A claim Himanshu already rejected must
        # not come back because the model forgot or disagreed.
        claim = (
            _slug(store.canonical_name(subject)),
            canonical_predicate(predicate),
            _slug(store.canonical_name(obj)),
        )
        if claim in already:
            print(f"[Inference] Skipped a repeat proposal: {subject} {predicate} {obj}")
            continue
        already.add(claim)
        try:
            confidence = float(item.get("confidence", 0.5))
        except (TypeError, ValueError):
            confidence = 0.5
        try:
            written = store.propose_inference(
                subject, predicate, obj, rationale,
                confidence=confidence,
                source_id=f"inference:{int(time.time())}",
            )
        except Exception as exc:
            print(f"[Inference] Could not record proposal: {exc}")
            continue
        if written:
            recorded.append(written)
            print(f"[Inference] Proposed ({item.get('kind','?')}, {confidence:.2f}): {written}")

    if not recorded:
        print("[Inference] Nothing new worth proposing.")
    auto_promote()
    return recorded


#: What it takes to be believed without being asked.
#:
#: Two conditions, because the first one alone is nearly worthless. The
#: confidence number arrives in the same JSON as the claim it describes, and
#: the engine attaches 0.99 to restatements of data it was handed -- "tracks
#: daily health metrics", from a step count appearing twice in its own input.
#: A threshold on that number promotes the vacuous along with the useful.
#:
#: The second condition is corroboration: the engine read the facts again, on
#: a later pass, and arrived at the same claim. That is evidence of a kind the
#: model cannot award itself.
AUTO_PROMOTE_CONFIDENCE = 0.90
AUTO_PROMOTE_SIGHTINGS = 2
AUTO_PROMOTE_LIMIT = 10


def auto_promote():
    """Believe the corroborated ones without interrupting him for them.

    Everything it promotes stays a derived inference, stays ineligible to
    authorize anything, and stays below a stated fact in authority -- so the
    worst case of a wrong promotion is a belief that colours a reply, not one
    that does something. And it is recorded as `auto_accepted` rather than
    `accepted`, so what VANTA decided on its own can be listed and undone
    separately from what Himanshu agreed to.
    """
    try:
        promoted = store.auto_promote_inferences(
            min_confidence=AUTO_PROMOTE_CONFIDENCE,
            min_sightings=AUTO_PROMOTE_SIGHTINGS,
            limit=AUTO_PROMOTE_LIMIT)
    except Exception as exc:
        print(f"[Inference] Could not promote proposals: {exc}")
        return []
    for row in promoted or ():
        print(f"[Inference] Believed without asking "
              f"({row['confidence']:.2f}, seen {row['sightings']}x): "
              f"{row['subject']} -{row['predicate']}-> {row['object']}")
    return list(promoted or ())


def format_pending(rows):
    """Render proposals for Himanshu to accept or dismiss."""
    if not rows:
        return "I haven't formed any new ideas about you that are waiting on you."
    from memory.neo4j_store import display_name
    lines = ["Here's what I've started to think, none of it confirmed:"]
    for i, row in enumerate(rows, 1):
        subject = display_name(row.get("subject"))
        obj = display_name(row.get("object"))
        predicate = str(row.get("predicate", "")).replace("_", " ").lower()
        lines.append(f"{i}. {subject} {predicate} {obj}  ({float(row.get('confidence',0.5)):.0%} sure)")
        if row.get("rationale"):
            lines.append(f"   because: {row['rationale']}")
    lines.append(
        "Confirm one with CONFIRM_INSIGHT (subject/predicate/object), or drop it "
        "with REJECT_INSIGHT. Until confirmed none of these are used as fact."
    )
    return "\n".join(lines)


_worker_started = False


def start_worker(call_llm_fn, is_busy, interval_seconds=3600):
    """Runs a pass on idle, spaced out. Never speaks; findings wait to be asked for."""
    global _worker_started
    if _worker_started:
        return
    _worker_started = True

    def loop():
        # Let the system finish booting before adding background model calls.
        time.sleep(300)
        while True:
            try:
                if not is_busy():
                    run_inference_pass(call_llm_fn)
            except Exception as exc:
                print(f"[Inference] Pass failed: {exc}")
            # What the graph implies, and what his separate worlds say about
            # each other. Separate try blocks: a failure in one must not cost
            # the others their pass, and none may cost the reflection above.
            base = os.path.dirname(os.path.abspath(__file__))
            for label, module_name in (("Deduction", "deduction"),
                                       ("Linking", "entity_linking")):
                try:
                    if not is_busy():
                        import importlib
                        importlib.import_module(module_name).run_pass(
                            base, call_llm_fn)
                except Exception as exc:
                    print(f"[{label}] Pass failed: {exc}")
            time.sleep(interval_seconds)

    thread = threading.Thread(target=loop, daemon=True, name="inference-worker")
    thread.start()
    print("[Inference] Background reflection worker started.")
