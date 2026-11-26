# -*- coding: utf-8 -*-
"""Opening a page is not an alternative to filling the form on it.

Asked to open a college portal and apply for a gate pass, VANTA opened a
Firefox window and went silent. Twelve model calls, 114,000 tokens, and the
browser sitting on about:blank. The transcript:

    <ACTION type="BROWSER_GOTO" url="https://my.amrita.edu" />
    [Outcomes] Blocked unselected action proposal: The plan did not select
    BROWSER_GOTO for o1; it selected BROWSER_FILL_FORM. BROWSER_GOTO is an
    alternative way to achieve the same outcome, so running it as well would
    do the same thing twice.

Two independent defects, and it needed both to fail this badly.

*The planner contradicted itself.* ALTERNATIVE_GROUPS called
BROWSER_FILL_FORM, BROWSER_CLICK and BROWSER_GOTO interchangeable, while
PREREQUISITE_ACTIONS said -- correctly, in a comment -- that
BROWSER_FILL_FORM "cannot act on a page nobody has opened". The gate obeyed
the alternatives table, so navigation was refused as a duplicate of the thing
that needed it.

*A failed browser step read as a success.* Cornered, the model tried to
navigate with the form filler, `BROWSER_FILL_FORM fields="url=..."`. That
filled nothing and returned {"ok": false}, but _classify_output only read
prefixes and `[Browser Receipt]` is not one, so the ledger recorded a success.
From then on navigation was refused with "o1 is already satisfied", which is
the branch that cannot be released by persistence -- so the turn could never
recover, and ended with <DONE />.
"""

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import actions
import outcome_compiler
import outcome_planner
import outcomes


class StepsAreNotSubstitutes(unittest.TestCase):
    def test_no_prerequisite_is_also_an_alternative(self):
        """The structural invariant. A step that must run before X cannot
        also be a way of avoiding X."""
        self.assertEqual(outcome_planner.contradictions(), [])

    def test_navigation_is_not_an_alternative_to_filling_a_form(self):
        for group in outcome_planner.ALTERNATIVE_GROUPS.values():
            if "BROWSER_FILL_FORM" in group:
                with self.subTest(group=group):
                    self.assertNotIn("BROWSER_GOTO", group)

    def test_a_web_workflow_declares_no_alternatives(self):
        self.assertNotIn(outcomes.WEB_WORKFLOW,
                         outcome_planner.ALTERNATIVE_GROUPS)
        self.assertIn(outcomes.WEB_WORKFLOW,
                      outcome_planner.MULTI_STEP_OPERATIONS)

    def test_filling_a_form_still_needs_the_page_open(self):
        """Removing the false alternative must not lose the true dependency."""
        self.assertIn("BROWSER_GOTO",
                      outcome_planner.PREREQUISITE_ACTIONS["BROWSER_FILL_FORM"])


#: The real request, which compiles to a single WEB_WORKFLOW outcome.
UTTERANCE = "apply for my gate pass on the portal"


def plan_with(candidates, utterance=UTTERANCE):
    """Compile, bind and select, the way a turn does.

    Through the real path rather than by constructing a resolved outcome by
    hand: the defect lived in how selection treated a bound outcome, so a
    hand-built one could agree with the fix and disagree with production.
    """
    import outcome_compiler
    compiled = outcome_compiler.compile(utterance)
    bound = outcome_planner.bind(
        compiled, retrieve=lambda query, limit=8: list(candidates))
    return compiled, outcome_planner.select(bound, utterance, [])


class SelectingAWebWorkflow(unittest.TestCase):
    def select(self, candidates):
        _compiled, chosen = plan_with(candidates)
        return [item for item in chosen
                if item.operation == outcomes.WEB_WORKFLOW]

    def test_it_binds_to_the_act_that_completes_it(self):
        chosen = self.select(["BROWSER_START", "BROWSER_GOTO",
                              "BROWSER_FILL_FORM", "BROWSER_SUBMIT"])
        self.assertEqual(len(chosen), 1)
        self.assertEqual(chosen[0].selected_action, "BROWSER_FILL_FORM")

    def test_it_rejects_nothing(self):
        """The failure exactly: every other step became a refusable duplicate."""
        chosen = self.select(["BROWSER_START", "BROWSER_GOTO",
                              "BROWSER_FILL_FORM", "BROWSER_SUBMIT"])
        self.assertEqual(chosen[0].alternatives, (),
                         "a step of the sequence can still be refused as a "
                         "duplicate of another step")

    def test_it_copes_when_the_completing_action_was_not_shortlisted(self):
        chosen = self.select(["BROWSER_START", "BROWSER_GOTO"])
        self.assertEqual(len(chosen), 1)
        self.assertEqual(chosen[0].alternatives, ())


class NavigationIsNeverRefused(unittest.TestCase):
    """Through the runtime that actually did the refusing."""

    def runtime(self):
        import execution_reliability
        return execution_reliability.OutcomeRuntime(*plan_with(
            ("BROWSER_START", "BROWSER_GOTO", "BROWSER_FILL_FORM")))

    def test_going_to_the_portal_is_allowed(self):
        problem = self.runtime().validate_proposal(
            '<ACTION type="BROWSER_GOTO" url="https://my.amrita.edu" />')
        self.assertEqual(problem, "",
                         "navigating to the page was refused again")

    def test_so_is_every_other_step_of_the_sequence(self):
        runtime = self.runtime()
        for name in ("BROWSER_START", "BROWSER_SNAPSHOT", "BROWSER_CLICK",
                     "BROWSER_TYPE", "BROWSER_UPLOAD", "BROWSER_SUBMIT",
                     "BROWSER_WAIT"):
            with self.subTest(action=name):
                self.assertEqual(
                    runtime.validate_proposal(f'<ACTION type="{name}" />'), "")


class MultiStepApplicationIsOneTransaction(unittest.TestCase):
    def test_read_only_routine_does_not_turn_its_prohibition_into_submission(self):
        """The incident: only the second SAY was heard because DONE was
        rejected for a submission the routine explicitly prohibited."""
        prompt = (
            "Open https://placements.haveloc.com, check for pending jobs and "
            "newly posted internship opportunities, and report the relevant "
            "listings to Himanshu. Do not submit applications or make any "
            "purchases."
        )
        compiled = outcome_compiler.compile(prompt)
        operations = [item.operation for item in compiled.outcomes]
        self.assertEqual(operations, [outcomes.INSPECT_WEB_PAGE])
        self.assertNotIn(outcomes.WEB_WORKFLOW, operations)

    def test_common_negative_scopes_do_not_become_positive_outcomes(self):
        cases = (
            "open the portal but do not submit the application",
            "check the portal without applying or purchasing",
            "visit the website and report the status; never fill the form",
            "browse the portal and avoid uploading anything",
        )
        for prompt in cases:
            with self.subTest(prompt=prompt):
                operations = [item.operation for item in
                              outcome_compiler.compile(prompt).outcomes]
                self.assertNotIn(outcomes.WEB_WORKFLOW, operations)

    def test_positive_submission_and_emphatic_positive_remain_workflows(self):
        for prompt in (
                "open the portal and submit the application",
                "don't forget to submit the application on the portal"):
            with self.subTest(prompt=prompt):
                operations = [item.operation for item in
                              outcome_compiler.compile(prompt).outcomes]
                self.assertIn(outcomes.WEB_WORKFLOW, operations)

    def test_portal_job_resume_and_submit_compile_as_one_workflow(self):
        compiled = outcome_compiler.compile(
            "go to my placements portal, open the newest job, add my resume "
            "and submit the application")
        self.assertEqual(len(compiled.outcomes), 1)
        self.assertEqual(compiled.outcomes[0].operation,
                         outcomes.WEB_WORKFLOW)
        self.assertIn("selected", compiled.outcomes[0].required_result)
        self.assertIn("post-submit", compiled.outcomes[0].required_result)

    def test_job_application_without_repeating_browser_word_is_one_workflow(self):
        compiled = outcome_compiler.compile(
            "scroll through this job application, click add resume, upload "
            "my resume and submit")
        self.assertEqual([item.operation for item in compiled.outcomes],
                         [outcomes.WEB_WORKFLOW])

    def test_genuinely_interchangeable_actions_are_still_refused(self):
        """The gate is not being disabled, only corrected."""
        import execution_reliability
        runtime = execution_reliability.OutcomeRuntime(*plan_with(
            ("SEARCH_WEB", "DEEP_RESEARCH"),
            utterance="research the job market for me"))
        problem = runtime.validate_proposal(
            '<ACTION type="DEEP_RESEARCH" query="jobs" />')
        self.assertIn("did not select", problem)


class AFailedBrowserStepIsAFailure(unittest.TestCase):
    """The flag was added so a form where every field failed would stop
    reading as a login. Nothing downstream read it."""

    def classify(self, payload):
        return actions._classify_output(
            "[Browser Receipt] " + json.dumps(payload))

    def test_a_form_that_filled_nothing_is_not_a_success(self):
        self.assertFalse(self.classify(
            {"ok": False, "operation": "fill_form", "filled": [],
             "failed": ["url: no field by that ref"]}))

    def test_a_real_success_is_still_a_success(self):
        self.assertTrue(self.classify(
            {"ok": True, "operation": "goto", "url": "https://my.amrita.edu"}))

    def test_a_receipt_with_no_flag_is_not_verified_success(self):
        self.assertFalse(self.classify({"operation": "tabs"}))

    def test_an_unreadable_receipt_cannot_complete_an_outcome(self):
        """Unknown work must remain unresolved rather than be retried blindly."""
        self.assertFalse(actions._classify_output(
            "[Browser Receipt] {truncated"))

    def test_every_browser_operation_honours_the_flag(self):
        for operation in ("start", "goto", "click", "type", "fill_form",
                          "submit", "upload", "snapshot", "wait"):
            with self.subTest(operation=operation):
                self.assertFalse(
                    self.classify({"ok": False, "operation": operation}))

    def test_the_ordinary_prefixes_still_work(self):
        self.assertFalse(actions._classify_output("[Error] nope"))
        self.assertFalse(actions._classify_output("[Blocked] refused"))
        self.assertTrue(actions._classify_output("[Success] done"))

    def test_the_receipt_builder_can_actually_say_false(self):
        """Otherwise the flag this reads is never set."""
        import inspect
        source = inspect.getsource(__import__("browser"))
        self.assertIn("ok=bool(filled) and not failed", source)


class TheWholeChain(unittest.TestCase):
    """A failed step must not lock the outcome shut.

    The "already satisfied" branch cannot be released by persistence -- by
    design, because a second reminder is the harm the gate prevents. So a
    false success there is unrecoverable, which is why these two defects
    together ended the turn rather than slowing it down.
    """

    def test_a_failed_fill_does_not_satisfy_the_outcome(self):
        import execution_reliability
        runtime = execution_reliability.OutcomeRuntime(*plan_with(
            ("BROWSER_GOTO", "BROWSER_FILL_FORM")))

        failure = ('[Browser Receipt] {"ok": false, "operation": '
                   '"fill_form", "filled": [], "failed": ["url: no field"]}')
        # Shaped exactly as execute_actions records it, including the ok flag
        # it derives with _classify_output -- which is the half that was wrong.
        runtime.apply_result({"type": "BROWSER_FILL_FORM",
                              "ok": actions._classify_output(failure),
                              "output": failure})

        self.assertEqual(
            runtime.validate_proposal('<ACTION type="BROWSER_GOTO" />'), "",
            "a form that filled nothing closed the outcome, and navigation "
            "stayed locked out for the rest of the turn")


if __name__ == "__main__":
    unittest.main()
