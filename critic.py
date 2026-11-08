# -*- coding: utf-8 -*-
"""
critic.py — VANTA's verification layer.

The executor grading its own work is not verification. It reads its own
narration back as if it were evidence, and a model that has just written
"I've added the calendar event" will agree that the calendar event exists.

This module judges the task against ACTION RECEIPTS instead: the structured
records of what the tools actually returned. Prose is explicitly not evidence.

It answers one of three things:

    PASS         — the receipts show the deliverable exists.
    FAIL         — the receipts show it does not, and names what is missing.
    UNVERIFIABLE — nothing observable could settle it.

UNVERIFIABLE matters as much as the other two. The old loop had no way to say
"this cannot be checked", so a plan with an unobservable success criterion
(the planner likes to write things like "Himanshu acknowledges the update")
spun until the step budget ran out and then died without answering. A task
that cannot be verified is reported honestly rather than retried forever.
"""

import json
import re

MAX_REVIEWS = 2

# Criteria phrased about someone's inner state can never be settled by a tool
# receipt. They are treated as unverifiable rather than as failures.
_UNOBSERVABLE = re.compile(
    r"\b(acknowledg\w*|understand\w*|satisf\w*|happy|aware|agrees?|feels?|"
    r"appreciat\w*|realis\w*|realiz\w*|trusts?|confident)\b",
    re.IGNORECASE,
)

CRITIC_PROMPT = """\
You are verifying whether a task was actually completed. You are not the agent \
that performed it and you have no stake in the answer.

GOAL: {goal}
DELIVERABLE: {deliverable}
ORIGINAL USER REQUEST: {request}
REQUIRED EVIDENCE:
{criteria}

ACTION RECEIPTS (the only ground truth; these are what the tools returned):
{receipts}

WHAT THE AGENT SAID IT DID (this is a claim, NOT evidence):
{claim}

COMPILED REPORT CONTENT (a content artifact, not an assertion that its claims are true):
{report_artifacts}

Judge only from the receipts. An agent statement with no matching receipt is an \
unsupported claim. A receipt marked [FAILED] did not happen, whatever the agent \
said about it. A receipt marked [OK] did happen, and you must not ask for it to \
be redone or re-checked.

Return exactly one JSON object:
{{
  "verdict": "pass" | "fail" | "unverifiable",
  "reason": "one sentence, specific, citing the receipt or its absence",
  "missing": ["each required piece of evidence that no receipt supports"],
  "next_action": "the single most useful concrete action to take next, or null"
}}

Use "pass" when the receipts support the deliverable.
Use "fail" when a receipt contradicts it, or a required action never ran.
Use "unverifiable" when no possible receipt could settle it — for example when \
the criterion describes what a person thinks or feels, or the deliverable was a \
spoken answer rather than a change to something. Do not use "fail" for these.

Receipt rules:
- A successful create/update receipt returned by the service is authoritative
  evidence of that write. Do not demand a second read merely to prove it again.
- The receipt list is the complete action ledger for this task. A preservation
  criterion (leave some unrelated object unchanged) is satisfied when there is
  no successful mutation of that object and no receipt contradicts it.
- Judge only requirements in the goal, deliverable, and listed criteria. Never
  invent a stronger verification mechanism than the task requires.
- REPORT_PREPARED proves that the attached report content was compiled for the
  in-app report surface. A report shown in that surface does not need a .md,
  .docx, PDF or shell/file-write receipt unless the original user requested a
  file/export. The word durable does not itself request a file export.
- Report compilation does not verify research claims. Resolve missing factual
  evidence by reading appropriate sources, or qualify unsupported claims; saving
  the same text into a file supplies no factual corroboration.
"""


class Verdict:
    def __init__(self, data=None):
        data = data or {}
        raw = str(data.get("verdict", "") or "").strip().lower()
        self.verdict = raw if raw in {"pass", "fail", "unverifiable"} else "unverifiable"
        self.reason = " ".join(str(data.get("reason", "") or "").split())[:400]
        missing = data.get("missing") or []
        self.missing = [
            " ".join(str(m).split())[:200] for m in missing if str(m or "").strip()
        ][:6] if isinstance(missing, list) else []
        nxt = data.get("next_action")
        self.next_action = " ".join(str(nxt).split())[:300] if nxt else None

    @property
    def passed(self):
        return self.verdict == "pass"

    @property
    def failed(self):
        return self.verdict == "fail"

    def __repr__(self):
        return f"<Verdict {self.verdict}: {self.reason[:60]!r}>"


def observable_criteria(criteria):
    """Split receipt-verifiable criteria from claims about inner state.

    Metadata such as an end time, timezone, or duration is observable when a
    provider receipt exposes it. Filtering those words here hid incomplete
    receipts and made verification depend on hard-coded phrases.
    """
    observable, unobservable = [], []
    for item in criteria or []:
        if _UNOBSERVABLE.search(item):
            unobservable.append(item)
        else:
            observable.append(item)
    return observable, unobservable


def format_receipts(receipts):
    """Render the task's accumulated action receipts for the critic."""
    if not receipts:
        return "(no actions were executed during this task)"
    lines = []
    for item in receipts:
        state = "OK" if item.get("ok") else "FAILED"
        output = " ".join(str(item.get("output", "")).split())[:400]
        lines.append(f"[{state}] {item.get('type')}: {output}")
    return "\n".join(lines[-40:])


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
                try:
                    return json.loads(text[start:i + 1])
                except Exception:
                    return None
    return None


def review(plan, receipts, claim_text, call_llm_fn):
    """Judge task completion from receipts. Never raises."""
    # A schedule, explanation, recommendation or other response whose planned
    # deliverable is the response itself is evidenced by the delivered text,
    # not by an external tool. Sending it to a receipt-only critic made a good
    # answer fail, then caused the executor to invent a file-writing task.
    if getattr(plan, "output_only", False) and not receipts:
        return Verdict({
            "verdict": "unverifiable",
            "reason": "The planned deliverable is the response itself; no external action receipt is required.",
        })
    criteria = list(plan.success_criteria or [])
    observable, unobservable = observable_criteria(criteria)

    # Nothing observable was ever required, and nothing was executed: there is
    # no fact of the matter to check. Asking the model would only invite a
    # fabricated justification.
    if not observable and not receipts:
        return Verdict({
            "verdict": "unverifiable",
            "reason": "No executable evidence was required or produced for this task.",
        })

    criteria_text = "\n".join(f"  - {c}" for c in observable) or f"  - {plan.deliverable}"
    if unobservable:
        criteria_text += "\n(Criteria about what a person thinks or feels were excluded as unverifiable.)"

    prompt = CRITIC_PROMPT.format(
        goal=plan.goal or "(unstated)",
        deliverable=plan.deliverable or "(unstated)",
        criteria=criteria_text,
        receipts=format_receipts(receipts),
        claim=" ".join((claim_text or "(nothing said)").split())[:1500],
        request=next((str(r.get('original_request')) for r in receipts or [] if r.get('original_request')), '(not separately recorded)'),
        report_artifacts='\n\n'.join(
            'TITLE: '+str(r['report_artifact'].get('title', ''))+'\nSTATE: prepared, delivery follows review\n'
            +str(r['report_artifact'].get('source', ''))[:18000]
            for r in receipts or [] if isinstance(r.get('report_artifact'), dict)) or '(none)',
    )
    messages = [
        {"role": "system", "content": "You are a strict verifier. You output only JSON."},
        {"role": "user", "content": prompt},
    ]
    try:
        raw = call_llm_fn(messages, task_level="critic")
    except TypeError:
        try:
            raw = call_llm_fn(messages)
        except Exception as exc:
            print(f"[Critic] Review call failed: {exc}")
            return Verdict({"verdict": "unverifiable", "reason": "The verifier could not be reached."})
    except Exception as exc:
        print(f"[Critic] Review call failed: {exc}")
        return Verdict({"verdict": "unverifiable", "reason": "The verifier could not be reached."})

    data = _extract_json(raw)
    if not data:
        print("[Critic] Could not parse verdict JSON.")
        return Verdict({"verdict": "unverifiable", "reason": "The verifier returned no usable judgement."})

    verdict = Verdict(data)
    print(f"[Critic] {verdict}")
    return verdict


def correction_prompt(verdict, plan):
    """The message handed back to the executor after a failed review."""
    lines = [
        "VERIFICATION FAILED. An independent check of your action receipts could not "
        f"confirm the deliverable: {plan.deliverable or plan.goal}",
        f"Reason: {verdict.reason}",
    ]
    if verdict.missing:
        lines.append("Unsupported by any receipt:")
        lines.extend(f"  - {item}" for item in verdict.missing)
    if verdict.next_action:
        lines.append(f"Most useful next step: {verdict.next_action}")
    lines.append(
        "Do not repeat any action whose receipt is already marked [OK]; those succeeded. "
        "Emit the specific ACTION that produces the missing evidence. If no action can "
        "produce it, say so plainly to Himanshu and output <DONE /> without claiming success."
    )
    return "\n".join(lines)
