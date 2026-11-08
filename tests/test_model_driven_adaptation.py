import datetime as dt
import os
import tempfile
import unittest
from unittest import mock

import camera_library
import capabilities
import learned_procedures
import outcome_compiler
import outcomes
import runtime_power


class ModelVisibleLifecycle(unittest.TestCase):
    def tearDown(self):
        runtime_power.resume()

    def test_standby_is_declared_not_intercepted_at_ingress(self):
        cap = capabilities.get("SET_STANDBY")
        self.assertIsNotNone(cap)
        self.assertIn("minutes", cap.schema.get("optional", ()))
        source = open("agent.py", encoding="utf-8").read()
        self.assertNotIn("def _handle_power_command", source)

    def test_standby_request_compiles_to_registered_outcome(self):
        compiled = outcome_compiler.compile("activate standby mode for 5 minutes")
        self.assertEqual(compiled.outcomes[0].operation, outcomes.ENTER_STANDBY)

    def test_standby_without_duration_is_indefinite(self):
        compiled = outcome_compiler.compile("activate standby")
        self.assertEqual(compiled.outcomes[0].operation, outcomes.ENTER_STANDBY)
        self.assertIn("Omit minutes for indefinite standby",
                      capabilities.get("SET_STANDBY").notes)

    def test_runtime_power_is_state_not_a_phrase_parser(self):
        self.assertFalse(hasattr(runtime_power, "parse_command"))
        runtime_power.enter(2)
        self.assertTrue(runtime_power.is_standby())
        self.assertGreater(runtime_power.remaining_seconds(), 0)


class ModelVisibleIdentity(unittest.TestCase):
    def test_named_visible_person_compiles_to_enrollment(self):
        compiled = outcome_compiler.compile(
            "you can see a person on camera, save him as Himanshu")
        self.assertEqual(compiled.outcomes[0].operation, outcomes.ENROLL_FACE)
        self.assertEqual(capabilities.get("REMEMBER_FACE").name, "REMEMBER_FACE")

    def test_direct_face_memory_request_is_not_downgraded_to_a_note(self):
        compiled = outcome_compiler.compile("remember this face")
        self.assertEqual(compiled.outcomes[0].operation, outcomes.ENROLL_FACE)

    def test_explicit_face_name_reaches_the_selected_action(self):
        import outcome_planner
        compiled = outcome_compiler.compile(
            "remember this face as Himanshu")
        self.assertEqual(compiled.outcomes[0].target, "Himanshu")
        bound = outcome_planner.bind(
            compiled, lambda _query, _limit: ["REMEMBER_FACE"])
        selected = outcome_planner.select(bound)
        self.assertEqual(selected[0].selected_action, "REMEMBER_FACE")
        self.assertIn("[Himanshu]",
                      outcome_planner.render_selection(selected))

    def test_unnamed_face_requests_one_identity_instead_of_only_observing(self):
        import outcome_planner
        compiled = outcome_compiler.compile("remember this face")
        bound = outcome_planner.bind(
            compiled, lambda _query, _limit: ["REMEMBER_FACE"])
        selected = outcome_planner.select(bound)
        self.assertEqual(
            outcome_planner.clarification_for(compiled, bound, selected),
            "Whose face should I remember?")


class GraphProcedures(unittest.TestCase):
    @mock.patch("memory.neo4j_store.store.query")
    def test_explicit_procedure_is_written_to_neo4j(self, query):
        result = learned_procedures.learn(
            "when webcam visibility is poor",
            "turn on screen light, recapture, verify, then restore",
            "SCREEN_LIGHT,CAPTURE_CAMERA,SCREEN_LIGHT_OFF")
        self.assertTrue(result.startswith("[Success]"))
        cypher = query.call_args.args[0]
        self.assertIn("LearnedProcedure", cypher)
        self.assertIn("HAS_PROCEDURE", cypher)

    @mock.patch("learned_procedures.list_active")
    def test_only_relevant_graph_procedure_enters_prompt(self, active):
        active.return_value = [
            {"id": "p1", "trigger": "webcam image is dark",
             "instruction": "use the screen light and recapture",
             "action_sequence": ["SCREEN_LIGHT", "CAPTURE_CAMERA"]},
            {"id": "p2", "trigger": "writing an email",
             "instruction": "keep it brief", "action_sequence": []},
        ]
        block = learned_procedures.prompt_block("capture me through the webcam")
        self.assertIn("screen light", block)
        self.assertNotIn("writing an email", block)

    @mock.patch("memory.neo4j_store.store.query")
    def test_empty_procedure_graph_uses_dynamic_relationship_lookup(self, query):
        query.return_value = []
        self.assertEqual(learned_procedures.list_active(), [])
        cypher = query.call_args.args[0]
        self.assertIn("type(r)=$relationship", cypher)
        self.assertNotIn("[:HAS_PROCEDURE]", cypher)

    def test_future_instruction_compiles_to_learning(self):
        compiled = outcome_compiler.compile(
            "in the future use the screen light whenever the camera is dark")
        self.assertEqual(compiled.outcomes[0].operation, outcomes.LEARN_BEHAVIOUR)

    @mock.patch("learned_procedures.list_active")
    def test_taught_teaching_rule_is_retrieved_for_later_lesson(self, active):
        active.return_value = [{
            "id": "quiz-after-teaching",
            "trigger": "whenever teaching Himanshu a topic",
            "instruction": "test him with active recall before finishing",
            "action_sequence": [],
        }]
        block = learned_procedures.prompt_block("teach me transformer attention")
        self.assertIn("active recall", block)

    @mock.patch("memory.neo4j_store.store.query")
    @mock.patch("learned_procedures.list_active")
    def test_user_can_retire_a_taught_rule_without_deleting_history(
            self, active, query):
        active.return_value = [{
            "id": "quiz-after-teaching",
            "trigger": "whenever teaching a topic",
            "instruction": "test me before finishing",
        }]
        result = learned_procedures.retire("stop testing me before finishing")
        self.assertIn("Retired procedure", result)
        cypher = query.call_args.args[0]
        self.assertIn("active=false", cypher)
        self.assertNotIn("DELETE", cypher.upper())


class CameraArchive(unittest.TestCase):
    def test_capture_paths_are_unique_and_dated(self):
        with tempfile.TemporaryDirectory() as base:
            first = camera_library.new_path(base)
            second = camera_library.new_path(base)
            self.assertNotEqual(first, second)
            self.assertTrue(first.startswith(camera_library.root(base)))

    @mock.patch("memory.neo4j_store.store.query")
    def test_files_older_than_thirty_days_are_removed(self, _query):
        with tempfile.TemporaryDirectory() as base:
            path = camera_library.new_path(base)
            with open(path, "wb") as handle:
                handle.write(b"old image")
            old = (dt.datetime.now().timestamp()
                   - (camera_library.RETENTION_DAYS + 1) * 86400)
            os.utime(path, (old, old))
            removed = camera_library.purge_expired(base)
            self.assertIn(path, removed)
            self.assertFalse(os.path.exists(path))

    def test_model_can_request_a_retained_capture(self):
        cap = capabilities.get("VIEW_CAMERA_CAPTURE")
        self.assertEqual(cap.runs_in, capabilities.RUNS_IN_AGENT_LOOP)
        source = open("agent.py", encoding="utf-8").read()
        self.assertIn('"VIEW_CAMERA_CAPTURE"', source)
        self.assertIn("camera_library.resolve", source)


if __name__ == "__main__":
    unittest.main()
