# -*- coding: utf-8 -*-
"""
execution_reliability.py -- making the final answer follow the receipts.

The failures this file exists for all have the same shape: VANTA said
something about its own work that was not true. Music opened and played and it
reported the open action as failed. Messages were described as approved and
sent when nothing had been sent. `<DONE />` arrived while outcomes were still
pending. Chrome and the music player opened twice. Each of those is a case
where the narration and the receipts disagreed and the narration won.

So nothing here asks the model what happened. The ledger is driven by action
receipts, and three things are derived from it rather than from prose:

  validate_proposal   refuses an action the plan did not select, before it runs
  apply_result        moves an outcome only on the evidence of its receipt
  completion_problem  refuses <DONE /> while a required outcome is unfinished

The second half is the fingerprint, which is how an approval survives the turn
that created it and how the same side effect stops happening twice. An approval
stores the fingerprint of the exact action it was granted for, and on approval
the payload is hashed again: if the two differ, the thing being approved is not
the thing that was shown, and it is refused.

WHY SELECTION IS ENFORCED AND NOT MERELY SUGGESTED
--------------------------------------------------
Phase B puts the chosen action for each outcome into the prompt. A prompt is a
request. For "remind me at 4:30" the model can see SET_ALARM, ADD_TASK and
ADD_CALENDAR_EVENT in its shortlist, and a model that fires two of them has
produced two reminders from one sentence. The alternatives are named in the
plan precisely so that proposing one is detectable, and detectable means it can
be refused rather than regretted.
"""

import hashlib
import json
import re

import outcomes
import outcome_planner

#: Receipt prefixes meaning the action never reached the outside world. Only
#: these permit an alternative to be tried, because only these prove nothing
#: happened. Anything else -- a timeout, an exception, a truncated reply --
#: leaves the side effect unknown, and unknown is not permission.
_REFUSED_BEFORE_ACTING = (
    "[authorization policy]", "[autonomy policy]", "[mode policy]",
    "[approval required]", "[confirmation required]", "[unavailable]",
    "[invalid]", "[schema]",
    "[not run]",
)

#: Receipt prefixes meaning the action is waiting on a human, not finished and
#: not failed. Treating this as failure is what made VANTA report refusals for
#: work that was merely paused.
_AWAITING = ("[approval required]", "[confirmation required]")

_FAILURE_MARKERS = ("[error]", "[failed]", "[unavailable]", "[unverified]",
                    "[partial]", "[pending]", "[not run]", "[interrupted]",
                    "[blocked]", "[calendar policy]", "[dataset error]",
                    "[authorization policy]", "[autonomy policy]", "[mode policy]",
                    "[browser error]")

#: Actions whose repetition inside one turn is a defect rather than a choice.
#: Deliberately a allow-list of side-effecting operations: re-reading a file,
#: re-taking a screenshot or searching twice is harmless and sometimes
#: necessary, and suppressing those would break legitimate retry loops. What
#: must not happen twice is a launch, a send, a purchase or a created record --
#: Chrome and the music player have each opened twice from a single request,
#: and a message delivered twice cannot be taken back.
DEDUPLICATED_ACTIONS = frozenset({
    # launching things
    "LAUNCH_APP", "OPEN_URL", "BROWSER_START", "BROWSER_GOTO",
    # playing things
    "MUSIC_PLAY", "SPOTIFY_PLAY",
    # telling people things
    "SEND_EMAIL", "SEND_TELEGRAM", "SEND_WHATSAPP", "NOTIFY",
    # creating records
    "ADD_CALENDAR_EVENT", "SET_ALARM", "ADD_ALARM", "ADD_TASK", "ADD_TASKS",
    "TRACK_COMMITMENT", "ADD_ROUTINE", "WATCH_CREATE", "FLIGHT_WATCH_CREATE",
    "REMEMBER_FACE", "FORGET_FACE",
    "SET_STANDBY", "RESUME_STANDBY",
    # spending money or committing to something
    "BOOK_RIDE", "DRIVE_UPLOAD", "DRIVE_SHARE",
})

#: The attribute a deliberate repeat carries. "Play it again" is a real
#: request, and suppression must not make it impossible -- only accidental.
REPEAT_ATTR = "repeat"

#: How many times the same alternative may be refused before the gate yields.
#: A gate with no release is a deadlock, and a deadlock costs a whole turn.
#: Only applies while the outcome has NOT succeeded -- once it has, insisting
#: is asking for a duplicate and is refused indefinitely.
_RELEASE_AFTER_BLOCKS = 3


_ACTION_TYPE = re.compile(
    r"<\s*ACTION\s+[^>]*?(?<![A-Za-z0-9_])type\s*=\s*[\"']([A-Za-z0-9_]+)[\"']",
    re.IGNORECASE)

#: Attributes added by the approval machinery after a payload is hashed. They
#: must not change a fingerprint, or an approved retry would never match the
#: approval it was granted under.
_VOLATILE_ATTRS = frozenset({
    "confirmed", "approved", "approval_id", "type", "speech",
})


def fingerprint(action_type, attrs, turn_id="", outcome_id=""):
    """A stable identity for "this action, with this payload, in this turn".

    Used for two jobs that need the same answer. An approval records it so the
    payload cannot be altered between being shown and being run, and
    duplicate suppression compares it so that the same send, launch or
    calendar entry does not happen twice inside one turn.

    Normalised so that irrelevant differences do not produce a different
    identity: the action name's case, the order of the attributes, and the
    bookkeeping flags the approval path adds afterwards.
    """
    payload = {
        str(key): str(value)
        for key, value in dict(attrs or {}).items()
        if str(key).lower() not in _VOLATILE_ATTRS
    }
    material = json.dumps({
        "action": str(action_type or "").strip().upper(),
        "payload": payload,
        "turn": str(turn_id or ""),
        "outcome": str(outcome_id or ""),
    }, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _receipt_state(result):
    """What a receipt actually says: awaiting, succeeded or failed."""
    text = str(result.get("output", "") or "").strip().lower()
    if any(text.startswith(marker) or marker in text[:40]
           for marker in _AWAITING):
        return outcomes.AWAITING_APPROVAL
    if (text and result.get("ok") is True
            and not any(text.startswith(marker) for marker in _FAILURE_MARKERS)):
        return outcomes.SUCCEEDED
    return outcomes.FAILED


def _side_effect_for(result):
    """Whether a failed action left anything behind.

    Defaults to unknown. A send that timed out may well have been delivered,
    and the only safe reading of "I don't know" is to stop -- which is what
    prevents a retry from delivering a second copy.
    """
    text = str(result.get("output", "") or "").strip().lower()
    if any(text.startswith(marker) for marker in _REFUSED_BEFORE_ACTING):
        return outcomes.SIDE_EFFECT_NONE
    return outcomes.SIDE_EFFECT_UNKNOWN


class OutcomeRuntime:
    """Ledger-backed execution for one turn's selected outcomes."""

    def __init__(self, compiled_plan, selected):
        self.plan = compiled_plan
        self.selected = tuple(selected or ())
        # Only outcomes with a selected action are enforced. An outcome the
        # compiler could not classify has no implementation, so requiring it
        # to succeed meant requiring something nobody had defined -- and the
        # turn reported failure for two calendar reminders it had already
        # created and verified.
        # Advise everywhere; enforce only what was actually understood. A
        # GENERIC outcome means the operation was not recognised, so a
        # suggested action for it is a hint for the model, not an obligation.
        # Suppressing those entirely was worse than the phantom it fixed: the
        # top candidate was DRIVE_UPLOAD, BOARD_PLOT and SHOW_ROUTE for three
        # real requests, and selecting nothing turned a correct shortlist into
        # "which part would you like me to do first?".
        selected_ids = {item.outcome_id for item in self.selected
                        if item.selected_action
                        and item.operation != outcomes.GENERIC}
        self.ledger = outcomes.Ledger(tuple(
            outcome for outcome in (getattr(compiled_plan, "outcomes", ()) or ())
            if outcome.outcome_id in selected_ids))
        #: selected action -> the outcome it was selected for
        self._by_action = {}
        #: an alternative that was deliberately NOT chosen -> its outcome
        self._rejected = {}
        #: An action aimed at the wrong evidence source is not an alternative
        #: implementation. A screenshot cannot satisfy a camera observation,
        #: and a webcam frame cannot satisfy a screen-reading request.
        self._incompatible = {}
        for item in self.selected:
            if item.selected_action:
                self._by_action.setdefault(
                    item.selected_action.upper(), item.outcome_id)
            for alternative in item.alternatives:
                name = str(alternative).upper()
                if name not in self._by_action:
                    self._rejected.setdefault(name, item.outcome_id)
            for incompatible in outcome_planner.incompatible_actions(
                    item.operation):
                name = str(incompatible).upper()
                if name not in self._by_action:
                    self._incompatible.setdefault(
                        name, (item.outcome_id, item.selected_action))
        #: fingerprints already dispatched this turn, for duplicate
        #: suppression within the turn.
        self._dispatched = {}
        #: how many times each unselected alternative has been refused, so a
        #: refusal cannot repeat forever.
        self._blocks = {}

    # ---------------------------------------------------------------- #
    # Lookups
    # ---------------------------------------------------------------- #

    def outcome_for_action(self, action_type):
        """Which outcome an action belongs to, or "" for a helper action.

        Discovery, contact lookup and screenshots are not outcomes; they are
        steps toward one. Returning "" for them keeps them out of the ledger
        so they cannot be mistaken for progress.
        """
        upper = str(action_type or "").upper()
        if upper in self._by_action:
            return self._by_action[upper]
        # An alternative is, by definition, another way to achieve the same
        # outcome -- so one that succeeds has satisfied it. Without this, the
        # model doing the job by a different route left the outcome pending
        # and the gate refused a turn whose work was done.
        return self._rejected.get(upper, "")

    def selected_actions(self):
        return tuple(self._by_action)

    # ---------------------------------------------------------------- #
    # Before dispatch
    # ---------------------------------------------------------------- #

    def validate_proposal(self, response):
        """Refuse an alternative the plan passed over.

        Only alternatives are refused, never unrecognised actions: the model
        legitimately needs LOOKUP_CONTACTS, DISCOVER_ACTIONS or a screenshot
        on the way to an outcome, and blocking those would be a new false
        denial in place of the old one.
        """
        for name in _ACTION_TYPE.findall(str(response or "")):
            upper = name.upper()
            if upper in self._by_action:
                continue
            conflict = self._incompatible.get(upper)
            if conflict:
                outcome_id, chosen = conflict
                return (
                    f"{upper} observes the wrong source for {outcome_id}. "
                    f"The request requires {chosen}; use that registered "
                    f"action and do not substitute camera and screen inputs.")
            outcome_id = self._rejected.get(upper)
            if not outcome_id:
                continue
            chosen = next(
                (item.selected_action for item in self.selected
                 if item.outcome_id == outcome_id), "")
            record = self.ledger.get(outcome_id)

            # Already done. Insisting cannot be granted -- a second reminder
            # or a second message is the harm the gate exists to prevent -- so
            # the answer has to be an instruction to stop rather than a
            # refusal that invites another attempt.
            if record.state == outcomes.SUCCEEDED:
                self.ledger.note_alternative(
                    outcome_id, upper, "proposed after the outcome succeeded")
                return (
                    f"{outcome_id} is already satisfied by {chosen}, which "
                    f"succeeded. Running {upper} as well would create a "
                    f"second one. Report what was done and finish; if "
                    f"{chosen} was the wrong mechanism, say so plainly "
                    f"instead of adding another.")

            self._blocks[upper] = self._blocks.get(upper, 0) + 1
            attempts = self._blocks[upper]
            if attempts >= _RELEASE_AFTER_BLOCKS:
                # Repeated insistence on an unperformed outcome means the
                # selection was probably wrong -- the planner wanted a
                # calendar entry, the default chose an alarm, and the critic
                # kept failing a turn that could not proceed. Four refusals
                # in a row cost fourteen model calls and sixty-eight thousand
                # tokens and ended with nothing done. Yielding once is worse
                # than nothing only if the outcome already happened, and that
                # case returned above.
                self.ledger.note_alternative(
                    outcome_id, upper,
                    f"released after {attempts} blocked proposals; selection "
                    f"of {chosen} was overridden by persistent insistence")
                self._by_action.setdefault(upper, outcome_id)
                return ""

            self.ledger.note_alternative(
                outcome_id, upper, "proposed but not selected")
            return (
                f"The plan did not select {upper} for {outcome_id}; it "
                f"selected {chosen}. {upper} is an alternative way to "
                f"achieve the same outcome, so running it as well would "
                f"do the same thing twice. Use {chosen}, or explain why it "
                f"cannot satisfy the outcome.")
        return ""

    def already_dispatched(self, action_type, attrs, turn_id=""):
        """Whether this exact action with this exact payload already ran.

        Chrome and the music player have each opened twice from one request,
        because a repeated model step re-issued an action that had already
        succeeded. The fingerprint makes the repeat recognisable.
        """
        mark = fingerprint(action_type, attrs, turn_id,
                           self.outcome_for_action(action_type))
        return mark in self._dispatched

    def note_dispatch(self, action_type, attrs, turn_id=""):
        mark = fingerprint(action_type, attrs, turn_id,
                           self.outcome_for_action(action_type))
        self._dispatched[mark] = str(action_type or "").upper()
        return mark

    def begin(self, action_type):
        """An outcome's selected action has started."""
        outcome_id = self.outcome_for_action(action_type)
        if not outcome_id:
            return None
        record = self.ledger.get(outcome_id)
        if record.terminal:
            return record
        self.ledger.note_attempt(outcome_id, action_type)
        return self.ledger.mark(outcome_id, outcomes.EXECUTING)

    # ---------------------------------------------------------------- #
    # After dispatch
    # ---------------------------------------------------------------- #

    def apply_result(self, result):
        """Move an outcome according to one action's receipt."""
        if not isinstance(result, dict):
            return None
        action_type = str(result.get("type", "") or "")
        outcome_id = self.outcome_for_action(action_type)
        if not outcome_id:
            # A helper action's receipt is recorded as context, never as
            # progress toward an outcome.
            for item in self.selected:
                self.ledger.note_discovery(
                    item.outcome_id, f"{action_type}: ran as a helper step")
                break
            return None
        record = self.ledger.get(outcome_id)
        if record.state == outcomes.SUCCEEDED:
            return record

        state = _receipt_state(result)
        receipt = str(result.get("output", "") or "").strip()
        if not receipt:
            receipt = '[Unverified] No execution receipt was returned.'
        if state == outcomes.SUCCEEDED:
            # The required result is declared by the outcome and verified by
            # the capability. A success with no receipt text cannot be
            # recorded at all -- the ledger refuses it.
            return self.ledger.mark(outcome_id, outcomes.SUCCEEDED,
                                    receipt=receipt or "(no receipt text)")
        if state == outcomes.AWAITING_APPROVAL:
            return self.ledger.mark(outcome_id, outcomes.AWAITING_APPROVAL,
                                    failure=receipt,
                                    side_effect=outcomes.SIDE_EFFECT_NONE)
        return self.ledger.mark(outcome_id, outcomes.FAILED, failure=receipt,
                                side_effect=_side_effect_for(result))

    def apply_outcome(self, outcome):
        """Apply every receipt from one dispatcher run."""
        data = getattr(outcome, "data", None)
        if isinstance(data, list):
            for item in data:
                self.apply_result(item)
            return
        text = ""
        try:
            text = outcome.model_text()
        except Exception:
            text = str(outcome or "")
        action_type = getattr(outcome, "action_type", "") or ""
        lowered = text.strip().lower()
        self.apply_result({
            "type": action_type,
            "ok": bool(lowered) and not any(lowered.startswith(marker)
                          for marker in _FAILURE_MARKERS),
            "output": text,
        })

    def alternative_for(self, outcome_id):
        """A fallback action, but only when nothing happened the first time."""
        if not self.ledger.may_try_alternative(outcome_id):
            return ""
        for item in self.selected:
            if item.outcome_id == outcome_id:
                for name in item.alternatives:
                    return name
        return ""

    # ---------------------------------------------------------------- #
    # Completion
    # ---------------------------------------------------------------- #

    def completion_problem(self):
        """Why <DONE /> is not allowed yet, or "" when it is.

        Allowed once nothing is still moving. That includes a turn whose
        outcomes failed or are waiting on approval: those are terminal for
        this turn and must be reported honestly, not retried forever. What it
        does not include is an outcome still pending or executing, which is
        the case that used to end with a confident summary of work that had
        not happened.
        """
        unfinished = [
            record for record in self.ledger.records()
            if not record.terminal
            and record.state != outcomes.AWAITING_APPROVAL
        ]
        if not unfinished:
            return ""
        described = ", ".join(
            f"{record.outcome_id} ({record.state})" for record in unfinished)
        required = "; ".join(
            f"{record.outcome_id} needs {self.ledger.intent(record.outcome_id).required_result}"
            for record in unfinished)
        return (f"The task is not complete: {described}. {required}. "
                f"Run the selected action for each unfinished outcome, or "
                f"report plainly why it cannot be done.")

    def task_successful(self):
        return self.ledger.task_successful()

    def authoritatively_successful(self):
        """Whether typed receipts fully settle every compiled user outcome.

        A second LLM review adds no evidence when the compiler understood every
        requested outcome, selected one registered capability for each, and
        every provider returned a successful receipt. Generic or partially
        selected plans still need the critic because their contract is not
        complete enough for deterministic verification.
        """
        compiled = tuple(getattr(self.plan, "outcomes", ()) or ())
        if not compiled:
            return False
        if any(getattr(item, "operation", "") == outcomes.GENERIC
               for item in compiled):
            return False
        expected = {item.outcome_id for item in compiled}
        selected = {
            item.outcome_id for item in self.selected
            if item.selected_action and item.operation != outcomes.GENERIC
        }
        if expected != selected:
            return False
        records = tuple(self.ledger.records())
        return bool(records) and self.ledger.task_successful() and all(
            any(str(receipt or "").strip() for receipt in record.receipts)
            for record in records)

    def turn_terminal(self):
        return self.ledger.turn_terminal() or not self.ledger.records()

    def honest_summary(self):
        """What to tell him, derived from the ledger rather than narration.

        Separated from completion because finishing the handling of a task and
        succeeding at it are different facts, and collapsing them is what let
        a failed send be reported as a sent message.
        """
        if not self.ledger.records():
            return ""
        if self.ledger.task_successful():
            return ""
        parts = []
        for record in self.ledger.failures():
            intent = self.ledger.intent(record.outcome_id)
            detail = record.failure or "no receipt"
            parts.append(f"{intent.operation.replace('_', ' ').lower()} "
                         f"({record.state}: {detail})")
        if not parts:
            return ""
        return ("Report honestly that these did not succeed: "
                + "; ".join(parts))
