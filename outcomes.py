# -*- coding: utf-8 -*-
"""
outcomes.py -- what was asked, what will run, and what actually happened.

Three structures, deliberately separate, because collapsing them is how a
system starts lying about itself:

  CompiledOutcome   the request, interpreted. Frozen.
  ResolvedOutcome   that interpretation plus the action chosen for it. Frozen.
  OutcomeExecution  what happened when it ran. The only mutable thing here.

The separation is not bookkeeping tidiness. If execution state lives on the
interpretation, then a retry, a fallback or a failure can quietly rewrite what
the user was understood to have asked for, and afterwards nothing can tell
whether VANTA did the wrong thing or merely recorded the right thing badly.
Freezing the interpretation means the question survives the answer.

WHY COMPLETION IS TWO FLAGS
---------------------------
VANTA has said it approved messages it never sent, and reported an open action
as failed while the application was plainly running. Both come from one habit:
treating "the turn is over" and "the task worked" as the same fact. They are
not, and a single boolean forces a choice between claiming a success that did
not happen and withholding an answer that is ready.

  turn_terminal    every outcome reached a terminal state; there is nothing
                   left to wait for and a reply may be given
  task_successful  every required outcome actually succeeded, with a receipt

A fully reported failure is terminal and not successful. That combination is
what lets VANTA finish the turn by saying honestly that something did not
work, instead of either hanging or claiming it did.

Verification lives in the capability layer. An outcome declares the result it
requires in words; the capability that runs it owns the concrete check. There
are deliberately no file, device or network calls in this file.
"""

import time

# --- operations ------------------------------------------------------- #
# Intent verbs, not action names. This is the distinction that makes
# "alternatives" expressible at all: CREATE_REMINDER is one thing the user
# wants, and SET_ALARM, ADD_TASK and ADD_CALENDAR_EVENT are three ways to
# provide it. Naming the outcome after an action would make three ways of
# satisfying one request look like three requests.
UPDATE_SCHEDULED_EVENT = "UPDATE_SCHEDULED_EVENT"
CREATE_SCHEDULED_EVENT = "CREATE_SCHEDULED_EVENT"
CANCEL_SCHEDULED_EVENT = "CANCEL_SCHEDULED_EVENT"
CREATE_REMINDER = "CREATE_REMINDER"
CREATE_TASK = "CREATE_TASK"
CREATE_ALARM = "CREATE_ALARM"
SEND_MESSAGE = "SEND_MESSAGE"
SEND_EMAIL = "SEND_EMAIL"
RECORD_NOTE = "RECORD_NOTE"
PERCEIVE_VISUAL = "PERCEIVE_VISUAL"
# Visual intent is split by source and requested result.  Treating all four
# as PERCEIVE_VISUAL made a camera request and a screen request alternatives
# for the same outcome; once the planner picked the screen, it actively
# blocked the model when it correctly tried the webcam.  Displaying an
# already-captured artifact is different again: loading it into model context
# is not proof that Himanshu can see it.
OBSERVE_CAMERA = "OBSERVE_CAMERA"
OBSERVE_SCREEN = "OBSERVE_SCREEN"
DISPLAY_CAMERA_CAPTURE = "DISPLAY_CAMERA_CAPTURE"
READ_SCHEDULE = "READ_SCHEDULE"
READ_MAIL = "READ_MAIL"
ENTER_STANDBY = "ENTER_STANDBY"
ENROLL_FACE = "ENROLL_FACE"
LEARN_BEHAVIOUR = "LEARN_BEHAVIOUR"
MOVE_ARTIFACT = "MOVE_ARTIFACT"
DISPLAY_CONTENT = "DISPLAY_CONTENT"
EDIT_IN_APPLICATION = "EDIT_IN_APPLICATION"
CREATE_PROJECT = "CREATE_PROJECT"
OPEN_PROJECT = "OPEN_PROJECT"

# --- the five that used to compile to GENERIC ------------------------- #
# Each was a real request that the compiler could not name, so nothing was
# decided and the model was left to pick the top of a shortlist. That worked
# when the shortlist was good and failed silently when it was not -- the gate
# pass offered TRENDING_NEWS and MOVE_FILE.
#: Look something up, in the world rather than in memory.
RESEARCH_TOPIC = "RESEARCH_TOPIC"
#: Write a document to disk for Himanshu to open.
CREATE_DOCUMENT = "CREATE_DOCUMENT"
#: Put a file somewhere remote.
UPLOAD_FILE = "UPLOAD_FILE"
#: Work through a form on a website: a portal, an application, a booking.
WEB_WORKFLOW = "WEB_WORKFLOW"
# Read a concrete page or portal.  This is deliberately distinct from
# WEB_WORKFLOW, which may fill and submit a form.  A scheduled read-only
# portal check needs navigation and a snapshot, never a submission receipt.
INSPECT_WEB_PAGE = "INSPECT_WEB_PAGE"
#: Explain something in a rich report, with working.
TEACH_ON_BOARD = "TEACH_ON_BOARD"
#: Draw a graph of something in a rich report.
PLOT_ON_BOARD = "PLOT_ON_BOARD"
#: How long a journey takes.
TRAVEL_TIME = "TRAVEL_TIME"
#: Show the journey on a map.
SHOW_JOURNEY = "SHOW_JOURNEY"
ANSWER_FROM_KNOWLEDGE = "ANSWER_FROM_KNOWLEDGE"
GENERIC = "GENERIC"

# --- execution states -------------------------------------------------- #
PENDING = "pending"
READY = "ready"
EXECUTING = "executing"
AWAITING_APPROVAL = "awaiting_approval"
SUCCEEDED = "succeeded"
FAILED = "failed"
BLOCKED = "blocked"
CANCELLED = "cancelled"
SUPERSEDED = "superseded"

STATES = (PENDING, READY, EXECUTING, AWAITING_APPROVAL, SUCCEEDED, FAILED,
          BLOCKED, CANCELLED, SUPERSEDED)

#: Nothing further will happen to an outcome in one of these.
TERMINAL_STATES = frozenset({SUCCEEDED, FAILED, BLOCKED, CANCELLED,
                             SUPERSEDED})

# --- side-effect certainty -------------------------------------------- #
# Whether a failed attempt left something behind. This decides whether a
# second provider may be tried, so the default has to be the cautious one:
# a message that timed out may well have been delivered, and "I don't know"
# must never read as "nothing happened". That mistake sends things twice.
SIDE_EFFECT_NONE = "none"
SIDE_EFFECT_UNKNOWN = "unknown"
SIDE_EFFECT_OCCURRED = "occurred"

# --- confirmation boundaries ------------------------------------------ #
CONFIRM_NONE = "none"
CONFIRM_BEFORE_EXECUTE = "before_execute"
CONFIRM_BEFORE_EXTERNAL_SEND = "before_external_send"


class CompiledOutcome:
    """One end state the user asked for. Immutable.

    Carries no candidates, no selection and no state: at compile time none of
    those are known, and a field that is empty "for now" is a field something
    will eventually fill in place, which is the mutation this design exists to
    prevent.
    """

    __slots__ = ("_outcome_id", "_operation", "_target", "_arguments",
                 "_content_spans", "_temporal_scope", "_dependencies",
                 "_required_result", "_confirmation_boundary", "_confidence",
                 "_frozen")

    def __init__(self, outcome_id, operation, target="", arguments=None,
                 content_spans=(), temporal_scope="", dependencies=(),
                 required_result="", confirmation_boundary=CONFIRM_NONE,
                 confidence=1.0):
        if operation not in OPERATIONS:
            raise ValueError(f"unknown operation: {operation!r}")
        if confirmation_boundary not in (CONFIRM_NONE, CONFIRM_BEFORE_EXECUTE,
                                         CONFIRM_BEFORE_EXTERNAL_SEND):
            raise ValueError(
                f"unknown confirmation boundary: {confirmation_boundary!r}")
        object.__setattr__(self, "_outcome_id", str(outcome_id))
        object.__setattr__(self, "_operation", operation)
        object.__setattr__(self, "_target", str(target or ""))
        object.__setattr__(self, "_arguments",
                           dict(arguments or {}))
        object.__setattr__(self, "_content_spans", tuple(content_spans))
        object.__setattr__(self, "_temporal_scope", str(temporal_scope or ""))
        object.__setattr__(self, "_dependencies", tuple(dependencies))
        object.__setattr__(self, "_required_result",
                           str(required_result or "").strip()
                           or _default_required_result(operation))
        object.__setattr__(self, "_confirmation_boundary",
                           confirmation_boundary)
        object.__setattr__(self, "_confidence", float(confidence))
        object.__setattr__(self, "_frozen", True)

    # Read-only views. The arguments dict is copied on access so that a caller
    # holding it cannot reach back into the interpretation.
    outcome_id = property(lambda self: self._outcome_id)
    operation = property(lambda self: self._operation)
    target = property(lambda self: self._target)
    arguments = property(lambda self: dict(self._arguments))
    content_spans = property(lambda self: self._content_spans)
    temporal_scope = property(lambda self: self._temporal_scope)
    dependencies = property(lambda self: self._dependencies)
    required_result = property(lambda self: self._required_result)
    confirmation_boundary = property(lambda self: self._confirmation_boundary)
    confidence = property(lambda self: self._confidence)

    def __setattr__(self, name, value):
        raise AttributeError(
            f"CompiledOutcome is immutable; execution state belongs in the "
            f"ledger, not on the interpretation (tried to set {name!r})")

    def __delattr__(self, name):
        raise AttributeError("CompiledOutcome is immutable")

    def __repr__(self):
        return (f"CompiledOutcome({self._outcome_id!r}, {self._operation!r}, "
                f"target={self._target!r})")


OPERATIONS = (UPDATE_SCHEDULED_EVENT, CREATE_SCHEDULED_EVENT,
              CANCEL_SCHEDULED_EVENT, CREATE_REMINDER, CREATE_TASK,
              CREATE_ALARM, SEND_MESSAGE, SEND_EMAIL, RECORD_NOTE,
              PERCEIVE_VISUAL, OBSERVE_CAMERA, OBSERVE_SCREEN,
              DISPLAY_CAMERA_CAPTURE, READ_SCHEDULE, READ_MAIL,
              ENTER_STANDBY, ENROLL_FACE, LEARN_BEHAVIOUR,
              MOVE_ARTIFACT, DISPLAY_CONTENT, EDIT_IN_APPLICATION,
              CREATE_PROJECT, OPEN_PROJECT,
              RESEARCH_TOPIC, CREATE_DOCUMENT, UPLOAD_FILE, WEB_WORKFLOW,
              INSPECT_WEB_PAGE,
              TEACH_ON_BOARD, PLOT_ON_BOARD, TRAVEL_TIME, SHOW_JOURNEY,
              ANSWER_FROM_KNOWLEDGE, GENERIC)

#: What success has to look like, when the compiler does not say. Phrased as
#: the evidence rather than the act, because "the reminder was created" is
#: satisfied by an attempt while "retrievable afterwards" is not.
_REQUIRED_RESULTS = {
    UPDATE_SCHEDULED_EVENT: "the refreshed event confirms the new time",
    CREATE_SCHEDULED_EVENT: "the created event is retrievable with its time",
    CANCEL_SCHEDULED_EVENT: "the event is absent when the calendar is re-read",
    CREATE_REMINDER: "a provider receipt, and the reminder is retrievable",
    CREATE_TASK: "a provider receipt, and the task appears in the list",
    CREATE_ALARM: "a provider receipt, and the alarm is listed",
    SEND_MESSAGE: "a provider message id or delivery status",
    SEND_EMAIL: "a provider message id",
    RECORD_NOTE: "the note is retrievable",
    PERCEIVE_VISUAL: "a non-empty frame that vision analysis received",
    OBSERVE_CAMERA: "a non-empty webcam frame that vision analysis received",
    OBSERVE_SCREEN: "a non-empty current-screen frame that vision analysis received",
    DISPLAY_CAMERA_CAPTURE: "the retained camera image is visibly rendered in VANTA",
    READ_SCHEDULE: "the schedule was read",
    READ_MAIL: "the inbox was read",
    ENTER_STANDBY: "the runtime reports standby active with the requested duration",
    ENROLL_FACE: "an encrypted face template id linked to the named person",
    LEARN_BEHAVIOUR: "a Neo4j procedure id with its trigger and instruction",
    MOVE_ARTIFACT: "the source is absent and the destination file exists",
    DISPLAY_CONTENT: "the requested content is visibly rendered in VANTA's dashboard",
    EDIT_IN_APPLICATION: (
        "the requested application visibly contains the requested content "
        "and a readback or current-window observation confirms it"),
    CREATE_PROJECT: (
        "the project directory exists under vault/projects and is active"),
    OPEN_PROJECT: (
        "the named project was resolved inside vault/projects and is active"),
    RESEARCH_TOPIC: "findings with their sources",
    CREATE_DOCUMENT: "the file exists at the stated path with the content in it",
    UPLOAD_FILE: "a provider receipt naming the uploaded file",
    WEB_WORKFLOW: "the page state after submitting confirms it was accepted",
    INSPECT_WEB_PAGE: "the managed browser is on the requested URL and a current page snapshot was read",
    TEACH_ON_BOARD: "the report visibly renders the explanation",
    PLOT_ON_BOARD: "the report visibly renders the plot",
    TRAVEL_TIME: "a duration from a routing provider",
    SHOW_JOURNEY: "the route is on screen",
    ANSWER_FROM_KNOWLEDGE: "an answer grounded in retrieved evidence",
    GENERIC: "the action returned a success receipt",
}


def _default_required_result(operation):
    return _REQUIRED_RESULTS.get(operation, _REQUIRED_RESULTS[GENERIC])


class ResolvedOutcome:
    """A compiled outcome bound to the actions that could satisfy it.

    Produced by binding and then by selection, each returning a new object.
    Frozen as well: once something has been chosen, the record of what was
    chosen and what was passed over should not be editable by whatever runs
    next.
    """

    __slots__ = ("_outcome", "_candidate_actions", "_alternatives",
                 "_selected_action")

    def __init__(self, outcome, candidate_actions=(), alternatives=(),
                 selected_action=""):
        object.__setattr__(self, "_outcome", outcome)
        object.__setattr__(self, "_candidate_actions",
                           tuple(candidate_actions))
        object.__setattr__(self, "_alternatives", tuple(alternatives))
        object.__setattr__(self, "_selected_action", str(selected_action or ""))

    outcome = property(lambda self: self._outcome)
    outcome_id = property(lambda self: self._outcome.outcome_id)
    operation = property(lambda self: self._outcome.operation)
    target = property(lambda self: self._outcome.target)
    arguments = property(lambda self: self._outcome.arguments)
    content_spans = property(lambda self: self._outcome.content_spans)
    dependencies = property(lambda self: self._outcome.dependencies)
    required_result = property(lambda self: self._outcome.required_result)
    candidate_actions = property(lambda self: self._candidate_actions)
    alternatives = property(lambda self: self._alternatives)
    selected_action = property(lambda self: self._selected_action)

    def with_selection(self, selected, alternatives):
        """A new resolved outcome carrying the choice. Never mutates."""
        return ResolvedOutcome(self._outcome, self._candidate_actions,
                               alternatives, selected)

    def __setattr__(self, name, value):
        raise AttributeError("ResolvedOutcome is immutable; use with_selection")

    def __repr__(self):
        return (f"ResolvedOutcome({self.outcome_id!r}, {self.operation!r}, "
                f"selected={self._selected_action!r})")


class OutcomeExecution:
    """The mutable runtime record for one outcome. The only mutable thing."""

    __slots__ = ("outcome_id", "state", "attempts", "receipts", "failure",
                 "side_effect", "notes", "created_at", "updated_at")

    def __init__(self, outcome_id):
        self.outcome_id = str(outcome_id)
        self.state = PENDING
        self.attempts = []
        self.receipts = []
        self.failure = ""
        self.side_effect = SIDE_EFFECT_UNKNOWN
        #: Discovery and alternative evaluation land here. Deliberately not in
        #: `attempts`, because they are not attempts at the outcome and must
        #: never be mistaken for progress toward it.
        self.notes = []
        self.created_at = time.time()
        self.updated_at = self.created_at

    @property
    def terminal(self):
        return self.state in TERMINAL_STATES

    @property
    def succeeded(self):
        return self.state == SUCCEEDED

    def __repr__(self):
        return f"OutcomeExecution({self.outcome_id!r}, {self.state!r})"


class Ledger:
    """Execution state for a turn's outcomes, keyed by outcome_id."""

    def __init__(self, compiled_outcomes):
        self._outcomes = {outcome.outcome_id: outcome
                          for outcome in compiled_outcomes or ()}
        self._records = {outcome_id: OutcomeExecution(outcome_id)
                         for outcome_id in self._outcomes}

    # ---------------------------------------------------------------- #

    def get(self, outcome_id):
        return self._records[str(outcome_id)]

    def records(self):
        return tuple(self._records.values())

    def intent(self, outcome_id):
        """The frozen interpretation. Read-only by construction."""
        return self._outcomes[str(outcome_id)]

    def mark(self, outcome_id, state, receipt="", failure="",
             side_effect=None):
        """Move an outcome to a new state.

        A success requires a receipt. That is the rule the system kept
        breaking: SET_ALARM reported failure while succeeding, a browser
        action reported success it had not earned, and messages were described
        as approved and sent when nothing had been sent. Refusing to record
        success without evidence removes the cheapest way to be wrong.
        """
        if state not in STATES:
            raise ValueError(f"unknown outcome state: {state!r}")
        record = self.get(outcome_id)
        if state == SUCCEEDED and not str(receipt or "").strip():
            raise ValueError(
                f"{outcome_id} cannot be marked succeeded without a receipt; "
                f"its required result is "
                f"{self.intent(outcome_id).required_result!r}")
        record.state = state
        if receipt:
            record.receipts.append(str(receipt))
        if failure:
            record.failure = str(failure)
        if side_effect is not None:
            if side_effect not in (SIDE_EFFECT_NONE, SIDE_EFFECT_UNKNOWN,
                                   SIDE_EFFECT_OCCURRED):
                raise ValueError(f"unknown side effect: {side_effect!r}")
            record.side_effect = side_effect
        elif state == SUCCEEDED:
            record.side_effect = SIDE_EFFECT_OCCURRED
        record.updated_at = time.time()
        return record

    def note_attempt(self, outcome_id, action, detail=""):
        record = self.get(outcome_id)
        record.attempts.append({"action": str(action), "detail": str(detail),
                                "at": time.time()})
        return record

    def note_discovery(self, outcome_id, detail=""):
        """Record a registry search. Explicitly not progress.

        Discovery once consumed the whole action budget and left nothing for
        execution or a reply, and a turn that had only searched could read as
        a turn that had done something.
        """
        self.get(outcome_id).notes.append(
            {"kind": "discovery", "detail": str(detail), "at": time.time()})

    def note_alternative(self, outcome_id, action, detail=""):
        """Record that an alternative was considered. Also not progress."""
        self.get(outcome_id).notes.append(
            {"kind": "alternative", "action": str(action),
             "detail": str(detail), "at": time.time()})

    def supersede(self, outcome_id, reason=""):
        """He changed his mind mid-task: replace rather than run both."""
        return self.mark(outcome_id, SUPERSEDED, failure=str(reason or ""))

    def may_try_alternative(self, outcome_id):
        """Whether a second provider may be tried after a failure.

        Only when the receipt proves nothing happened. Uncertainty is not
        permission -- a timed-out send may have been delivered, and trying
        another channel on a maybe is how one message arrives twice.
        """
        record = self.get(outcome_id)
        if record.state not in (FAILED, BLOCKED):
            return False
        return record.side_effect == SIDE_EFFECT_NONE

    # --- completion --------------------------------------------------- #

    def turn_terminal(self):
        """Nothing is still pending, so a reply may be given."""
        return bool(self._records) and all(
            record.terminal for record in self._records.values())

    def task_successful(self):
        """Every outcome actually succeeded. Not the same as being finished."""
        return bool(self._records) and all(
            record.succeeded for record in self._records.values())

    def unfinished(self):
        return tuple(record for record in self._records.values()
                     if not record.terminal)

    def failures(self):
        return tuple(record for record in self._records.values()
                     if record.state in (FAILED, BLOCKED, CANCELLED,
                                         SUPERSEDED))

    def summary(self):
        """One honest line about what happened, for the runtime record."""
        if not self._records:
            return "no outcomes"
        if self.task_successful():
            return f"{len(self._records)} outcome(s) succeeded"
        parts = []
        for record in self._records.values():
            detail = f": {record.failure}" if record.failure else ""
            parts.append(f"{record.outcome_id} {record.state}{detail}")
        state = "handled" if self.turn_terminal() else "in progress"
        return f"{state} -- " + "; ".join(parts)
