import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import execution_reliability as reliability
import outcomes


def compiled_two():
    return type("Plan", (), {"outcomes": (
        outcomes.CompiledOutcome(
            "o1", outcomes.UPDATE_SCHEDULED_EVENT, "Review",
            {"new_time": "13:00"}, required_result="updated event receipt"),
        outcomes.CompiledOutcome(
            "o2", outcomes.CREATE_REMINDER, "",
            {"time": "16:30"}, required_result="created reminder receipt"),
    )})()


class ExecutionReliabilityTests(unittest.TestCase):
    def runtime(self):
        plan = compiled_two()
        selected = (
            outcomes.ResolvedOutcome(plan.outcomes[0],
                ("UPDATE_CALENDAR_EVENT",), (), "UPDATE_CALENDAR_EVENT"),
            outcomes.ResolvedOutcome(plan.outcomes[1],
                ("SET_ALARM", "ADD_TASK"), ("ADD_TASK",), "SET_ALARM"),
        )
        return reliability.OutcomeRuntime(plan, selected)

    def test_unselected_alternative_is_rejected_before_dispatch(self):
        runtime = self.runtime()
        message = runtime.validate_proposal(
            '<ACTION type="ADD_TASK" title="start project" />')
        self.assertIn("did not select ADD_TASK", message)

    def test_selected_actions_are_accepted(self):
        runtime = self.runtime()
        self.assertEqual(runtime.validate_proposal(
            '<ACTION type="SET_ALARM" time="16:30" />'), "")

    def test_receipts_drive_success_and_completion(self):
        runtime = self.runtime()
        runtime.apply_result({"type": "UPDATE_CALENDAR_EVENT", "ok": True,
                              "output": "[Success] refreshed event is 13:00"})
        runtime.apply_result({"type": "SET_ALARM", "ok": True,
                              "output": "[Success] alarm retrievable at 16:30"})
        self.assertTrue(runtime.ledger.task_successful())
        self.assertTrue(runtime.authoritatively_successful())
        self.assertEqual(runtime.completion_problem(), "")

    def test_partial_selection_never_bypasses_semantic_review(self):
        plan = compiled_two()
        selected = (outcomes.ResolvedOutcome(
            plan.outcomes[0], ("UPDATE_CALENDAR_EVENT",), (),
            "UPDATE_CALENDAR_EVENT"),)
        runtime = reliability.OutcomeRuntime(plan, selected)
        runtime.apply_result({"type": "UPDATE_CALENDAR_EVENT", "ok": True,
                              "output": "[Success] refreshed event is 13:00"})
        self.assertFalse(runtime.authoritatively_successful())

    def test_generic_outcome_never_bypasses_semantic_review(self):
        generic = outcomes.CompiledOutcome(
            "g1", outcomes.GENERIC, "do something unusual", {},
            required_result="the unusual thing exists")
        plan = type("Plan", (), {"outcomes": (generic,)})()
        selected = (outcomes.ResolvedOutcome(
            generic, ("RUN_COMMAND",), (), "RUN_COMMAND"),)
        runtime = reliability.OutcomeRuntime(plan, selected)
        self.assertFalse(runtime.authoritatively_successful())

    def test_done_is_rejected_while_outcome_is_pending(self):
        runtime = self.runtime()
        self.assertIn("not complete", runtime.completion_problem())

    def test_approval_pauses_without_claiming_success(self):
        runtime = self.runtime()
        runtime.apply_result({"type": "UPDATE_CALENDAR_EVENT", "ok": False,
                              "output": "[Approval Required] waiting"})
        runtime.apply_result({"type": "SET_ALARM", "ok": False,
                              "output": "[Approval Required] waiting"})
        self.assertEqual(runtime.completion_problem(), "")
        self.assertFalse(runtime.ledger.task_successful())

    def test_fingerprint_is_stable_and_payload_sensitive(self):
        one = reliability.fingerprint("SEND_EMAIL", {"to": "a", "body": "x"},
                                      "turn", "o1")
        same = reliability.fingerprint("send_email", {"body": "x", "to": "a"},
                                       "turn", "o1")
        changed = reliability.fingerprint("SEND_EMAIL", {"to": "b", "body": "x"},
                                          "turn", "o1")
        self.assertEqual(one, same)
        self.assertNotEqual(one, changed)


if __name__ == "__main__":
    unittest.main()
