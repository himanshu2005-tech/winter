# -*- coding: utf-8 -*-
"""
action_bus.py -- one table, no chain.

Dispatch used to be a 116-branch `if/elif action_type == ...` ladder inside
`execute_actions`, three and a half thousand lines long, with twenty-three of
those branches holding a second ladder inside them. 144 of the 240 action names
were selected by one of those inner ladders. Adding an action meant finding the
right rung and inserting another one, and nothing anywhere could answer "what
can be dispatched?" without re-reading the function.

That shape caused real failures, not just discomfort. A capability could be
authorized, schema-checked and documented and still have no rung -- which is
precisely how CAPTURE_CAMERA came to be undiscoverable. The ladder was the one
consumer nothing could enumerate.

So: a name maps to a handler in a dictionary, and dispatch is a lookup. Nothing
branches on which action is running, including for the unknown case -- an
unregistered name resolves to a registration whose handler explains that it is
unregistered, so default-deny is a table entry rather than a final `else`.

WHAT A HANDLER IS
-----------------
A callable taking one `ActionContext` and returning either None or a flow
signal. It reports its result by setting a receipt on the context. It does not
decide whether it is allowed to run, whether it is a duplicate, whether its
arguments are valid or whether it needs confirmation -- those are controls the
bus and the surrounding pipeline apply before it is ever called.

CONTROLS
--------
Declared per registration, not written into the body:

    requires=     preconditions, checked in order; the first message wins
    receipt=      what evidence a success returns, in words
    family=       which group of actions this belongs to, for diagnostics

A precondition is a callable(ctx) returning "" to proceed or a message to
refuse with. Three of them exist because the old ladder had them interleaved
*between* its rungs, where they read as dispatch and were impossible to see:
"a project must be open", "only Himanshu may do this". They are preconditions,
so they are declared as preconditions.

FLOW
----
A handler normally returns None and the pipeline records its receipt. Three
other outcomes exist, because the old ladder expressed them with `continue` and
`break` against the enclosing loop:

    FLOW_RECORDED    the handler already recorded its own receipt; do not
                     record it again
    FLOW_STOP_BATCH  stop processing the remaining actions in this reply
    FLOW_NEXT        the default

Nothing here executes anything by itself, and registration grants no
permission. The authorization set, the schema, the mode policy and the
approval gate remain exactly where they were and still run first; this only
replaces the question "which code runs for this name".
"""

import time


# --- flow signals ----------------------------------------------------- #
FLOW_NEXT = "next"
FLOW_RECORDED = "recorded"
FLOW_STOP_BATCH = "stop_batch"
FLOW_SIGNALS = (FLOW_NEXT, FLOW_RECORDED, FLOW_STOP_BATCH)


class ActionContext:
    """Everything a handler is given, and the only thing it is given.

    Deliberately an object rather than a pile of arguments: the old branch
    bodies read nine different names out of the enclosing function, and a
    tenth could be added without any of them noticing. A context is one thing
    to extend and one thing to fake in a test.
    """

    __slots__ = ("action_type", "attrs", "response_text", "element_map",
                 "ui_queue", "base_dir", "origin", "turn_id",
                 "interrupt_event", "previous_output", "legacy", "_receipt",
                 "started")

    def __init__(self, action_type, attrs, ui_queue, base_dir, origin="user",
                 turn_id="", interrupt_event=None, element_map=None,
                 response_text="", previous_output="", legacy=None):
        self.action_type = str(action_type or "").upper()
        self.attrs = dict(attrs or {})
        self.ui_queue = ui_queue
        self.base_dir = base_dir
        self.origin = str(origin or "user")
        self.turn_id = str(turn_id or "")
        self.interrupt_event = interrupt_event
        self.element_map = element_map
        self.response_text = response_text or ""
        self.previous_output = previous_output or ""
        self.legacy = legacy
        self._receipt = ""
        self.started = time.monotonic()

    # -- arguments ----------------------------------------------------- #
    def attr(self, name, default=""):
        value = self.attrs.get(name, default)
        return value.strip() if isinstance(value, str) else value

    def flag(self, name, default=False):
        raw = str(self.attrs.get(name, "")).strip().lower()
        if not raw:
            return bool(default)
        return raw in ("true", "yes", "1", "on")

    # -- the result ---------------------------------------------------- #
    def report(self, receipt):
        """Record what happened. This is how a handler returns its result.

        It writes through to the legacy `_last_output` as well, because the
        surrounding pipeline, the critic and the recovery sweep all still read
        that, and a second source of truth for "what did the last action say"
        is how a receipt goes missing.
        """
        text = str(receipt or "")
        self._receipt = text
        if self.legacy is not None:
            self.legacy._last_output = text
        return text

    @property
    def receipt(self):
        return self._receipt

    def say(self, text):
        """Put a line on the Activity stream and in the terminal."""
        if self.ui_queue is not None:
            self.ui_queue.put(("info", str(text)))

    def step(self, text):
        """One short line on the notch: what is being done right now."""
        if self.ui_queue is not None:
            self.ui_queue.put(("action", str(text)[:60]))

    def elapsed(self):
        return time.monotonic() - self.started


# --- preconditions ----------------------------------------------------- #
def user_only(message=None):
    """Only a person may run this; a proactive turn may not."""
    def check(ctx):
        if ctx.origin == "user":
            return ""
        return message or (
            f"[Blocked] {ctx.action_type} may only be run when Himanshu asks "
            f"for it, not on VANTA's own initiative.")
    check.label = "user_only"
    return check


def open_project_required(ctx):
    """A project file action needs a project open."""
    import projects
    named = str(ctx.attrs.get("project", "") or "").strip()
    if named:
        return ""
    if projects.get_active(ctx.base_dir):
        return ""
    return ("[Error] No project is open. Use OPEN_PROJECT first, or "
            "LIST_PROJECTS to see what exists.")


open_project_required.label = "open_project_required"


# --- registrations ------------------------------------------------------ #
# --- receipt verification ---------------------------------------------- #
#: What a success receipt must look like to be evidence rather than a claim.
#: The recurring defect in this system is an action reporting success it did
#: not earn -- a calendar receipt that was a bare URL, a typing receipt that
#: reported len(text) while a character had been dropped, a draw that rendered
#: nothing and said it had. Every one of those receipts asserted. None cited.
_EVIDENCE_MARKS = ("0", "1", "2", "3", "4", "5", "6", "7", "8", "9",
                   "http://", "https://", "\\", "/", "id=", "=")


def must_cite_evidence(ctx, receipt):
    """A success must carry something checkable.

    Deliberately crude: a number, a path, an id or a URL. It cannot tell a
    true receipt from a convincing one, and it is not meant to -- it catches
    the specific shape of "[Success] done", which is the shape a receipt takes
    when nobody has thought about what evidence the action could return.
    """
    text = str(receipt or "")
    if not text.startswith("[Success]"):
        return ""          # a refusal or an error needs no evidence
    if any(mark in text[len("[Success]"):] for mark in _EVIDENCE_MARKS):
        return ""
    return ("it reports success without citing anything -- no identifier, "
            "count, path or URL that could be checked")


def must_name(*attributes):
    """A success must name back the thing it was told to act on.

    A receipt that does not repeat its target cannot distinguish "sent to
    Madhu" from "sent to the last contact that happened to be resolved".
    """
    wanted = tuple(attributes)

    def check(ctx, receipt):
        text = str(receipt or "")
        if not text.startswith("[Success]"):
            return ""
        for name in wanted:
            value = str(ctx.attrs.get(name, "") or "").strip()
            if value and value not in text:
                return (f"it reports success without naming the {name} it "
                        f"acted on ({value!r})")
        return ""

    check.label = "must_name(" + ", ".join(wanted) + ")"
    return check


must_cite_evidence.label = "must_cite_evidence"


class Registration:
    """One handler, its names, and the controls declared around it."""

    __slots__ = ("names", "handler", "family", "preconditions", "receipt",
                 "verifiers", "doc", "_contracts", "_by_name")

    def __init__(self, names, handler, family="", preconditions=(),
                 receipt="", verify=(), doc=""):
        self.names = tuple(names)
        self.handler = handler
        self.family = family or (names[0] if names else "")
        self.preconditions = tuple(preconditions)
        self.receipt = receipt
        self.verifiers = tuple(verify) if isinstance(verify, (list, tuple)) \
            else (verify,)
        self.doc = doc or (handler.__doc__ or "").strip().split("\n")[0]
        #: Contracts and checks that belong to one name rather than the whole
        #: family this handler serves.
        self._contracts = {}
        self._by_name = {}

    def set_contract(self, action_type, contract, verify=()):
        self._contracts[action_type] = contract
        if verify:
            existing = self._by_name.get(action_type, ())
            added = tuple(v for v in verify if v not in existing
                          and v not in self.verifiers)
            self._by_name[action_type] = existing + added

    def run(self, ctx):
        """Apply the declared controls, then the handler, then the contract."""
        started = time.perf_counter()
        completed = False
        try:
            for check in self.preconditions:
                message = check(ctx)
                if message:
                    ctx.report(message)
                    ctx.say(message)
                    return FLOW_NEXT
            if ctx.legacy is not None:
                ctx.legacy._last_output = None
            signal = self.handler(ctx)
            # Older handlers write through the legacy bridge. Their receipts
            # must pass the same verifiers as handlers using ctx.report().
            if not ctx.receipt and ctx.legacy is not None:
                text = getattr(ctx.legacy, '_last_output', None)
                if isinstance(text, str) and text:
                    ctx.report(text)
            self._check_receipt(ctx)
            completed = True
            return signal if signal in FLOW_SIGNALS else FLOW_NEXT
        finally:
            from performance_metrics import record
            record('action', ctx.action_type, time.perf_counter() - started,
                   ok=completed and bool(ctx.receipt) and not ctx.receipt.casefold().startswith(
                       ('[error]', '[unverified]', '[blocked]', '[interrupted]', '[browser error]')))

    def verifiers_for(self, action_type):
        """The contract checks for one name.

        Per name, not per registration: one handler serves a whole family --
        the seven Drive actions share one -- so a contract stored on the
        registration meant whichever name was applied last overwrote the rest,
        and DRIVE_UPLOAD ended up owing "the permission id" because
        DRIVE_SHARE was applied after it.
        """
        return self.verifiers + self._by_name.get(action_type, ())

    def contract_for(self, action_type):
        return self._contracts.get(action_type, "") or self.receipt

    def _check_receipt(self, ctx):
        """Hold the handler to its declared receipt contract.

        A receipt that fails is downgraded rather than deleted: the work may
        well have happened, and saying otherwise would be its own false
        claim. What it stops is the turn treating an unevidenced success as a
        verified one -- which is how a task reports itself complete while the
        thing it was asked to do never occurred.
        """
        for verifier in self.verifiers_for(ctx.action_type):
            try:
                problem = verifier(ctx, ctx.receipt)
            except Exception as exc:
                problem = f'receipt verification failed ({type(exc).__name__})'
            if problem:
                text = ctx.receipt
                if text.startswith('[Success]'):
                    text = text[len('[Success]'):].strip()
                ctx.report(
                    "[Unverified] " + text
                    + f"\nThis is not confirmed: {problem}. Treat it as "
                    f"unknown and check before reporting it as done.")
                return

    def __repr__(self):
        return f"Registration({self.names!r}, family={self.family!r})"


def _unregistered(ctx):
    """What happens for a name with no handler: nothing, loudly.

    This is the old chain's final `else` as a table entry. It matters that it
    is one: default-deny that lives in a fall-through is default-deny nobody
    can enumerate or test.
    """
    import unavailability
    ctx.report(unavailability.Unavailable(
        ctx.action_type, unavailability.ABSENT_FROM_REGISTRY,
        "It has no handler on the action bus, so nothing would run. "
        "Use DISCOVER_ACTIONS to find the capability that does exist."
    ).for_model())
    ctx.say(ctx.receipt)
    return FLOW_NEXT


UNREGISTERED = Registration(("<unregistered>",), _unregistered,
                            family="<none>",
                            receipt="an explanation, never a success")


class ActionBus:
    """The table. One instance, built at import time by the registrations."""

    def __init__(self):
        self._table = {}
        self._registrations = []

    # -- building ------------------------------------------------------ #
    def register(self, *names, family="", requires=(), receipt="", verify=()):
        """Decorator. Binds one handler to one or more action names."""
        if not names:
            raise ValueError("an action registration needs at least one name")

        def decorate(handler):
            upper = tuple(str(name).upper() for name in names)
            clashes = [name for name in upper if name in self._table]
            if clashes:
                # Two handlers for one name means one of them is unreachable,
                # and which one depends on import order. That was a real risk
                # in the ladder, where the same name appeared on two rungs and
                # the second was dead code.
                raise ValueError(
                    f"{', '.join(clashes)} already has a handler "
                    f"({self._table[clashes[0]].family})")
            registration = Registration(
                upper, handler, family=family,
                preconditions=tuple(requires) if isinstance(requires, (list, tuple))
                else (requires,),
                receipt=receipt, verify=verify)
            self._registrations.append(registration)
            for name in upper:
                self._table[name] = registration
            return handler
        return decorate

    # -- using --------------------------------------------------------- #
    def dispatch(self, ctx):
        """Run the handler for this action. A lookup, not a search."""
        return self._table.get(ctx.action_type, UNREGISTERED).run(ctx)

    def resolve(self, name):
        return self._table.get(str(name or "").upper())

    def handles(self, name):
        return str(name or "").upper() in self._table

    def names(self):
        return set(self._table)

    def registrations(self):
        return list(self._registrations)

    def families(self):
        groups = {}
        for registration in self._registrations:
            groups.setdefault(registration.family, []).extend(
                registration.names)
        return groups

    def apply_contracts(self, contracts, verified=()):
        """Attach receipt contracts to already-registered actions.

        A table rather than 53 edits to 53 decorators, so that the contracts
        can be read as a list of what each action owes -- and so that adding
        one is adding a line. `verified` names the actions whose receipts are
        additionally held to that contract at run time; a contract without
        verification is documentation, which is worth having but is not a
        control.
        """
        unknown = sorted(set(contracts) - set(self._table))
        missing_contract = sorted(set(verified) - set(contracts))
        applied = 0
        for name, contract in contracts.items():
            key = str(name).upper()
            registration = self._table.get(key)
            if registration is None:
                continue
            registration.set_contract(
                key, contract,
                verify=(must_cite_evidence,) if key in verified else ())
            applied += 1
        return {"applied": applied, "unknown": unknown,
                "verified_without_contract": missing_contract}

    def contract(self, name):
        """What this action owes as evidence, in words."""
        key = str(name or "").upper()
        registration = self._table.get(key)
        return registration.contract_for(key) if registration else ""

    def verified_names(self):
        """Actions whose receipts are checked, not merely documented."""
        return sorted(
            name for name, registration in self._table.items()
            if registration.verifiers_for(name))

    def without_receipt_contract(self):
        """Registered actions that have not said what evidence they return."""
        return sorted(name for name, registration in self._table.items()
                      if not registration.contract_for(name))

    def handler_source(self, name):
        """The source of the handler for this action.

        Several tests assert a property by reading the code that implements
        it -- that a gate runs before the thing it gates, that a timeout is
        bounded. They used to find it by slicing actions.py between two
        `elif action_type ==` markers, which is guesswork that breaks whenever
        a neighbouring action is renamed. The table knows which function it
        is, so it can simply be asked for it.
        """
        import inspect
        registration = self.resolve(name)
        if registration is None:
            return ""
        try:
            return inspect.getsource(registration.handler)
        except (OSError, TypeError):
            return ""

    def describe(self, name):
        """Where a name is handled and under what controls."""
        registration = self.resolve(name)
        if registration is None:
            return f"{name}: no handler on the bus"
        controls = ", ".join(getattr(check, "label", getattr(check, "__name__", "check"))
                             for check in registration.preconditions)
        return (f"{name}: {registration.family} "
                f"({registration.handler.__name__})"
                + (f" requires {controls}" if controls else ""))


#: The one bus. Handlers register against this at import time.
BUS = ActionBus()


def action(*names, family="", requires=(), receipt="", verify=()):
    """Register a handler on the shared bus.

    Adding a capability is this decorator plus a function. Nothing else needs
    editing for it to be dispatchable -- which is the whole point, since the
    thing that used to need editing was a ladder 3,500 lines long.
    """
    return BUS.register(*names, family=family, requires=requires,
                        receipt=receipt, verify=verify)
