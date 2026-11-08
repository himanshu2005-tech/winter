# -*- coding: utf-8 -*-
"""Complex requests, decided end to end -- the permanent version.

Ten requests of the kind Himanshu actually makes, each carried through the
real pipeline: COMPILE the request into end states, REACH a capability for
each, SELECT exactly one action per outcome, and confirm each selected action
is WIRED (documented, schema'd, authorized, dispatchable).

Five of these used to compile to GENERIC, which meant nothing was decided and
the model was left to take the top of a shortlist. That is fine when the
shortlist is good and silently wrong when it is not: "apply for my college
gate pass on the student portal" offered TRENDING_NEWS, MOVE_FILE and
COPY_FILE, and nothing in the system would have noticed.

NOTHING IS DISPATCHED HERE
--------------------------
No action runs, so the suite has no side effects: no files written, no
screenshots taken, no messages, no uploads, no browser. It asserts what would
be chosen and whether that choice could work, which is the part that kept
being wrong. Whether each action then behaves is covered by its own tests.

Expectations are written as "any of these would satisfy this outcome" plus
"none of these may be chosen", rather than one exact answer, because several
tools legitimately satisfy some outcomes and pinning one would fail on every
reasonable retuning.
"""

import ast
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import action_registry
import capabilities
import capability_migration
import integrity
import outcome_compiler
import outcome_planner
import outcomes

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _catalogs():
    source = open(os.path.join(BASE_DIR, "agent.py"), encoding="utf-8",
                  errors="replace").read()
    found = {}
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "ACTION_CATALOGS":
                    found = ast.literal_eval(node.value)
    found.update(capabilities.catalog_overlay())
    return found


#: (title, utterance, [per-outcome any_of], never_select)
#:
#: `any_of` has one entry per end state the request contains. The request must
#: produce an action from each.
TASKS = [
    ("jobs researched into a document",
     "find me some AI ML and software engineering jobs and create a word "
     "document with them on my desktop",
     [{"SEARCH_WEB", "KNOWLEDGE_SEARCH", "DEEP_RESEARCH"},
      {"SAVE_FILE", "WRITE_FILE"}],
     {"TRENDING_NEWS", "BROWSER_START", "MOVE_FILE", "COPY_FILE"}),

    ("compound reschedule and reminder",
     "Move my final-year project review to 1 PM and remind me at 4:30 to "
     "start my new project",
     [{"UPDATE_CALENDAR_EVENT"},
      {"SET_ALARM", "ADD_TASK", "ADD_CALENDAR_EVENT"}],
     {"CODING_TASK_START", "CREATE_PROJECT", "GITHUB_READ_REPO",
      "DELETE_CALENDAR_EVENT"}),

    ("upload a file to drive",
     "upload hyperverge.pdf from my vault docs to my google drive",
     [{"DRIVE_UPLOAD", "BROWSER_UPLOAD"}],
     {"SEARCH_WEB", "GET_TRAVEL_TIME", "LIST_DIRECTORY", "MOVE_FILE"}),

    ("a form on a student portal",
     "apply for my college gate pass on the student portal for sunday",
     [{"BROWSER_FILL_FORM", "BROWSER_CLICK", "BROWSER_GOTO"}],
     {"TRENDING_NEWS", "MOVE_FILE", "COPY_FILE", "FLIGHT_WATCH_CREATE"}),

    ("a multi-step job application with a resume",
     "go to my placements portal, open the newest job, add my resume and "
     "submit the application",
     [{"BROWSER_FILL_FORM"}],
     {"DRIVE_UPLOAD", "SEARCH_WEB", "TRENDING_NEWS", "MOVE_FILE"}),

    ("a question about what is in front of him",
     "look through the camera and tell me what i am holding",
     [{"CAPTURE_CAMERA"}],
     {"SEND_WHATSAPP", "SEND_TELEGRAM", "TAKE_SCREENSHOT", "SEARCH_WEB"}),

    ("inbox triaged into tasks",
     "check my email and add a task for anything that needs a reply",
     [{"READ_EMAILS"}, {"ADD_TASK", "ADD_TASKS"}],
     {"SEND_EMAIL", "READ_CALENDAR", "BROWSER_SNAPSHOT"}),

    ("what is on the screen",
     "what is on my screen right now and is there an error in it",
     [{"TAKE_SCREENSHOT", "READ_SCREEN_TEXT"}],
     {"CAPTURE_CAMERA", "SET_BRIGHTNESS", "SHOW_ON_SCREEN"}),

    ("a message and a reschedule",
     "tell madhu i will be late and push my meeting to 2",
     [{"SEND_WHATSAPP", "SEND_TELEGRAM"}, {"UPDATE_CALENDAR_EVENT"}],
     {"SEND_EMAIL", "DELETE_CALENDAR_EVENT", "ADD_CALENDAR_EVENT"}),

    ("teaching with a plot",
     "explain gradient descent on the board and plot the loss curve",
     [{"RESULT_WINDOW"}],
     {"BOARD_HIDE", "BOARD_CLEAR", "SEARCH_WEB"}),

    ("a journey, timed and shown",
     "how long to the airport and show me the route",
     [{"GET_TRAVEL_TIME"}, {"SHOW_ROUTE"}],
     {"BOOK_RIDE", "IDE_SHOW_DIFF", "FLIGHT_SEARCH", "SHOW_ON_SCREEN"}),
]


class ComplexTasks(unittest.TestCase):
    """COMPILE -> REACH -> SELECT -> WIRED, with nothing dispatched."""

    @classmethod
    def setUpClass(cls):
        capability_migration.configure_registry(_catalogs())
        action_registry.warm_semantic_index(background=False)
        cls.inventory = capability_migration.build_live()
        with open(os.path.join(BASE_DIR, "actions.py"), encoding="utf-8",
                  errors="replace") as handle:
            cls.executable = integrity.executable_names(handle.read())

    def plan_for(self, utterance):
        def retrieve(query, limit=8):
            return [item["name"] for item in action_registry.search(
                BASE_DIR, query, None, limit, "user")]

        compiled = outcome_compiler.compile(utterance)
        bound = outcome_planner.bind(compiled, retrieve=retrieve)
        chosen = outcome_planner.select(bound, utterance, [])
        return compiled, bound, chosen

    # --- COMPILE ------------------------------------------------------ #

    def test_every_request_is_understood(self):
        """No request may compile to GENERIC: that is "I cannot name this"."""
        unnamed = []
        for title, utterance, _any_of, _never in TASKS:
            compiled = outcome_compiler.compile(utterance)
            for outcome in compiled.outcomes:
                if outcome.operation == outcomes.GENERIC:
                    unnamed.append(f"{title}: {outcome.outcome_id}")
        self.assertEqual(unnamed, [], f"compiled to GENERIC: {unnamed}")

    def test_every_request_yields_one_outcome_per_end_state(self):
        wrong = []
        for title, utterance, any_of, _never in TASKS:
            compiled = outcome_compiler.compile(utterance)
            if len(compiled.outcomes) != len(any_of):
                wrong.append(
                    f"{title}: wanted {len(any_of)} outcome(s), got "
                    f"{[(o.outcome_id, o.operation) for o in compiled.outcomes]}")
        self.assertEqual(wrong, [], "\n  ".join(wrong))

    def test_argument_text_never_reaches_routing(self):
        """A reminder body or message content must not steer retrieval."""
        leaks = []
        for title, utterance, _any_of, _never in TASKS:
            compiled = outcome_compiler.compile(utterance)
            queries = " ".join(compiled.retrieval_queries()).lower()
            for span in compiled.spans:
                body = span.text.lower().strip()
                if len(body) >= 6 and body in queries:
                    leaks.append(f"{title}: {body!r}")
        self.assertEqual(leaks, [], "\n  ".join(leaks))

    # --- SELECT -------------------------------------------------------- #

    def test_each_outcome_gets_an_action(self):
        missing = []
        for title, utterance, any_of, _never in TASKS:
            _compiled, _bound, chosen = self.plan_for(utterance)
            selected = [item.selected_action for item in chosen]
            for index, acceptable in enumerate(any_of):
                if not (set(selected) & acceptable):
                    missing.append(
                        f"{title}: outcome {index + 1} wanted one of "
                        f"{sorted(acceptable)}, selected {selected}")
        self.assertEqual(missing, [], "\n\n  " + "\n  ".join(missing))

    def test_irrelevant_actions_are_never_selected(self):
        offences = []
        for title, utterance, _any_of, never in TASKS:
            _compiled, _bound, chosen = self.plan_for(utterance)
            selected = {item.selected_action for item in chosen}
            wrong = selected & never
            if wrong:
                offences.append(f"{title}: selected {sorted(wrong)}")
        self.assertEqual(offences, [], "\n  ".join(offences))

    def test_no_action_is_selected_twice(self):
        """Two outcomes resolving to one action does the work twice."""
        duplicates = []
        for title, utterance, _any_of, _never in TASKS:
            _compiled, _bound, chosen = self.plan_for(utterance)
            selected = [item.selected_action for item in chosen]
            if len(selected) != len(set(selected)):
                duplicates.append(f"{title}: {selected}")
        self.assertEqual(duplicates, [], "\n  ".join(duplicates))

    def test_one_action_per_outcome_exactly(self):
        for title, utterance, any_of, _never in TASKS:
            with self.subTest(task=title):
                _compiled, _bound, chosen = self.plan_for(utterance)
                self.assertEqual(len(chosen), len(any_of))
                for item in chosen:
                    self.assertTrue(item.selected_action)

    def test_alternatives_are_recorded_but_not_run(self):
        """Where tools compete, the unchosen ones stay visible as options."""
        for title, utterance, _any_of, _never in TASKS:
            with self.subTest(task=title):
                _compiled, _bound, chosen = self.plan_for(utterance)
                for item in chosen:
                    self.assertNotIn(item.selected_action, item.alternatives)

    # --- WIRED --------------------------------------------------------- #

    def test_every_selected_action_can_actually_run(self):
        problems = []
        for title, utterance, _any_of, _never in TASKS:
            _compiled, _bound, chosen = self.plan_for(utterance)
            for item in chosen:
                name = item.selected_action
                record = self.inventory.get(name)
                if record is None:
                    problems.append(f"{title}: {name} is not in the inventory")
                    continue
                if not record["authorized"]:
                    problems.append(f"{title}: {name} is not authorized")
                if name not in self.executable and not record["declared"]:
                    problems.append(f"{title}: {name} has no handler")
        self.assertEqual(problems, [], "\n  ".join(problems))

    def test_every_selected_action_is_documented(self):
        """Undocumented means absent from the registry the model searches."""
        undocumented = []
        for title, utterance, _any_of, _never in TASKS:
            _compiled, _bound, chosen = self.plan_for(utterance)
            for item in chosen:
                record = self.inventory.get(item.selected_action) or {}
                if not record.get("syntax"):
                    undocumented.append(f"{title}: {item.selected_action}")
        self.assertEqual(undocumented, [], "\n  ".join(undocumented))

    # --- RECEIPT ------------------------------------------------------- #

    def test_every_outcome_states_what_success_requires(self):
        for title, utterance, _any_of, _never in TASKS:
            with self.subTest(task=title):
                compiled = outcome_compiler.compile(utterance)
                for outcome in compiled.outcomes:
                    self.assertTrue(outcome.required_result,
                                    f"{title}/{outcome.outcome_id}")

    def test_a_mutating_outcome_requires_evidence_not_narration(self):
        """A read may be its own evidence; a change may not."""
        mutating = {outcomes.CREATE_DOCUMENT, outcomes.UPLOAD_FILE,
                    outcomes.UPDATE_SCHEDULED_EVENT, outcomes.SEND_MESSAGE,
                    outcomes.CREATE_REMINDER, outcomes.CREATE_TASK}
        for title, utterance, _any_of, _never in TASKS:
            compiled = outcome_compiler.compile(utterance)
            for outcome in compiled.outcomes:
                if outcome.operation not in mutating:
                    continue
                with self.subTest(task=title, outcome=outcome.outcome_id):
                    wording = outcome.required_result.lower()
                    self.assertTrue(
                        any(word in wording for word in
                            ("receipt", "confirms", "exists", "retrievable",
                             "id", "status")),
                        f"{outcome.operation}: {outcome.required_result!r}")


class TheSuiteHasNoSideEffects(unittest.TestCase):
    """Evaluation must not touch anything outside the process."""

    def test_nothing_in_this_module_dispatches_an_action(self):
        source = open(os.path.abspath(__file__), encoding="utf-8").read()
        # Needles assembled at runtime so this check does not match its own
        # list of them -- spelled out, the test failed on its own source.
        forbidden = ("execute_" + "actions(", "sub" + "process",
                     "Image" + "Grab", "pyauto" + "gui", "requests." + "post")
        for needle in forbidden:
            self.assertNotIn(needle, source,
                             f"{needle} would cause a real side effect")


if __name__ == "__main__":
    unittest.main()
