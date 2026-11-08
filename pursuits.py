# -*- coding: utf-8 -*-
"""
pursuits.py -- work that outlives a turn.

VANTA could already remember facts, hold deadlines, watch for conditions and
run background coding tasks. What it had nowhere to put was the ordinary shape
of most real work: an objective being pursued over days, made of steps, some
done and some not, with something currently in the way.

    commitments.py  something with a hard time and consequences if missed
    watches.py      a condition to observe and alert on
    followups.py    a human question after a completed event
    coding_tasks.py one background agent run
    life_model.py   goals, as reviewable facts
    workflows.py    how a kind of task is usually done

"Find me a flat." "Apply to these twenty roles." "Get through the syllabus by
December." None of those is a deadline, a condition, a single run, a fact or a
precedent, so none of them had a home. Every turn started over, and the only
record that work was in flight was whatever happened to be in the transcript.
That is the difference between an assistant that does tasks and one that
carries them.

FOUR RULES, EACH ONE A FAILURE AVOIDED
--------------------------------------
*A step is done when it has a receipt, not when something says it is.* This is
the defect that runs through this whole system -- a calendar event whose
receipt was a bare URL, a file save that silently did nothing, a typing action
reporting a length while a character was dropped. A to-do list that takes
"done" on trust would launder every one of those into apparent progress across
days instead of one turn. `advance` refuses to close a step without evidence.

*Progress is counted, never asserted.* "How far along is this" is answered by
counting steps in each state and showing the receipts, so it cannot drift from
what actually happened.

*Blocked is a state, with a reason and what would clear it.* The failure mode
of long work is silent stalling: VANTA blocked the same action four times in
one turn, burned 68,000 tokens, and learned a false lesson from it. A step that
cannot proceed says so, in a form someone can act on.

*A pursuit grants nothing.* Same rule as memory and workflows: this is a plan,
never an authorization. Every step still goes through the ordinary action
path -- authorization, schema, confirmation, receipts. A pursuit that could
authorize its own steps would be a way to pre-approve anything by writing it
down.
"""

import datetime
import json
import os
import tempfile
import threading
import uuid

_FILENAME = os.path.join("vault", "memory", "pursuits.json")
_LOCK = threading.RLock()

# --- states ---------------------------------------------------------- #
ACTIVE = "active"
BLOCKED = "blocked"
PAUSED = "paused"
DONE = "done"
ABANDONED = "abandoned"
PURSUIT_STATES = (ACTIVE, BLOCKED, PAUSED, DONE, ABANDONED)
OPEN_STATES = (ACTIVE, BLOCKED)

STEP_PENDING = "pending"
STEP_DONE = "done"
STEP_BLOCKED = "blocked"
STEP_SKIPPED = "skipped"
STEP_STATES = (STEP_PENDING, STEP_DONE, STEP_BLOCKED, STEP_SKIPPED)

#: How long an untouched pursuit waits before it is worth mentioning again.
#: Long enough not to nag, short enough that a week's silence is noticed.
STALE_AFTER_DAYS = 4

#: Caps. A pursuit with eighty steps is a project, and a prompt that carries
#: every pursuit in full is a prompt with no room for the work.
MAX_STEPS = 40
MAX_TITLE = 120
MAX_OPEN_PURSUITS = 12


class PursuitError(Exception):
    """Something was asked of a pursuit that cannot be done to it."""


def _now():
    return datetime.datetime.now().isoformat(timespec="seconds")


def _path(base_dir):
    path = os.path.join(base_dir, _FILENAME)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    return path


def _load(base_dir):
    try:
        with open(_path(base_dir), "r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, list) else []
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return []


def _save(base_dir, rows):
    """Write atomically. A half-written file loses every pursuit at once."""
    target = _path(base_dir)
    directory = os.path.dirname(target)
    handle = tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=directory, delete=False, suffix=".tmp")
    try:
        json.dump(rows, handle, ensure_ascii=False, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    finally:
        handle.close()
    os.replace(handle.name, target)


def _find(rows, identifier):
    """By id, or by an unambiguous prefix of the title."""
    needle = str(identifier or "").strip().lower()
    if not needle:
        return None
    for row in rows:
        if row.get("id", "").lower() == needle:
            return row
    matches = [row for row in rows
               if needle in row.get("title", "").lower()
               and row.get("state") in OPEN_STATES]
    return matches[0] if len(matches) == 1 else None


def _step(pursuit, reference):
    """One step, by its number (1-based, as shown) or by matching text."""
    steps = pursuit.get("steps", [])
    raw = str(reference or "").strip()
    if raw.isdigit():
        index = int(raw) - 1
        if 0 <= index < len(steps):
            return steps[index]
        return None
    needle = raw.lower()
    if not needle:
        return None
    for step in steps:
        if step.get("id", "").lower() == needle:
            return step
    matches = [step for step in steps if needle in step.get("what", "").lower()]
    return matches[0] if len(matches) == 1 else None


# ------------------------------------------------------------------ #
# Writing
# ------------------------------------------------------------------ #
def start(base_dir, title, why="", steps=()):
    """Begin pursuing something. Returns a receipt naming the id."""
    clean = " ".join(str(title or "").split())[:MAX_TITLE]
    if not clean:
        raise PursuitError("a pursuit needs a title saying what is wanted")
    with _LOCK:
        rows = _load(base_dir)
        open_now = [row for row in rows if row.get("state") in OPEN_STATES]
        for row in open_now:
            if row.get("title", "").lower() == clean.lower():
                return (f"[Success] Already pursuing '{clean}' "
                        f"[{row['id']}], started {row['created']}. "
                        f"{progress_line(row)}")
        if len(open_now) >= MAX_OPEN_PURSUITS:
            raise PursuitError(
                f"{len(open_now)} pursuits are already open, which is more "
                f"than anyone is actually pursuing. Close or pause one first "
                f"-- LIST_PURSUITS shows them.")
        pursuit = {
            "id": uuid.uuid4().hex[:8],
            "title": clean,
            "why": " ".join(str(why or "").split())[:400],
            "state": ACTIVE,
            "created": _now(),
            "updated": _now(),
            "steps": [],
            "log": [],
        }
        rows.append(pursuit)
        for what in steps:
            _append_step(pursuit, what)
        _note(pursuit, f"started: {clean}")
        _save(base_dir, rows)
    return (f"[Success] Pursuing '{clean}' [{pursuit['id']}] with "
            f"{len(pursuit['steps'])} step(s). {progress_line(pursuit)}")


def _append_step(pursuit, what):
    clean = " ".join(str(what or "").split())[:300]
    if not clean:
        return None
    if len(pursuit["steps"]) >= MAX_STEPS:
        raise PursuitError(
            f"a pursuit holds at most {MAX_STEPS} steps; this one is full. "
            f"Close the finished ones or start a second pursuit.")
    step = {
        "id": uuid.uuid4().hex[:6],
        "what": clean,
        "state": STEP_PENDING,
        "receipt": "",
        "reason": "",
        "updated": _now(),
    }
    pursuit["steps"].append(step)
    return step


def _note(pursuit, text):
    """Append to the pursuit's own history, newest last."""
    pursuit.setdefault("log", []).append({"at": _now(), "what": str(text)[:300]})
    del pursuit["log"][:-40]
    pursuit["updated"] = _now()


def plan(base_dir, identifier, steps):
    """Add steps to a pursuit."""
    wanted = [line for line in
              (str(step).strip() for step in steps) if line]
    if not wanted:
        raise PursuitError("no steps were given")
    with _LOCK:
        rows = _load(base_dir)
        pursuit = _find(rows, identifier)
        if pursuit is None:
            raise PursuitError(_unknown(rows, identifier))
        existing = {step["what"].lower() for step in pursuit["steps"]}
        added = []
        for what in wanted:
            if what.lower() in existing:
                continue
            step = _append_step(pursuit, what)
            if step is not None:
                added.append(step)
                existing.add(what.lower())
        _note(pursuit, f"planned {len(added)} step(s)")
        _save(base_dir, rows)
    if not added:
        return (f"[Success] Nothing new to add to '{pursuit['title']}' "
                f"[{pursuit['id']}]; those steps are already planned. "
                f"{progress_line(pursuit)}")
    return (f"[Success] Added {len(added)} step(s) to '{pursuit['title']}' "
            f"[{pursuit['id']}]. {progress_line(pursuit)}")


def advance(base_dir, identifier, step_reference, receipt="", blocked=""):
    """Record what happened to one step.

    A step closes only against evidence. `receipt` is what was actually
    produced -- a file path, a message id, a confirmation read back from
    somewhere. Without it the step stays open, because a pursuit that accepts
    "done" on trust turns one unverified action into days of apparent
    progress, and there is no later point at which anyone finds out.
    """
    with _LOCK:
        rows = _load(base_dir)
        pursuit = _find(rows, identifier)
        if pursuit is None:
            raise PursuitError(_unknown(rows, identifier))
        step = _step(pursuit, step_reference)
        if step is None:
            raise PursuitError(
                f"'{step_reference}' is not a step of '{pursuit['title']}'. "
                f"Its steps are:\n" + steps_text(pursuit))

        reason = " ".join(str(blocked or "").split())[:300]
        evidence = " ".join(str(receipt or "").split())[:600]
        if reason:
            step["state"] = STEP_BLOCKED
            step["reason"] = reason
            step["updated"] = _now()
            _note(pursuit, f"blocked: {step['what']} -- {reason}")
            pursuit["state"] = (BLOCKED if pursuit["state"] == ACTIVE
                                else pursuit["state"])
            _save(base_dir, rows)
            return (f"[Success] Step '{step['what']}' is blocked: {reason}. "
                    f"'{pursuit['title']}' [{pursuit['id']}] is waiting. "
                    f"{progress_line(pursuit)}")

        if not evidence:
            raise PursuitError(
                f"a step closes against evidence, not a claim. Say what "
                f"'{step['what']}' actually produced -- a path, an id, a "
                f"confirmation read back -- in receipt=\"...\", or say what "
                f"is in the way in blocked=\"...\".")

        step["state"] = STEP_DONE
        step["receipt"] = evidence
        step["reason"] = ""
        step["updated"] = _now()
        _note(pursuit, f"done: {step['what']} -- {evidence[:120]}")
        # One step clearing can unblock the pursuit, but only if nothing else
        # is still stuck.
        if pursuit["state"] == BLOCKED and not [
                item for item in pursuit["steps"]
                if item["state"] == STEP_BLOCKED]:
            pursuit["state"] = ACTIVE
            _note(pursuit, "no longer blocked")
        _save(base_dir, rows)
    return (f"[Success] Step '{step['what']}' of '{pursuit['title']}' "
            f"[{pursuit['id']}] is done: {evidence[:160]}. "
            f"{progress_line(pursuit)}")


def close(base_dir, identifier, outcome="", state=DONE):
    """Finish a pursuit, successfully or not."""
    if state not in (DONE, ABANDONED, PAUSED):
        raise PursuitError(f"a pursuit cannot be closed as {state!r}")
    with _LOCK:
        rows = _load(base_dir)
        pursuit = _find(rows, identifier)
        if pursuit is None:
            raise PursuitError(_unknown(rows, identifier))
        unfinished = [step for step in pursuit["steps"]
                      if step["state"] == STEP_PENDING]
        pursuit["state"] = state
        pursuit["outcome"] = " ".join(str(outcome or "").split())[:400]
        _note(pursuit, f"{state}: {pursuit['outcome'] or 'no detail given'}")
        _save(base_dir, rows)
    tail = ""
    if unfinished and state == DONE:
        # Said rather than hidden: closing a pursuit with steps still pending
        # is usually right (the goal was reached another way) and occasionally
        # a mistake, and only Himanshu can tell which.
        tail = (f" {len(unfinished)} step(s) were still pending and were left "
                f"unfinished.")
    return (f"[Success] '{pursuit['title']}' [{pursuit['id']}] is {state}."
            f"{tail} {progress_line(pursuit)}")


def _unknown(rows, identifier):
    open_rows = [row for row in rows if row.get("state") in OPEN_STATES]
    if not open_rows:
        return (f"there is no pursuit matching '{identifier}', and nothing is "
                f"currently being pursued. START_PURSUIT begins one.")
    names = ", ".join(f"{row['title']} [{row['id']}]" for row in open_rows[:6])
    return (f"there is no single pursuit matching '{identifier}'. Open "
            f"pursuits: {names}.")


# ------------------------------------------------------------------ #
# Reading
# ------------------------------------------------------------------ #
def counts(pursuit):
    """Steps by state. The basis of every progress claim made anywhere."""
    tally = {state: 0 for state in STEP_STATES}
    for step in pursuit.get("steps", []):
        tally[step.get("state", STEP_PENDING)] = tally.get(
            step.get("state", STEP_PENDING), 0) + 1
    tally["total"] = len(pursuit.get("steps", []))
    return tally


def progress_line(pursuit):
    """One sentence of progress, counted rather than described."""
    tally = counts(pursuit)
    if not tally["total"]:
        return "No steps planned yet."
    parts = [f"{tally[STEP_DONE]}/{tally['total']} done"]
    if tally[STEP_BLOCKED]:
        parts.append(f"{tally[STEP_BLOCKED]} blocked")
    if tally[STEP_SKIPPED]:
        parts.append(f"{tally[STEP_SKIPPED]} skipped")
    return ", ".join(parts) + "."


def next_step(pursuit):
    """The first step that could be worked on now, or None."""
    for step in pursuit.get("steps", []):
        if step.get("state") == STEP_PENDING:
            return step
    return None


def blockers(pursuit):
    return [step for step in pursuit.get("steps", [])
            if step.get("state") == STEP_BLOCKED]


def steps_text(pursuit):
    """The steps, numbered as they are referred to."""
    lines = []
    marks = {STEP_DONE: "x", STEP_BLOCKED: "!", STEP_SKIPPED: "-",
             STEP_PENDING: " "}
    for number, step in enumerate(pursuit.get("steps", []), start=1):
        mark = marks.get(step.get("state"), " ")
        line = f"  {number:>2}. [{mark}] {step['what']}"
        if step.get("state") == STEP_DONE and step.get("receipt"):
            line += f"\n         evidence: {step['receipt'][:160]}"
        if step.get("state") == STEP_BLOCKED and step.get("reason"):
            line += f"\n         blocked: {step['reason'][:160]}"
        lines.append(line)
    return "\n".join(lines) or "  (no steps planned yet)"


def describe(base_dir, identifier):
    """One pursuit in full: where it stands and what is in the way."""
    rows = _load(base_dir)
    pursuit = _find(rows, identifier)
    if pursuit is None:
        raise PursuitError(_unknown(rows, identifier))
    lines = [f"{pursuit['title']} [{pursuit['id']}] -- {pursuit['state']}",
             f"started {pursuit['created']}, last touched {pursuit['updated']}"]
    if pursuit.get("why"):
        lines.append(f"why: {pursuit['why']}")
    lines.append(progress_line(pursuit))
    lines.append("steps:")
    lines.append(steps_text(pursuit))
    upcoming = next_step(pursuit)
    if upcoming:
        lines.append(f"next: {upcoming['what']}")
    stuck = blockers(pursuit)
    if stuck:
        lines.append("waiting on: "
                     + "; ".join(f"{s['what']} ({s['reason']})" for s in stuck))
    if pursuit.get("outcome"):
        lines.append(f"outcome: {pursuit['outcome']}")
    return "\n".join(lines)


def listing(base_dir, include_closed=False):
    """Every pursuit, newest activity first."""
    rows = _load(base_dir)
    if not include_closed:
        rows = [row for row in rows if row.get("state") in OPEN_STATES]
    return sorted(rows, key=lambda row: row.get("updated", ""), reverse=True)


def summary(base_dir, include_closed=False):
    """What is being pursued, as a block for a person or the model."""
    rows = listing(base_dir, include_closed=include_closed)
    if not rows:
        return ("Nothing is being pursued right now. START_PURSUIT begins "
                "something that will take more than one sitting.")
    lines = []
    for row in rows:
        line = f"- {row['title']} [{row['id']}] -- {row['state']}, " \
               f"{progress_line(row)}"
        upcoming = next_step(row)
        if upcoming:
            line += f" Next: {upcoming['what']}"
        stuck = blockers(row)
        if stuck:
            line += f" Blocked on: {stuck[0]['what']} ({stuck[0]['reason']})"
        lines.append(line)
    return "\n".join(lines)


def days_since(pursuit):
    try:
        touched = datetime.datetime.fromisoformat(pursuit.get("updated", ""))
    except (ValueError, TypeError):
        return 0.0
    return (datetime.datetime.now() - touched).total_seconds() / 86400.0


def stale(base_dir, after_days=STALE_AFTER_DAYS):
    """Open pursuits nothing has happened to lately.

    The point of writing work down is that it does not have to be remembered,
    which only holds if something notices when it stops moving.
    """
    return [row for row in listing(base_dir)
            if days_since(row) >= after_days]


def prompt_block(base_dir, max_pursuits=4):
    """What the model is told about work already in flight.

    Bounded hard. The value is "there is a thread here and here is where it
    was left", not a full transcript of every objective -- a prompt that
    carries all of it has no room left for the turn.
    """
    rows = listing(base_dir)[:max_pursuits]
    if not rows:
        return ""
    lines = ["IN PROGRESS (work spanning more than one conversation). These "
             "are plans, not permissions: every step still goes through the "
             "ordinary action path. A step closes only against evidence, so "
             "use ADVANCE_PURSUIT with the receipt the work produced."]
    for row in rows:
        line = f"- {row['title']} [{row['id']}]: {progress_line(row)}"
        upcoming = next_step(row)
        if upcoming:
            line += f" Next: {upcoming['what']}."
        stuck = blockers(row)
        if stuck:
            line += f" Waiting on: {stuck[0]['what']} ({stuck[0]['reason']})."
        age = days_since(row)
        if age >= STALE_AFTER_DAYS:
            line += f" Untouched for {int(age)} days."
        lines.append(line)
    return "\n".join(lines)
