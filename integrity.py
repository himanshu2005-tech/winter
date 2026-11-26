# -*- coding: utf-8 -*-
"""
integrity.py -- the consistency check that would have caught the camera.

A capability needs six things to be usable: documentation the model can read,
an entry in the searchable registry, an input schema, an authorization
decision, a mode classification, and something that actually runs. Each lives
in a different module, nothing compared them, and so CAPTURE_CAMERA sat with
two of the six for as long as it took someone to ask what VANTA could see.

The failure was not hard to detect. It was simply never looked for. Everything
here is a set difference over data the process already has in memory; the whole
check runs in milliseconds. It was absent, not expensive.

Reported at startup rather than raised: a drifting action must not stop VANTA
from booting, because an assistant that refuses to start is worse than one with
one unreachable tool. Tests assert the opposite -- there, drift is a failure,
which is what keeps the report from becoming wallpaper nobody reads.
"""

import re


def _active_agent_module():
    """Return the already-running agent when launched as ``agent.py``.

    A script is registered as ``__main__``, not ``agent``.  Importing
    ``agent`` from a startup integrity check would therefore execute all of
    its top-level initialization a second time, including opening another
    microphone stream.  Tests and library callers still import the normal
    module when no live agent script exists.
    """
    import importlib
    import os
    import sys

    running = sys.modules.get("__main__")
    running_path = os.path.normcase(os.path.abspath(
        str(getattr(running, "__file__", "") or "")))
    agent_path = os.path.normcase(os.path.abspath(
        os.path.join(os.path.dirname(__file__), "agent.py")))
    if (running_path == agent_path and
            hasattr(running, "ACTION_CATALOGS")):
        return running
    return importlib.import_module("agent")

#: Problem kinds, in the order they are reported. Roughly severity: something
#: that can never run, then something that cannot be found, then something
#: unvalidated.
EXECUTOR_WITHOUT_REGISTRATION = "executor_without_registration"
REGISTRATION_WITHOUT_EXECUTOR = "registration_without_executor"
UNDISCOVERABLE = "undiscoverable"
MISSING_AUTHORIZATION = "missing_authorization"
MISSING_SCHEMA = "missing_schema"
UNKNOWN_MODE_REFERENCE = "unknown_mode_reference"
DUPLICATE_DEFINITION = "duplicate_definition"
HANDLER_UNRESOLVABLE = "handler_unresolvable"
INTEGRATION_GAP = "integration_gap"

#: Problems that make a capability unusable rather than merely unpolished.
#: Only these are worth printing at startup; the rest are a report to read on
#: purpose. Missing schemas number in the dozens and would bury the signal.
SEVERE = (EXECUTOR_WITHOUT_REGISTRATION, REGISTRATION_WITHOUT_EXECUTOR,
          UNDISCOVERABLE, MISSING_AUTHORIZATION, UNKNOWN_MODE_REFERENCE,
          DUPLICATE_DEFINITION, HANDLER_UNRESOLVABLE)
SEVERE = SEVERE + (INTEGRATION_GAP,)

#: Aliases normalised before the authorization check in actions.py, and the
#: retired *_ID stubs intercepted before it. They are deliberately absent from
#: the registries and must not be reported as drift.
_NOT_DRIFT = frozenset({
    "MOUSE_MOVE", "MOUSE_DRAG", "DOUBLECLICK", "RIGHTCLICK",
    "CLICK_ID", "DOUBLE_CLICK_ID", "RIGHT_CLICK_ID",
    "DOUBLECLICK_ID", "RIGHTCLICK_ID",
})

_ACTION_TAG = re.compile(r"<\s*ACTION\b([^>]*?)/?>", re.IGNORECASE)
#: The word boundary matters. Without it this matches the tail of
#: `control_type=` and `search_type=` and reports healthy entries as having two
#: type attributes -- which is exactly the false alarm it produced first time.
_TYPE_ATTR = re.compile(
    r'(?<![A-Za-z0-9_])type\s*=\s*["\']([A-Za-z0-9_|]+)["\']', re.IGNORECASE)


class Problem:
    """One inconsistency, with enough detail to act on it."""

    __slots__ = ("kind", "action", "detail")

    def __init__(self, kind, action, detail):
        self.kind = kind
        self.action = action
        self.detail = detail

    @property
    def severe(self):
        return self.kind in SEVERE

    def __str__(self):
        return f"{self.action}: {self.detail}"

    def __repr__(self):
        return f"Problem({self.kind!r}, {self.action!r})"


def _example_spans(text):
    """Character ranges holding the body of a block-form ACTION tag.

    A tag inside another tag's body is an example of an action, not an action.
    The CREATE_ACTION entry teaches the model how to author one by showing a
    WORD_COUNT it writes from scratch, and WORD_COUNT was consequently read as
    a documented capability with no executor -- a defect report for a worked
    example. Excluding nested tags is general, so the next tutorial entry does
    not produce the same false alarm.
    """
    spans = []
    for match in _ACTION_TAG.finditer(text):
        if match.group(0).rstrip().endswith("/>"):
            continue
        close = text.lower().find("</action>", match.end())
        if close != -1:
            spans.append((match.end(), close))
    return spans


def documented_names(catalogs):
    """Action names the model is told about, which is also what is indexed."""
    names = set()
    for text in (catalogs or {}).values():
        text = str(text or "")
        spans = _example_spans(text)
        for match in _ACTION_TAG.finditer(text):
            if any(start <= match.start() < end for start, end in spans):
                continue
            found = _TYPE_ATTR.findall(match.group(1))
            if found:
                names |= {part.upper() for part in found[0].split("|")
                          if part.strip()}
    return names


def duplicate_type_attributes(catalogs):
    """Catalogue tags carrying more than one type attribute.

    A real one is unusable: the attribute parser takes the last value, so the
    documented name is not the name that dispatches.
    """
    found = []
    for section, text in (catalogs or {}).items():
        for match in _ACTION_TAG.finditer(str(text or "")):
            if len(_TYPE_ATTR.findall(match.group(1))) > 1:
                found.append((section, match.group(1).strip()))
    return found


def live_executable_names():
    """Every name the running process can actually run, from the table itself.

    Dispatch used to be an if/elif ladder that nothing could enumerate -- the
    reason `executable_names` below reads source with regexes at all, and the
    reason CAPTURE_CAMERA could be authorized and unreachable at once. The
    action bus answers the question directly, so when the real modules are
    importable this is the authoritative answer and the regexes are only a
    fallback.

    Separate from `executable_names` on purpose: that one is a pure function
    of the text it is handed, and several tests check the extraction itself by
    passing synthetic source. Folding the live table into it made those tests
    see the whole real registry.
    """
    names = set()
    try:
        import action_bus
        import actions as actions_module        # importing registers them
        names |= action_bus.BUS.names()
        # Handled, but by a pipeline table rather than a registration.
        for table in ("POST_LOOP_ACTIONS", "DEFERRED_ACTIONS",
                      "RETIRED_ACTIONS", "ONCE_PER_TASK_ACTIONS"):
            names |= set(getattr(actions_module, table, ()) or ())
    except Exception as exc:
        print(f"[Integrity] Action bus unavailable; falling back to reading "
              f"the source: {exc}")
    return names


def executable_names(actions_source):
    """Every name a dispatcher source text can run.

    A pure function of the text. Four paths count: the `action_type ==`
    comparisons, the membership tests, POST_LOOP_ACTIONS, and the two regex
    alternations for bare and body-carrying tags. Deriving this from one of
    them -- the obvious mistake -- reports the other three as missing
    executors. For the running system, prefer `live_executable_names`.
    """
    text = str(actions_source or "")
    names = set(re.findall(r"action_type\s*==\s*['\"]([A-Z0-9_]+)['\"]", text))
    # Registrations on the action bus. The principal dispatch path now, and
    # visible in the text, so the pure scan must read it or it reports every
    # migrated action as having no executor.
    for match in re.finditer(r"@action\(([^)]*)\)", text, re.S):
        names |= set(re.findall(r"['\"]([A-Z0-9_]+)['\"]", match.group(1)))
    for match in re.finditer(r"action_type\s+in\s*[\(\{]([^)}]*)[\)\}]", text):
        names |= set(re.findall(r"['\"]([A-Z0-9_]+)['\"]", match.group(1)))
    for match in re.finditer(
            r"(?:POST_LOOP_ACTIONS|DEFERRED_ACTIONS)\s*=\s*(?:frozenset\()?\{([^}]*)\}", text):
        names |= set(re.findall(r"['\"]([A-Z0-9_]+)['\"]", match.group(1)))
    # The bare-tag and body-tag regexes name their actions in an alternation.
    for match in re.finditer(r"<\(\(\?:([A-Z_|]{8,})\)", text):
        names |= set(match.group(1).split("|"))
    for match in re.finditer(r'type="\(\?:([A-Z_|]{8,})\)"', text):
        names |= set(match.group(1).split("|"))
    return {name for name in names if re.fullmatch(r"[A-Z][A-Z0-9_]{2,}", name)}


def mode_referenced_names(mode_source):
    """Names referenced by the mode policy groups."""
    names = set()
    pattern = (r"_(?:NETWORK|DESKTOP|OBSERVATION|CODE|FILE_WRITE)_ACTIONS"
               r"\s*=\s*frozenset\(\{([^}]*)\}")
    for match in re.finditer(pattern, str(mode_source or "")):
        names |= set(re.findall(r"['\"]([A-Z0-9_]+)['\"]", match.group(1)))
    return names


def check(catalogs, actions_source, mode_source, schema_names,
          authorized_names, declared=None, indexed=None, executable=None):
    """Every inconsistency across the six layers.

    `declared` is the capabilities.py registry. Capabilities declared there run
    from the agent loop rather than the dispatcher, so they are exempt from the
    executor check and validated against their own handler instead.

    `executable` is the set of names that can actually be dispatched. Passed in
    by `check_live` from the action bus, which knows exactly; derived from the
    source text when it is not, which is a guess made of regexes.
    """
    declared = declared or {}
    documented = documented_names(catalogs)
    if executable is None:
        executable = executable_names(actions_source)
    mode_refs = mode_referenced_names(mode_source)
    schema_names = set(schema_names or ())
    authorized = set(authorized_names or ())
    declared_names = {str(name).upper() for name in declared}

    problems = []

    def add(kind, action, detail):
        if action in _NOT_DRIFT:
            return
        problems.append(Problem(kind, action, detail))

    # A declared capability runs through its own handler, so it is "executable"
    # for the purposes of every check below.
    runnable = executable | declared_names

    for action in sorted(executable - authorized):
        add(MISSING_AUTHORIZATION, action,
            "has an executor but is absent from autonomy.KNOWN_ACTIONS, so "
            "default-deny refuses it at runtime.")

    for action in sorted(documented - runnable):
        add(REGISTRATION_WITHOUT_EXECUTOR, action,
            "is documented to the model but nothing dispatches it.")

    # Measured against what the registry has actually indexed, not against
    # the hand-written catalogues. Once the descriptor cutover is active the
    # registry is built from the generated inventory, which covers actions the
    # prose catalogues forgot -- so reporting those as undiscoverable would be
    # warning about a gap that is no longer there at runtime.
    reachable = {str(name).upper() for name in indexed} if indexed else documented
    for action in sorted(runnable - reachable):
        add(UNDISCOVERABLE, action,
            "can run but is not indexed in the searchable registry, so "
            "DISCOVER_ACTIONS can never return it.")

    for action in sorted(authorized - runnable - documented - reachable):
        add(EXECUTOR_WITHOUT_REGISTRATION, action,
            "is authorized but neither documented nor dispatched anywhere.")

    for action in sorted(mode_refs - authorized):
        add(UNKNOWN_MODE_REFERENCE, action,
            "is classified by a mode policy but unknown to authorization.")

    for action in sorted((documented | runnable) - schema_names):
        add(MISSING_SCHEMA, action,
            "has no entry in action_schema.ACTION_SPECS, so its input is "
            "never validated before the handler runs.")

    for section, attrs in duplicate_type_attributes(catalogs):
        problems.append(Problem(
            DUPLICATE_DEFINITION, section,
            f"catalogue tag carries two type attributes, so the dispatched "
            f"name is not the documented one: {attrs[:120]}"))

    # Declared capabilities promise a resolvable handler. A typo in a
    # "module:function" string is otherwise found only when the capability is
    # first used, which for the camera would mean found by the user.
    for name in sorted(declared_names):
        capability = declared[name]
        try:
            capability.resolve_handler()
        except Exception as exc:
            problems.append(Problem(
                HANDLER_UNRESOLVABLE, name,
                f"handler {capability.handler!r} could not be resolved: "
                f"{type(exc).__name__}: {exc}"))

    return problems


def by_kind(problems):
    grouped = {}
    for problem in problems or ():
        grouped.setdefault(problem.kind, []).append(problem)
    return grouped


def summary(problems):
    """One line per kind, counts only."""
    grouped = by_kind(problems)
    if not grouped:
        return "capability registries are consistent"
    return "; ".join(f"{kind}={len(items)}"
                     for kind, items in sorted(grouped.items()))


def check_live(declared=None):
    """Run the check against the running process's own modules."""
    import os
    import action_schema
    import autonomy
    agent = _active_agent_module()

    here = os.path.dirname(os.path.abspath(__file__))

    def read(name):
        with open(os.path.join(here, name), encoding="utf-8",
                  errors="replace") as handle:
            return handle.read()

    if declared is None:
        try:
            import capabilities
            declared = capabilities.CAPABILITIES
        except Exception:
            declared = {}

    # What the live registry actually holds, so the report describes the
    # running system rather than whichever import happened to touch the
    # registry first.  Normal startup performs the descriptor cutover before
    # this check, but library callers and tests do not run startup.  Reading a
    # half-configured legacy index there produced a false backlog of
    # "undiscoverable" actions even though the production cutover indexed
    # them.  Configure through the same equivalence-gated path first; if that
    # diagnostic setup itself fails, keep checking the entries that exist.
    indexed = None
    try:
        import action_registry
        import capability_migration
        capability_migration.configure_registry(agent.ACTION_CATALOGS)
        indexed = {entry["name"] for entry in action_registry._entries}
    except Exception as exc:
        print(f"[Integrity] Could not reproduce the runtime registry: {exc}")
        try:
            indexed = {entry["name"] for entry in action_registry._entries}
        except Exception:
            indexed = None

    problems = check(
        catalogs=agent.ACTION_CATALOGS,
        actions_source=read("actions.py"),
        mode_source=read("mode_manager.py"),
        schema_names=set(action_schema.ACTION_SPECS),
        authorized_names=set(autonomy.KNOWN_ACTIONS),
        declared=declared,
        indexed=indexed,
        executable=live_executable_names() | executable_names(read("actions.py")),
    )
    try:
        import integration_contracts
        available = (live_executable_names()
                     | executable_names(read("actions.py"))
                     | set(declared))
        for integration, missing_actions in integration_contracts.missing(
                available).items():
            for action_name in missing_actions:
                problems.append(Problem(
                    INTEGRATION_GAP, action_name,
                    f"the {integration} integration promises this operation "
                    "but no executable capability is registered."))
    except Exception as exc:
        problems.append(Problem(
            INTEGRATION_GAP, "INTEGRATION_CONTRACTS",
            f"integration coverage could not be audited: {exc}"))
    return problems


def report_at_startup(verbose=False):
    """Print the severe problems. Never raises; boot must not depend on this."""
    try:
        problems = check_live()
    except Exception as exc:
        print(f"[Integrity] Could not run the capability check: {exc}")
        return []
    severe = [problem for problem in problems if problem.severe]
    if not severe:
        unvalidated = len(by_kind(problems).get(MISSING_SCHEMA, ()))
        detail = f" ({unvalidated} without input schema)" if unvalidated else ""
        print(f"[Integrity] Capabilities consistent{detail}.")
        return problems
    print(f"[Integrity] {len(severe)} capability problem(s): "
          f"{summary(severe)}")
    for problem in severe if verbose else severe[:12]:
        print(f"[Integrity]   {problem}")
    if not verbose and len(severe) > 12:
        print(f"[Integrity]   ... and {len(severe) - 12} more")
    return problems
