# -*- coding: utf-8 -*-
"""
The reliability guarantees, at the points where they are actually enforced.

Three behaviours that were promised in a prompt and therefore did not hold:

  - the plan names one action per outcome, and the model ran two of them, so
    one "remind me at 4:30" became an alarm and a task;
  - an action already dispatched was re-issued by a repeated model step, so
    Chrome and the music player each opened twice;
  - `<DONE />` arrived while outcomes were still pending, and the turn ended
    with a confident summary of work that had not happened.

A prompt is a request. These tests cover the enforcement instead: a refusal
before dispatch, a fingerprint comparison, and a completion gate that reads
the ledger rather than the narration.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import execution_reliability as reliability
import outcome_compiler
import outcome_planner
import outcomes


def runtime_for(utterance, retrieve=None):
    """A runtime built the way the agent builds one, from a raw utterance."""
    compiled = outcome_compiler.compile(utterance)
    bound = outcome_planner.bind(compiled, retrieve=retrieve or _fake_retrieve)
    chosen = outcome_planner.select(bound, utterance, [])
    return compiled, chosen, reliability.OutcomeRuntime(compiled, chosen)


def _fake_retrieve(query, limit=8):
    """No registry, no embeddings -- these tests are about enforcement."""
    return []


class SelectionIsEnforced(unittest.TestCase):
    def test_unselected_reminder_channel_is_refused(self):
        _c, _s, runtime = runtime_for("remind me at 4:30 to call madhu")
        message = runtime.validate_proposal(
            '<ACTION type="ADD_TASK" title="call madhu" />')
        self.assertIn("did not select ADD_TASK", message)
        self.assertIn("same thing twice", message)

    def test_selected_channel_is_allowed(self):
        _c, _s, runtime = runtime_for("remind me at 4:30 to call madhu")
        self.assertEqual(runtime.validate_proposal(
            '<ACTION type="SET_ALARM" time="16:30" />'), "")

    def test_helper_actions_are_not_blocked(self):
        """A new false denial would be no better than the old one."""
        _c, _s, runtime = runtime_for("remind me at 4:30 to call madhu")
        for helper in ("DISCOVER_ACTIONS", "LOOKUP_CONTACTS",
                       "TAKE_SCREENSHOT", "READ_FILE"):
            with self.subTest(action=helper):
                self.assertEqual(
                    runtime.validate_proposal(f'<ACTION type="{helper}" />'),
                    "")

    def test_rejection_is_recorded_without_completing_the_outcome(self):
        _c, _s, runtime = runtime_for("remind me at 4:30 to call madhu")
        runtime.validate_proposal('<ACTION type="ADD_TASK" />')
        record = runtime.ledger.get("o1")
        self.assertEqual(record.state, outcomes.PENDING)
        self.assertTrue(any(note.get("kind") == "alternative"
                            for note in record.notes))

    def test_case_and_quoting_do_not_evade_the_check(self):
        _c, _s, runtime = runtime_for("remind me at 4:30 to call madhu")
        self.assertIn("did not select",
                      runtime.validate_proposal("<action type='add_task' />"))


class DuplicateSuppression(unittest.TestCase):
    def test_identical_action_is_recognised_as_already_dispatched(self):
        _c, _s, runtime = runtime_for("remind me at 4:30 to call madhu")
        attrs = {"type": "SET_ALARM", "time": "16:30", "label": "call madhu"}
        self.assertFalse(runtime.already_dispatched("SET_ALARM", attrs, "t1"))
        runtime.note_dispatch("SET_ALARM", attrs, "t1")
        self.assertTrue(runtime.already_dispatched("SET_ALARM", attrs, "t1"))

    def test_different_payload_is_not_a_duplicate(self):
        _c, _s, runtime = runtime_for("remind me at 4:30 to call madhu")
        runtime.note_dispatch("SET_ALARM", {"time": "16:30"}, "t1")
        self.assertFalse(
            runtime.already_dispatched("SET_ALARM", {"time": "17:30"}, "t1"))

    def test_only_side_effecting_actions_are_deduplicated(self):
        """Re-reading a file or searching again must stay possible."""
        for harmless in ("READ_FILE", "TAKE_SCREENSHOT", "SEARCH_WEB",
                         "LIST_TASKS", "DISCOVER_ACTIONS"):
            with self.subTest(action=harmless):
                self.assertNotIn(harmless, reliability.DEDUPLICATED_ACTIONS)

    def test_the_actions_that_doubled_in_real_use_are_covered(self):
        for risky in ("LAUNCH_APP", "BROWSER_START", "MUSIC_PLAY",
                      "SPOTIFY_PLAY", "SEND_WHATSAPP", "SEND_EMAIL",
                      "ADD_CALENDAR_EVENT", "SET_ALARM", "SET_STANDBY",
                      "RESUME_STANDBY"):
            with self.subTest(action=risky):
                self.assertIn(risky, reliability.DEDUPLICATED_ACTIONS)

    def test_deliberate_repeat_has_an_escape_hatch(self):
        self.assertEqual(reliability.REPEAT_ATTR, "repeat")

    def test_bookkeeping_flags_do_not_change_identity(self):
        """An approved retry must match the approval it was granted under."""
        plain = reliability.fingerprint("SEND_EMAIL", {"to": "a"}, "t", "o1")
        approved = reliability.fingerprint(
            "SEND_EMAIL",
            {"to": "a", "confirmed": "true", "approved": "true",
             "approval_id": "xyz"}, "t", "o1")
        self.assertEqual(plain, approved)

    def test_turn_scopes_the_fingerprint(self):
        """Yesterday's send must not suppress today's."""
        self.assertNotEqual(
            reliability.fingerprint("SEND_EMAIL", {"to": "a"}, "t1", "o1"),
            reliability.fingerprint("SEND_EMAIL", {"to": "a"}, "t2", "o1"))


class ReceiptsDecideCompletion(unittest.TestCase):
    def test_done_refused_while_pending(self):
        _c, _s, runtime = runtime_for("remind me at 4:30 to call madhu")
        problem = runtime.completion_problem()
        self.assertIn("not complete", problem)
        # The message has to say what is still required, or the model has
        # nothing to act on.
        self.assertIn("reminder", problem.lower())

    def test_done_allowed_once_the_receipt_exists(self):
        _c, _s, runtime = runtime_for("remind me at 4:30 to call madhu")
        runtime.apply_result({"type": "SET_ALARM", "ok": True,
                              "output": "[Success] alarm set for 16:30"})
        self.assertEqual(runtime.completion_problem(), "")
        self.assertTrue(runtime.task_successful())

    def test_failure_is_terminal_but_not_success(self):
        _c, _s, runtime = runtime_for("remind me at 4:30 to call madhu")
        runtime.apply_result({"type": "SET_ALARM", "ok": False,
                              "output": "[Error] provider refused"})
        self.assertEqual(runtime.completion_problem(), "")
        self.assertTrue(runtime.turn_terminal())
        self.assertFalse(runtime.task_successful())
        self.assertIn("did not succeed", runtime.honest_summary())

    def test_helper_receipt_is_not_progress(self):
        """Discovery once consumed the whole budget and looked like work."""
        _c, _s, runtime = runtime_for("remind me at 4:30 to call madhu")
        runtime.apply_result({"type": "DISCOVER_ACTIONS", "ok": True,
                              "output": "found 12 actions"})
        self.assertFalse(runtime.task_successful())
        self.assertIn("not complete", runtime.completion_problem())

    def test_approval_pause_is_not_failure_and_not_success(self):
        _c, _s, runtime = runtime_for("remind me at 4:30 to call madhu")
        runtime.apply_result({"type": "SET_ALARM", "ok": False,
                              "output": "[Approval Required] waiting for you"})
        self.assertEqual(runtime.ledger.get("o1").state,
                         outcomes.AWAITING_APPROVAL)
        self.assertFalse(runtime.task_successful())
        self.assertEqual(runtime.completion_problem(), "")

    def test_success_cannot_be_recorded_without_a_receipt(self):
        _c, _s, runtime = runtime_for("remind me at 4:30 to call madhu")
        runtime.apply_result({"type": "SET_ALARM", "ok": True, "output": ""})
        # An empty response cannot complete a user outcome, even if a provider
        # incorrectly attached ok=True to it.
        record = runtime.ledger.get('o1')
        self.assertNotEqual(record.state, outcomes.SUCCEEDED)
        self.assertFalse(record.receipts)
        self.assertIn('No execution receipt', record.failure)

    def test_begin_marks_executing(self):
        _c, _s, runtime = runtime_for("remind me at 4:30 to call madhu")
        runtime.begin("SET_ALARM")
        self.assertEqual(runtime.ledger.get("o1").state, outcomes.EXECUTING)

    def test_begin_on_a_helper_does_not_touch_an_outcome(self):
        _c, _s, runtime = runtime_for("remind me at 4:30 to call madhu")
        runtime.begin("DISCOVER_ACTIONS")
        self.assertEqual(runtime.ledger.get("o1").state, outcomes.PENDING)

    def test_succeeded_outcome_is_not_downgraded_by_a_later_receipt(self):
        _c, _s, runtime = runtime_for("remind me at 4:30 to call madhu")
        runtime.apply_result({"type": "SET_ALARM", "ok": True,
                              "output": "[Success] set"})
        runtime.apply_result({"type": "SET_ALARM", "ok": False,
                              "output": "[Error] second attempt failed"})
        self.assertTrue(runtime.task_successful())


class AlternativeRetry(unittest.TestCase):
    def test_policy_refusal_permits_an_alternative(self):
        _c, _s, runtime = runtime_for("remind me at 4:30 to call madhu")
        runtime.apply_result({
            "type": "SET_ALARM", "ok": False,
            "output": "[Mode Policy] SET_ALARM is disabled while SLEEP mode is active."})
        self.assertEqual(runtime.ledger.get("o1").side_effect,
                         outcomes.SIDE_EFFECT_NONE)
        self.assertTrue(runtime.alternative_for("o1"))

    def test_timeout_forbids_an_alternative(self):
        """A timed-out side effect may have happened. Stop."""
        _c, _s, runtime = runtime_for("remind me at 4:30 to call madhu")
        runtime.apply_result({"type": "SET_ALARM", "ok": False,
                              "output": "[Error] request timed out"})
        self.assertEqual(runtime.ledger.get("o1").side_effect,
                         outcomes.SIDE_EFFECT_UNKNOWN)
        self.assertEqual(runtime.alternative_for("o1"), "")

    def test_a_succeeded_outcome_has_no_alternative(self):
        _c, _s, runtime = runtime_for("remind me at 4:30 to call madhu")
        runtime.apply_result({"type": "SET_ALARM", "ok": True,
                              "output": "[Success] set"})
        self.assertEqual(runtime.alternative_for("o1"), "")


class ApplyDispatcherOutcome(unittest.TestCase):
    def test_receipt_list_is_applied(self):
        _c, _s, runtime = runtime_for("remind me at 4:30 to call madhu")

        class Outcome:
            data = [{"type": "SET_ALARM", "ok": True,
                     "output": "[Success] alarm at 16:30"}]

        runtime.apply_outcome(Outcome())
        self.assertTrue(runtime.task_successful())

    def test_textual_outcome_is_applied(self):
        _c, _s, runtime = runtime_for("remind me at 4:30 to call madhu")

        class Outcome:
            data = None
            action_type = "SET_ALARM"

            def model_text(self):
                return "[Error] could not reach the alarm store"

        runtime.apply_outcome(Outcome())
        self.assertFalse(runtime.task_successful())
        self.assertEqual(runtime.ledger.get("o1").state, outcomes.FAILED)


class NoProtectedContentReachesRecovery(unittest.TestCase):
    """The capability-recovery sweep searches wider, so it must stay clean."""

    def test_recovery_queries_exclude_content(self):
        compiled = outcome_compiler.compile(
            "remind me at 4:30 to start my new project")
        joined = " ".join(compiled.retrieval_queries()).lower()
        self.assertNotIn("start my new project", joined)


if __name__ == "__main__":
    unittest.main()


class TurnLocalsSurviveAFailedRegistry(unittest.TestCase):
    """The variables the turn reads later must be bound before the try.

    Registry configuration raised because a helper function was missing. The
    except branch logged it and carried on with the legacy catalogues, which
    is the right degradation -- but `outcome_runtime`, `compiled_plan` and
    `outcome_guidance` were initialised *inside* the try, so they were never
    assigned, and the turn died several hundred lines later with
    UnboundLocalError. A fallback that only works when nothing failed is not
    a fallback.
    """

    def _agent_source(self):
        path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "agent.py")
        with open(path, encoding="utf-8", errors="replace") as handle:
            return handle.read()

    def test_turn_locals_are_bound_before_the_registry_try(self):
        source = self._agent_source()
        anchor = source.index("capability_migration.configure_registry")
        window = source[max(0, anchor - 1600):anchor]
        for name in ("compiled_plan", "outcome_guidance", "outcome_runtime"):
            with self.subTest(variable=name):
                self.assertIn(
                    f"{name} = ", window,
                    f"{name} is not initialised before the registry try "
                    f"block, so a configuration failure leaves it unbound")

    def test_each_turn_local_is_initialised_once(self):
        """A second initialisation inside the try would mask the first."""
        source = self._agent_source()
        anchor = source.index("capability_migration.configure_registry")
        region = source[max(0, anchor - 1600):anchor + 4000]
        for name in ("compiled_plan", "outcome_runtime"):
            with self.subTest(variable=name):
                self.assertEqual(region.count(f"\n        {name} = "), 1)
