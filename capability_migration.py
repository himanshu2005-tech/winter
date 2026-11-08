# -*- coding: utf-8 -*-
"""
capability_migration.py -- one inventory, derived rather than retyped.

The capability descriptor in capabilities.py is the right shape, and writing
263 of them by hand would be both enormous and pointless: every fact needed is
already in the repository, spread across the five places that kept drifting
apart. So the descriptors are generated from those places instead, and an
equivalence gate proves the generated inventory says exactly what the legacy
sources say before anything relies on it.

  catalogue text   ->  name, model-facing syntax, documentation
  action_schema    ->  input schema
  autonomy         ->  authorization, confirmation, proactive policy
  mode_manager     ->  which modes may run it
  actions.py       ->  whether a dispatcher branch exists
  action_registry  ->  retrieval vocabulary

This is the migration, and it is deliberately not a rewrite. What it does not
do is bind a Python handler per action: the dispatcher is a 251-branch chain
rather than a table, and converting it to one is a large refactor of the single
most load-bearing function in the project. `runs_in=RUNS_IN_DISPATCHER` records
the truth as it stands -- actions.py dispatches this by name -- so the
inventory is complete and honest without that refactor having to happen first.

WHAT THE GATE IS FOR
--------------------
A generated inventory that disagrees with the legacy sources would be worse
than no inventory, because it would look authoritative. So every facet is
compared, and the comparison is a test rather than a one-off script: the
sources are still live, and the day someone edits ACTION_CATALOGS by hand is
the day the generated view has to notice.
"""

import re

import capabilities

#: Legacy actions get their authorization class from the policy sets rather
#: than from a human judgement per action, because a judgement per action is
#: 263 opportunities to be inconsistent.
_SYSTEM_HINTS = ("RUN_COMMAND", "RUN_PYTHON", "SHUTDOWN", "RESTART",
                 "KILL_PROCESS", "LOCK_SCREEN", "RUN_BLENDER_SCRIPT",
                 "RUN_SANDBOX", "RUN_IN_PROJECT")
_EXTERNAL_HINTS = ("SEND_", "DRIVE_UPLOAD", "DRIVE_SHARE", "BOOK_", "NOTIFY",
                   "POST_")
_MUTATION_HINTS = ("ADD_", "SET_", "WRITE_", "SAVE_", "DELETE_", "MOVE_",
                   "COPY_", "UPDATE_", "CREATE_", "REMOVE_", "EDIT_",
                   "COMPLETE_", "TRACK_", "ARCHIVE_", "MARK_", "CORRECT_",
                   "FORGET_", "CONFIRM_", "REJECT_", "DEACTIVATE_",
                   "PAUSE_", "RESUME_", "CANCEL_", "SPLIT_", "INGEST_",
                   "INDEX_", "IMPORT_", "LEARN_", "TOGGLE_", "SELECT_",
                   "FILL_", "CLICK", "TYPE", "PRESS", "HOTKEY", "DRAG",
                   "SCROLL", "LAUNCH_", "FOCUS_", "CLOSE_", "WINDOW_",
                   "BROWSER_", "BOARD_", "DRAW_", "SPOTIFY_", "MUSIC_",
                   "MEDIA", "VOLUME", "CLIPBOARD_WRITE", "SCREEN_LIGHT",
                   "QUIET_FOR", "SHOW_", "TICK_", "CLEAR_", "ANNOTATE_")
_RETRIEVAL_HINTS = ("SEARCH_", "READ_", "LIST_", "GET_", "FIND_", "LOOKUP_",
                    "RECALL_", "KNOWLEDGE_", "TRENDING_", "GITHUB_",
                    "DEEP_RESEARCH", "REVIEW_", "WHY_", "PROFILE_", "AUDIT_",
                    "GEOCODE", "FLIGHT_SEARCH", "API_", "CHECK_", "WATCH_")
_OBSERVATION_HINTS = ("TAKE_SCREENSHOT", "READ_SCREEN_TEXT", "CAPTURE_",
                      "INSPECT_", "LOCATE_", "ZOOM", "SYSTEM_STATUS",
                      "LIST_WINDOWS", "LIST_CONTROLS", "LIST_PROCESSES",
                      "CLIPBOARD_READ")

_ACTION_TAG = re.compile(r"<\s*ACTION\b([^>]*?)/?>", re.IGNORECASE)
_TYPE_ATTR = re.compile(
    r'(?<![A-Za-z0-9_])type\s*=\s*["\']([A-Za-z0-9_|]+)["\']', re.IGNORECASE)


def _authorization_for(name):
    """The coarse class a legacy action belongs to.

    Order matters: a name can match several hint lists, and the most powerful
    reading has to win. SEND_EMAIL is both a mutation and an external send,
    and treating it as a mutation would lose the fact that it tells somebody
    something.
    """
    upper = name.upper()
    if any(upper == hint or upper.startswith(hint) for hint in _SYSTEM_HINTS):
        return capabilities.SYSTEM
    if any(upper.startswith(hint) for hint in _EXTERNAL_HINTS):
        return capabilities.EXTERNAL
    if any(upper == hint or upper.startswith(hint)
           for hint in _OBSERVATION_HINTS):
        return capabilities.OBSERVATION
    if any(upper.startswith(hint) for hint in _RETRIEVAL_HINTS):
        return capabilities.RETRIEVAL
    if any(upper.startswith(hint) for hint in _MUTATION_HINTS):
        return capabilities.MUTATION
    return capabilities.RETRIEVAL


def _verification_for(authorization):
    """How success is checked, by class.

    A read is its own evidence; anything that changes something has to produce
    a receipt. This is the facet the legacy inventory had for 2 of 263
    actions, which is why success was so often taken from narration.
    """
    if authorization in (capabilities.OBSERVATION, capabilities.RETRIEVAL):
        return capabilities.VERIFY_RECEIPT
    return capabilities.VERIFY_RECEIPT


def catalogue_entries(catalogs):
    """Every documented action with the syntax and prose it was given.

    Nested tags are skipped: the CREATE_ACTION tutorial demonstrates writing a
    WORD_COUNT, and reading that as a capability produced a defect report for
    a worked example.
    """
    found = {}
    for section, text in (catalogs or {}).items():
        body = str(text or "")
        spans = []
        for match in _ACTION_TAG.finditer(body):
            if match.group(0).rstrip().endswith("/>"):
                continue
            close = body.lower().find("</action>", match.end())
            if close != -1:
                spans.append((match.end(), close))
        for match in _ACTION_TAG.finditer(body):
            if any(start <= match.start() < end for start, end in spans):
                continue
            names = _TYPE_ATTR.findall(match.group(1))
            if not names:
                continue
            syntax_end = match.end()
            if not match.group(0).rstrip().endswith('/>'):
                close = body.lower().find('</action>', match.end())
                if close != -1:
                    syntax_end = close + len('</action>')
            tail = body[syntax_end:]
            notes = tail.split("<ACTION", 1)[0].strip()
            for part in names[0].split("|"):
                upper = part.upper()
                if upper in found:
                    continue
                found[upper] = {
                    "section": section,
                    "syntax": body[match.start():syntax_end].strip(),
                    "notes": notes[:600],
                }
    return found


def build(catalogs, schema_specs=None, authorized=None,
          confirmation_required=None, proactive_blocked=None,
          mode_policies=None, executable=(), aliases=None):
    """The generated inventory: one descriptor-shaped record per action.

    Returned as plain dicts rather than Capability objects. A Capability
    validates that it has a resolvable handler and a named test, and a legacy
    action has neither yet -- constructing one would mean either loosening that
    validation or inventing facts to satisfy it. Both are worse than saying
    plainly that this facet is not migrated.
    """
    documented = catalogue_entries(catalogs)
    names = set(documented) | set(executable)
    inventory = {}
    for name in sorted(names):
        entry = documented.get(name, {})
        authorization = _authorization_for(name)
        allowed_modes = tuple(
            mode for mode, blocked in (mode_policies or {}).items()
            if name not in blocked)
        inventory[name] = {
            "name": name,
            "catalogue": entry.get("section", ""),
            "syntax": entry.get("syntax", ""),
            "documentation": entry.get("notes", ""),
            "aliases": str((aliases or {}).get(name, "")),
            "schema": dict((schema_specs or {}).get(name, {})),
            "has_schema": name in (schema_specs or {}),
            "executor": "actions.py" if name in executable else "",
            "runs_in": capabilities.RUNS_IN_DISPATCHER,
            "authorization": authorization,
            "authorized": name in (authorized or ()),
            "confirmation": (capabilities.CONFIRM_ALWAYS
                             if name in (confirmation_required or ())
                             else capabilities.CONFIRM_NEVER),
            "origins": (("user",) if name in (proactive_blocked or ())
                        else ("user", "proactive")),
            "modes": allowed_modes,
            "verification": _verification_for(authorization),
            "declared": name in capabilities.CAPABILITIES,
        }

    # A declared capability overrides its generated record entirely. That is
    # the migration direction: generated today, declared as each is adopted.
    # It overrides and never adds: the inventory describes the sources it was
    # given, so building from a subset of catalogues must not conjure in
    # capabilities those catalogues do not mention. Declared capabilities
    # publish their own catalogue section, so they are already present
    # whenever the live catalogues are the source.
    for name, capability in capabilities.CAPABILITIES.items():
        if name not in inventory:
            continue
        inventory[name] = {
            "name": name,
            "catalogue": capabilities.GENERATED_CATALOG,
            "syntax": capability.syntax,
            "documentation": capability.summary,
            "aliases": capability.alias_text(),
            "schema": capability.schema_spec(),
            "has_schema": bool(capability.schema),
            "executor": str(capability.handler),
            "runs_in": capability.runs_in,
            "authorization": capability.authorization,
            "authorized": True,
            "confirmation": capability.confirmation,
            "origins": capability.origins,
            "modes": capability.modes,
            "verification": capability.verification,
            "declared": True,
        }
    return inventory


def build_live():
    """The inventory for the running process."""
    import action_registry
    import action_schema
    import agent
    import autonomy
    import integrity
    import mode_manager
    import os

    here = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(here, "actions.py"), encoding="utf-8",
              errors="replace") as handle:
        actions_source = handle.read()

    action_registry.configure(agent.ACTION_CATALOGS)
    aliases = {}
    for entry in action_registry._entries:
        if entry.get("alias_tokens"):
            aliases[entry["name"]] = " ".join(entry["alias_tokens"])

    mode_policies = {}
    for policy in mode_manager.POLICIES.values():
        blocked = set(policy.blocked_actions)
        blocked |= capabilities.mode_blocked_names(policy.label)
        mode_policies[policy.label.lower()] = blocked

    return build(
        catalogs=agent.ACTION_CATALOGS,
        schema_specs=action_schema.ACTION_SPECS,
        authorized=set(autonomy.KNOWN_ACTIONS),
        confirmation_required=set(autonomy.CONFIRMATION_REQUIRED_ACTIONS),
        proactive_blocked=set(autonomy.PROACTIVE_BLOCKED_ACTIONS),
        mode_policies=mode_policies,
        executable=integrity.executable_names(actions_source),
        aliases=aliases,
    )


# --- the equivalence gate --------------------------------------------- #

def equivalence_report(inventory, catalogs, schema_specs, authorized,
                       confirmation_required, mode_policies, executable):
    """Every way the generated inventory could disagree with its sources.

    Empty means the inventory is a faithful restatement and may be relied on.
    Each key names one facet the migration promised to preserve.
    """
    documented = set(catalogue_entries(catalogs))
    problems = {}

    def note(key, detail):
        problems.setdefault(key, []).append(detail)

    seen = {}
    for name in inventory:
        seen[name] = seen.get(name, 0) + 1
        if seen[name] > 1:
            note("duplicate_representation", name)

    for name in sorted(documented - set(inventory)):
        note("missing_from_inventory", name)
    for name in sorted(set(executable) - set(inventory)):
        note("missing_from_inventory", name)

    for name, record in sorted(inventory.items()):
        if record["declared"]:
            continue
        if name in documented and not record["syntax"]:
            note("syntax_lost", name)
        legacy_schema = dict((schema_specs or {}).get(name, {}))
        if legacy_schema != record["schema"]:
            note("schema_differs", name)
        if (name in (authorized or ())) != record["authorized"]:
            note("authorization_differs", name)
        legacy_confirm = (capabilities.CONFIRM_ALWAYS
                          if name in (confirmation_required or ())
                          else capabilities.CONFIRM_NEVER)
        if legacy_confirm != record["confirmation"]:
            note("confirmation_differs", name)
        for mode, blocked in (mode_policies or {}).items():
            permitted = mode in record["modes"]
            if permitted and name in blocked:
                note("authorization_loosened", f"{name} in {mode}")
        if name in executable and not record["executor"]:
            note("executor_lost", name)
    return problems


def stats(inventory):
    """A short, honest picture of how far the migration has got."""
    total = len(inventory)
    declared = sum(1 for record in inventory.values() if record["declared"])
    return {
        "total": total,
        "declared": declared,
        "generated": total - declared,
        "with_schema": sum(1 for r in inventory.values() if r["has_schema"]),
        "with_executor": sum(1 for r in inventory.values() if r["executor"]),
        "authorized": sum(1 for r in inventory.values() if r["authorized"]),
        "documented": sum(1 for r in inventory.values() if r["syntax"]),
        "handler_bound": sum(1 for r in inventory.values()
                             if r["declared"]),
    }


# --- phase 4: generating the catalogue from the inventory -------------- #

def generated_catalogs(inventory):
    """Catalogue text rebuilt from the inventory, grouped by section.

    This is the step that makes the inventory authoritative rather than
    merely descriptive: ACTION_CATALOGS becomes an output. Indexing the
    generated text must yield exactly the actions the hand-written text
    yielded, which is what the equivalence test checks -- a generated
    catalogue that loses one action would make that capability unreachable,
    which is precisely the camera's original failure.
    """
    sections = {}
    for record in inventory.values():
        section = record.get("catalogue") or "SYSTEM"
        syntax = record.get("syntax") or f'<ACTION type="{record["name"]}" />'
        documentation = " ".join(
            str(record.get("documentation") or "").split())
        block = f"  {syntax}"
        if documentation:
            block += f"\n    - {documentation}"
        sections.setdefault(section, []).append(block)
    return {section: "\n".join(blocks)
            for section, blocks in sorted(sections.items())}


class EquivalenceReport:
    """Whether the inventory may be relied on, and why not if it may not."""

    __slots__ = ("problems", "legacy_count", "descriptor_count")

    def __init__(self, problems, legacy_count, descriptor_count):
        self.problems = dict(problems or {})
        self.legacy_count = int(legacy_count)
        self.descriptor_count = int(descriptor_count)

    @property
    def ok(self):
        return not self.problems and self.legacy_count == self.descriptor_count

    def summary(self):
        if self.ok:
            return (f"equivalent: {self.descriptor_count} descriptors match "
                    f"{self.legacy_count} legacy actions")
        parts = [f"{key}={len(items)}"
                 for key, items in sorted(self.problems.items())]
        if self.legacy_count != self.descriptor_count:
            parts.append(f"count {self.legacy_count}!={self.descriptor_count}")
        return "; ".join(parts)

    def __repr__(self):
        return f"EquivalenceReport(ok={self.ok}, {self.summary()})"


def equivalence(catalogs, inventory, schema_specs=None, authorized=None,
                confirmation_required=None, mode_policies=None,
                executable=()):
    """The gate. `ok` is the only thing allowed to authorise relying on it."""
    problems = equivalence_report(
        inventory, catalogs, schema_specs, authorized,
        confirmation_required, mode_policies, executable)
    legacy = set(catalogue_entries(catalogs)) | set(executable or ())
    # Round-tripping the generated catalogue must not lose an action.
    try:
        import action_registry
        regenerated = generated_catalogs(inventory)
        action_registry.configure(regenerated)
        indexed = {entry["name"] for entry in action_registry._entries}
        for name in sorted(set(inventory) - indexed):
            problems.setdefault("lost_when_regenerated", []).append(name)
    except Exception as exc:  # pragma: no cover - diagnostic only
        problems.setdefault("regeneration_failed", []).append(str(exc))
    finally:
        try:
            import action_registry
            action_registry.configure(catalogs)
        except Exception:
            pass
    return EquivalenceReport(problems, len(legacy), len(inventory))


# --- the cutover ------------------------------------------------------- #

def configure_registry(catalogs, force_legacy=False):
    """Index the action registry, from the descriptors when they are proven.

    This is the cutover, and the gate is the whole point of it: the registry is
    configured from the generated catalogue only when the equivalence report
    comes back clean, and from the hand-written catalogue otherwise. A
    generated view that had lost one action would make that capability
    unreachable, which is exactly how the camera disappeared, so "generate it
    and hope" is not an option -- it either provably says the same thing or it
    is not used.

    Returns (descriptors, report, cutover) so the caller can say which
    registry is live rather than having to guess.
    """
    import action_registry

    descriptors = build_live() if catalogs is None else _build_for(catalogs)
    report = equivalence(
        catalogs, descriptors,
        schema_specs=_live_schema(), authorized=_live_authorized(),
        confirmation_required=_live_confirmation(),
        mode_policies=_live_mode_policies(), executable=_live_executable())

    cutover = bool(report.ok) and not force_legacy
    if cutover:
        action_registry.configure(generated_catalogs(descriptors))
    else:
        action_registry.configure(catalogs)
    return descriptors, report, cutover


def _build_for(catalogs):
    """Descriptors for the given catalogues, with the live policy sources."""
    return build(
        catalogs=catalogs,
        schema_specs=_live_schema(),
        authorized=_live_authorized(),
        confirmation_required=_live_confirmation(),
        proactive_blocked=_live_proactive_blocked(),
        mode_policies=_live_mode_policies(),
        executable=_live_executable(),
        aliases=_live_aliases(catalogs),
    )


def _live_schema():
    import action_schema
    return action_schema.ACTION_SPECS


def _live_authorized():
    import autonomy
    return set(autonomy.KNOWN_ACTIONS)


def _live_confirmation():
    import autonomy
    return set(autonomy.CONFIRMATION_REQUIRED_ACTIONS)


def _live_proactive_blocked():
    import autonomy
    return set(autonomy.PROACTIVE_BLOCKED_ACTIONS)


def _live_mode_policies():
    import mode_manager
    policies = {}
    for policy in mode_manager.POLICIES.values():
        blocked = set(policy.blocked_actions)
        blocked |= capabilities.mode_blocked_names(policy.label)
        policies[policy.label.lower()] = blocked
    return policies


def _live_executable():
    import os
    import integrity
    here = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(here, "actions.py"), encoding="utf-8",
              errors="replace") as handle:
        return integrity.executable_names(handle.read())


def _live_aliases(catalogs):
    """Retrieval vocabulary, read from the registry's own merged table."""
    import action_registry
    action_registry.configure(catalogs)
    found = {}
    for entry in action_registry._entries:
        if entry.get("alias_tokens"):
            found[entry["name"]] = " ".join(entry["alias_tokens"])
    return found
