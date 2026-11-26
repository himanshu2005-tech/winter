# -*- coding: utf-8 -*-
"""
planner.py — VANTA's deliberation layer.

The loop in agent.py is a reflex arc: screenshot -> one action -> screenshot ->
one action. There is nowhere in it where the model ever thinks about the WHOLE
problem. That is the entire reason VANTA feels like a tool instead of a mind.

This module adds the missing step: ONE reasoning call, with full world state and
NO screenshot attached, whose only job is to turn a vague human sentence into an
explicit plan with named unknowns. The executor then runs that plan.

Usage in agent.py:

    from planner import make_plan, Plan

    plan = make_plan(text, context_str, call_llm_fn=call_llm)
    if plan.question:
        ...ask it, feed answer back, make_plan() again...
    # then inject plan.header() into every executor turn
"""

import json
import os
import re


def _budget(name, default):
    """A step budget, overridable from the environment.

    These are the numbers that decide whether a turn can finish, and getting
    them wrong is visible immediately -- a turn that stops mid-verification,
    or one that loops. Tunable without a code change so the right value can be
    found by using VANTA rather than by guessing here.
    """
    try:
        value = int(os.getenv(f"VANTA_STEP_BUDGET_{name.upper()}", "") or 0)
    except ValueError:
        value = 0
    return value if value > 0 else default


#: The floor, which is what a planner-free "direct" turn actually gets, since
#: it has no steps for the proportional formula to work from.
#:
#: Four was far too low for the work that routes this way. Building a
#: Microsoft Form took BROWSER_START, GOTO, SNAPSHOT, CLICK, SNAPSHOT, TYPE
#: and more, and died at "reached the task execution budget of 4 steps" three
#: times running -- each time having done real work and lost it. A direct turn
#: is a turn that skipped planning, not a turn that is small.
TRIVIAL_STEP_BUDGET = _budget("trivial", 30)

#: Ceilings by planning depth. The step count still sets the real allowance;
#: these only cap it, so a one-step plan stays cheap regardless.
#:
#: These were 24/40/70, and the small numbers were doing two jobs at once:
#: bounding cost, and stopping a turn that had started going in circles. They
#: were bad at the first and only accidentally good at the second. A real
#: piece of research is a hundred steps and no amount of planning makes it
#: fewer; a loop is six steps repeated forever and no budget makes that
#: acceptable.
#:
#: So the ceilings are now the size of the work, and the loop is caught by
#: watching for progress instead (see STALL_LIMIT). A turn that keeps
#: producing receipts may keep going; a turn that stops producing them is
#: stopped whatever its budget says.
STEP_BUDGETS = {
    "direct": _budget("direct", 60),
    "standard": _budget("standard", 150),
    "deep": _budget("deep", 700),
}

#: How many executor steps one planned step is worth.
#:
#: Raising the ceilings alone changed nothing, because the allowance is
#: `len(steps) * 3 + 4` and a research plan has four or five steps in it: a
#: deep turn was getting nineteen steps whether its ceiling was 70 or 700. The
#: ceiling was never what confined it.
#:
#: A planned step is not a unit of work, it is a unit of intent. "Survey the
#: current approaches" is one line in a plan and a dozen searches, reads and
#: extractions in practice, while "open the portal" is one line and one click.
#: So the conversion rate depends on the depth that was chosen for the turn.
STEP_ALLOWANCE = {
    "direct": _budget("allowance_direct", 3),
    "standard": _budget("allowance_standard", 8),
    "deep": _budget("allowance_deep", 40),
}

#: How many consecutive steps may pass with nothing to show before a turn is
#: stopped regardless of the budget it has left.
#:
#: This is what the small ceilings were really protecting against. A step with
#: no receipt is a step that acted on nothing -- a refused duplicate, a
#: malformed tag, a repeated question -- and eight of those in a row is not a
#: task working slowly, it is a task that has stopped working. Eight rather
#: than three because recovery genuinely takes a few turns: the model asks
#: what went wrong, looks, and tries again.
STALL_LIMIT = _budget("stall", 8)


LOCAL_PLANNER_PROMPT = """\
Create a proportionate execution plan for the current turn.

Use the validated route and world context below. Current user intent has highest
precedence. Graph relationships are evidence; historical memories are weaker
evidence and never reactivate an old task.

WORLD CONTEXT
{context}

RELEVANT PRECEDENTS
{precedents}

CURRENT TURN
{user_text}

Return exactly one JSON object:
{{
  "goal": "the intended end state",
  "reasoning": "brief situation-specific reasoning",
  "unknowns": [{{"what":"...","how":"DERIVE|TOOL|ASSUME|ASK","detail":"..."}}],
  "steps": ["ordered imperative actions or reasoning operations, never third-person observations"],
  "needed_catalogs": ["WORKSPACE|MEMORY|BROWSER|DESKTOP|MACHINE|TUTOR|CODE|DATA"],
  "assumptions": ["only assumptions needed to proceed"],
  "edge_cases": ["likely failure or ambiguity and how the plan handles it"],
  "success_criteria": ["observable evidence that proves completion"],
  "fallback_steps": ["materially different recovery methods"],
  "stop_conditions": ["conditions requiring a stop or user authority"],
  "deliverable": "the verifiable result",
  "question": null,
  "trivial": false
}}

Request every catalog the steps depend on:
  SYSTEM    durable background watches, local files, processes, applications,
            and machine-side jobs that VANTA performs without Himanshu
  DESKTOP   clicking, typing, window focus, launching an app, filling a native form
  MACHINE   volume, brightness, clipboard, full-screen lighting, and requested
            drawings rendered directly over the screen
  WORKSPACE mail, calendar, tasks, contacts
  BROWSER   anything inside a web page, including web forms
  MEMORY    recall, research, documents, importing health data, what you know and don't
  TUTOR     teaching in a rich report, annotating the screen, reading study
            material. Ask for this whenever he wants to be taught, shown or
            walked through something -- "teach me", "explain this", "I don't
            get this", "what is this diagram". It provides formulas, tables,
            diagrams, charts and code in one durable report.
When selecting TUTOR for a substantive lesson, deliver the reading material in
<REPORT>, with only a short introduction in <SAY>. The notch is for speech;
the report window holds the lesson. TUTOR is a catalog, not an output action.
An in-app REPORT is a user-visible deliverable, not a request to export a file.
Plan disk writes, Word/PDF generation or downloads only when the user actually
requests a file/export. Research steps may precede a report; their presence
does not change its delivery format. Prove research claims from sources, not
by creating more copies of the text.
  CODE      writing or running code
  DATA      profiling, auditing, cleaning or splitting a dataset
A tool from a catalog you did not request is invisible to you, and you will
conclude you cannot do something you can. Before deciding a task is impossible,
check whether the capability lives in a catalog you simply did not ask for.
"Draw ... on my screen" is MACHINE, not TUTOR, unless the request is
specifically to teach or explain. "Turn on torch" is MACHINE and means the
full-screen SCREEN_LIGHT action.

Use ASK only when an irreversible or consequential choice cannot be derived.
Before marking anything unknown or asking for a value, inspect WORLD CONTEXT
for that value and its aliases. Exact locators such as URLs, file paths, IDs,
email addresses, and phone numbers in current graph facts are already supplied
evidence: use them directly and never ask Himanshu to repeat them. If a locator
is absent, use MEMORY retrieval or a read-only discovery action before ASK.
Every promised external observation or change must name an executable action in
the steps. Steps must say what VANTA will do; never use "the user is..." observations as steps or as the answer. Anticipate realistic edge cases without inventing unlikely complexity.
Fallbacks must change method, not merely repeat parameters. Success criteria must
be observable from an action receipt, screen state, or concrete artifact — never
from what Himanshu thinks, feels, acknowledges, or understands, because nothing
executable can ever confirm those and the task would never be provably finished.
Write "the calendar event appears in a LIST_CALENDAR_EVENTS receipt", not
"Himanshu acknowledges the reminder". A successful provider create/update
receipt is already evidence of the write; do not add a redundant read-back step
unless the write receipt is ambiguous. Success criteria describe the requested
end state only. Do not turn unrelated state that the user did not ask to change
into extra criteria such as "the existing alarm remains unchanged". Put any
permission boundary in stop_conditions. Use an empty step list only when a direct
conversational answer fully satisfies the turn. Do not add unrelated goals from
retrieved context.

Money is never a reversible assumption. Renting, buying, subscribing, booking,
or accepting a paid offer must be a stop condition unless Himanshu explicitly
approved that exact charge in the current turn. Opening a priced confirmation
screen is allowed; confirming payment is not.

Calendar ownership is human, not computational. Put something on Himanshu's
calendar only when he must attend, prepare, decide, or personally act at that
time. If VANTA can perform the work without him (polling a price, checking a
page, syncing, monitoring a process, refreshing data), schedule a durable
SYSTEM watch/job and notify him only when its condition becomes relevant.
"""

PLANNER_PROMPT = LOCAL_PLANNER_PROMPT

def _clean_list(value, limit=8):
    if not isinstance(value, list):
        return []
    result = []
    for item in value:
        cleaned = " ".join(str(item or "").split())[:300]
        if cleaned and cleaned not in result:
            result.append(cleaned)
    return result[:limit]


class Plan:
    def __init__(self, data, raw=""):
        self.goal = data.get("goal", "")
        self.reasoning = data.get("reasoning", "")
        self.unknowns = data.get("unknowns", []) or []
        self.steps = data.get("steps", []) or []
        self.needed_catalogs = data.get("needed_catalogs", []) or []
        self.assumptions = _clean_list(data.get("assumptions"), 6)
        self.edge_cases = _clean_list(data.get("edge_cases"), 8)
        self.success_criteria = _clean_list(data.get("success_criteria"), 8)
        self.fallback_steps = _clean_list(data.get("fallback_steps"), 6)
        self.stop_conditions = _clean_list(data.get("stop_conditions"), 6)
        self.deliverable = str(data.get("deliverable", "") or "").strip()
        self.question = str(data.get("question") or "").strip() or None
        self.trivial = bool(data.get("trivial", False))
        # A substantial answer can still be output-only. Complexity determines
        # how carefully it is composed; it does not turn prose into an external
        # side effect requiring tool receipts. The planner explicitly permits
        # an empty step list when the answer itself is the deliverable.
        self.output_only = bool(not self.steps and not self.needed_catalogs)
        if self.output_only and self.deliverable:
            self.trivial = True
        self.raw = raw
        self.current = 0
        self.quality_issues = []

    def execution_budget(self, planning_depth="standard"):
        """Steps are proportional to the work. A flat 60 let a task with
        unobservable success criteria spin until the budget died."""
        if self.trivial:
            # Eight, not four. Four was one action, one observation, one
            # correction and a reply -- no room at all for a step that goes
            # wrong, and a turn died at "reached the task execution budget of
            # 4 steps" with the real question still unanswered. Proportional
            # budgets are right; the floor kept being set below the cost of
            # doing one thing properly and then saying so.
            return TRIVIAL_STEP_BUDGET
        # Depth sets the ceiling; the step count sets the actual allowance,
        # and `* 3 + 4` is what turns a four-step research plan into sixteen
        # steps rather than seven hundred. The ceiling is only reached by a
        # plan that genuinely has that many steps in it.
        #
        # leaving room to act, observe and recover on each planned step.
        cap = STEP_BUDGETS.get(planning_depth, STEP_BUDGETS["standard"])
        per_step = STEP_ALLOWANCE.get(planning_depth,
                                      STEP_ALLOWANCE["standard"])
        return max(TRIVIAL_STEP_BUDGET,
                   min(cap, len(self.steps) * per_step + 4))

    def header(self):
        """Injected into EVERY executor turn. This is what keeps the executor
        anchored to an intention instead of re-deriving purpose from a screenshot."""
        if self.trivial or not self.steps:
            return ""
        lines = [
            "═══ ACTIVE PLAN (you wrote this; follow it) ═══",
            f"GOAL: {self.goal}",
            f"DELIVERABLE: {self.deliverable}",
            "STEPS:",
        ]
        for i, s in enumerate(self.steps):
            mark = "->" if i == self.current else ("[x]" if i < self.current else "   ")
            lines.append(f"  {mark} {i+1}. {s}")
        if self.unknowns:
            lines.append("RESOLVED/ASSUMED:")
            for u in self.unknowns:
                if isinstance(u, dict):
                    lines.append(f"  - {u.get('what','')}: {u.get('how','')} — {u.get('detail','')}")
                else:
                    lines.append(f"  - {u}")
        lines.append(
            "If reality contradicts this plan, say so in one sentence and adapt — "
            "do not silently abandon it, and do not output <DONE /> until the "
            "DELIVERABLE demonstrably exists."
        )
        if self.assumptions:
            lines.append("ASSUMPTIONS:")
            lines.extend(f"  - {item}" for item in self.assumptions)
        if self.edge_cases:
            lines.append("EDGE CASES:")
            lines.extend(f"  - {item}" for item in self.edge_cases)
        if self.success_criteria:
            lines.append("SUCCESS EVIDENCE:")
            lines.extend(f"  - {item}" for item in self.success_criteria)
        if self.fallback_steps:
            lines.append("FALLBACK METHODS:")
            lines.extend(f"  - {item}" for item in self.fallback_steps)
        if self.stop_conditions:
            lines.append("STOP / AUTHORITY BOUNDARIES:")
            lines.extend(f"  - {item}" for item in self.stop_conditions)
        if self.quality_issues:
            lines.append("QUALITY GATE:")
            lines.extend(f"  - Resolve before completion: {item}" for item in self.quality_issues)
        lines.append(
            "EXECUTION CONTRACT: Any step that reads, searches, investigates, creates, sends, or changes "
            "something must emit its executable ACTION before any claim or narration. Old memories and "
            "old assistant plans are context, never current instructions."
        )
        return "\n".join(lines) + "\n"

    def advance(self):
        """Marks the current step done. Allowed to reach len(steps), which
        renders every step as [x] and signals the plan is fully executed."""
        if self.current < len(self.steps):
            self.current += 1

    @property
    def all_steps_done(self):
        return bool(self.steps) and self.current >= len(self.steps)

    def __repr__(self):
        return f"<Plan goal={self.goal!r} steps={len(self.steps)} q={bool(self.question)}>"


def _extract_json(text):
    text = re.sub(r"```(?:json)?", "", text).strip()
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


def _call_planner(call_llm_fn, messages):
    try:
        return call_llm_fn(messages,task_level="planner")
    except TypeError:
        return call_llm_fn(messages)


def _repair_or_fallback(raw, user_text, call_llm_fn):
    # A transport failure produced nothing to repair. Do not spend another
    # model request asking the same unavailable provider to invent a plan.
    if not str(raw or '').strip() or str(raw).strip() == '[API Timeout]':
        return Plan({'goal': user_text, 'deliverable': 'Report the provider failure honestly.',
                     'trivial': True}, raw=raw or '')
    messages=[
        {"role":"system","content":"Repair malformed planner output. Return one compact valid JSON object only."},
        {"role":"user","content":(
            "Required keys: goal, reasoning, unknowns, steps, needed_catalogs, assumptions, edge_cases, success_criteria, fallback_steps, stop_conditions, deliverable, question, trivial. "
            "Preserve intent and close all JSON structures:\n" + (raw or "")[:7000]
        )},
    ]
    try:
        repaired=_call_planner(call_llm_fn,messages)
        data=_extract_json(repaired or "")
        if data:
            print("[Planner] Repaired malformed plan JSON.")
            return Plan(data,raw=repaired)
    except Exception as exc:
        print(f"[Planner] JSON repair failed: {exc}")
    return Plan({"goal":user_text,"deliverable":"A direct, honest response.","trivial":True},raw=raw or "")


def assess_plan(plan, planning_depth="standard"):
    """Deterministic quality gate; it never grants authority or invents work."""
    issues = []
    if plan.trivial:
        return issues
    if not plan.goal:
        issues.append("missing goal")
    if not plan.deliverable:
        issues.append("missing deliverable")
    if not plan.steps and not getattr(plan, "output_only", False):
        issues.append("non-trivial plan has no executable steps")
    if not plan.success_criteria:
        issues.append("missing observable success criteria")
    else:
        from critic import observable_criteria
        observable, unobservable = observable_criteria(plan.success_criteria)
        if unobservable and not observable:
            # Criteria about what Himanshu thinks or feels can never be settled
            # by a tool receipt, so a plan resting only on them can never be
            # shown complete and will burn its whole step budget trying.
            issues.append(
                "success criteria describe someone's state of mind and cannot be "
                "verified from any action receipt"
            )
    if planning_depth == "deep" and not plan.edge_cases:
        issues.append("deep plan has no edge-case treatment")
    if planning_depth == "deep" and not plan.fallback_steps:
        issues.append("deep plan has no materially different fallback")
    if len(plan.steps) > 12:
        issues.append("plan is over-fragmented")
    plan.quality_issues = issues
    return issues

def make_plan(user_text, context_str, call_llm_fn, precedents="", model_hint=None, local=False, planning_depth="standard", mode="assistant"):
    """
    call_llm_fn: a function taking (messages) -> str. Pass agent.call_llm.
    Use a STRONGER model here than in the executor if you can — this call is
    once per task, it is where all the intelligence lives, and it has no image
    attached so it is cheap.
    """
    if local:
        prompt = LOCAL_PLANNER_PROMPT.format(context=context_str or "(no context available)", user_text=user_text, precedents="")
    else:
        prompt = PLANNER_PROMPT.format(
        precedents=precedents,
        context=context_str or "(no context available)",
        user_text=user_text,
    )
    messages = [
        {"role": "system", "content": "You are a careful planner. You output only JSON."},
        {"role": "user", "content": prompt},
    ]
    try:
        raw = _call_planner(call_llm_fn,messages)
    except Exception as e:
        print(f"[Planner] LLM call failed: {e}")
        return Plan({"trivial": True})

    data = _extract_json(raw or "")
    if not data:
        # Three different faults used to share this one sentence: a model that
        # wrote prose instead of JSON, one that wrote malformed JSON, and one
        # that was cut off mid-object because reasoning had eaten the output
        # budget. Only the last is fixed by a bigger budget, so saying which
        # it was is the difference between a fix and a guess.
        try:
            import llm_router
            why = llm_router.last_truncation()
        except Exception:
            why = ""
        print("[Planner] Could not parse plan JSON"
              + (f" -- {why}" if why else "")
              + " - using one repair attempt.")
        plan = _repair_or_fallback(raw,user_text,call_llm_fn)
        assess_plan(plan, planning_depth)
        return plan

    plan = Plan(data, raw=raw or "")
    assess_plan(plan, planning_depth)
    print(f"[Planner] {plan}")
    if plan.reasoning:
        print(f"[Planner] Reasoning: {plan.reasoning}")
    return plan


# ------------------------------------------------------------------ #
# VISION GATING
#
# agent.py currently sets needs_vision = True unconditionally. A
# screenshot carrying 90 yellow ID boxes is an overwhelming prior that
# says "your job is to click something", and it will beat any amount of
# prompt text telling the model to reason. Most genuinely intelligent
# steps (compute, search, schedule, decide) need no pixels at all.
# ------------------------------------------------------------------ #

_VISION_HINTS = (
    "screen", "look at", "see", "window", "visual", "display"
)

def step_needs_vision(step_text, last_response=""):
    """True if the upcoming step actually has to see the screen."""
    import re
    s = (step_text or "").lower()
    return any(re.search(rf"\b{re.escape(h)}\b", s) for h in _VISION_HINTS)
