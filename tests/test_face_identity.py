import os
import tempfile
import unittest
from unittest import mock

import numpy as np

import camera_perception
import face_identity


def _face(vector, score=0.98):
    value = np.asarray(vector, dtype=np.float32)
    value = value / np.linalg.norm(value)
    return face_identity.DetectedFace(
        bbox=(10, 10, 150, 150),
        normalized_box=(0.01, 0.01, 0.15, 0.15),
        detection_score=score, quality=0.9, embedding=value)


class EncryptionTests(unittest.TestCase):
    def test_embedding_round_trip_is_encrypted_and_dimension_checked(self):
        vector = np.linspace(-1, 1, 512, dtype=np.float32)
        vector /= np.linalg.norm(vector)
        with mock.patch.object(face_identity, "_key", return_value=b"k" * 32):
            payload = face_identity._encrypt_embedding(vector)
            restored = face_identity._decrypt_embedding(payload, 512)
        self.assertTrue(payload.startswith("v1:"))
        self.assertNotIn(base64_prefix(vector), payload)
        np.testing.assert_allclose(restored, vector, atol=1e-6)

    def test_wrong_dimension_is_rejected(self):
        vector = np.ones(512, dtype=np.float32)
        with mock.patch.object(face_identity, "_key", return_value=b"k" * 32):
            payload = face_identity._encrypt_embedding(vector)
            with self.assertRaisesRegex(ValueError, "dimension"):
                face_identity._decrypt_embedding(payload, 256)


def base64_prefix(vector):
    import base64
    return base64.urlsafe_b64encode(
        np.asarray(vector, dtype="<f4").tobytes()).decode()[:32]


class MatchingTests(unittest.TestCase):
    def setUp(self):
        self.a = np.zeros(512, dtype=np.float32)
        self.a[0] = 1.0
        self.b = np.zeros(512, dtype=np.float32)
        self.b[1] = 1.0

    def test_clear_match_is_named(self):
        with mock.patch.object(face_identity, "detect",
                               return_value=(_face(self.a),)), \
             mock.patch.object(face_identity, "_templates", return_value=(
                 ("Madhu", "face_m", self.a),
                 ("Someone Else", "face_s", self.b))):
            result = face_identity.identify("ignored.jpg")
        self.assertEqual(result[0].status, "recognized")
        self.assertEqual(result[0].person, "Madhu")

    def test_low_score_stays_unknown(self):
        query = np.zeros(512, dtype=np.float32)
        query[2] = 1.0
        with mock.patch.object(face_identity, "detect",
                               return_value=(_face(query),)), \
             mock.patch.object(face_identity, "_templates", return_value=(
                 ("Madhu", "face_m", self.a),)):
            result = face_identity.identify("ignored.jpg")
        self.assertEqual(result[0].status, "unknown")
        self.assertEqual(result[0].person, "")

    def test_close_competing_people_are_ambiguous(self):
        first = self.a.copy()
        second = self.a.copy()
        second[0] = 0.99
        second[1] = 0.1
        second /= np.linalg.norm(second)
        with mock.patch.object(face_identity, "detect",
                               return_value=(_face(self.a),)), \
             mock.patch.object(face_identity, "_templates", return_value=(
                 ("Madhu", "face_m", first),
                 ("Anu", "face_a", second))):
            result = face_identity.identify("ignored.jpg")
        self.assertEqual(result[0].status, "ambiguous")
        self.assertEqual(result[0].person, "")


class EnrollmentTests(unittest.TestCase):
    def test_enrollment_writes_ciphertext_and_graph_link_not_raw_vector(self):
        vector = np.zeros(512, dtype=np.float32)
        vector[0] = 1.0
        stored = {}
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as image:
            image.write(b"synthetic enrollment frame")
            path = image.name
        try:
            from memory.neo4j_store import store
            with mock.patch.object(face_identity, "detect",
                                   return_value=(_face(vector),)), \
                 mock.patch.object(face_identity, "_templates", return_value=()), \
                 mock.patch.object(face_identity, "_phone_status", return_value=""), \
                 mock.patch.object(face_identity, "resolve_person_node",
                                   return_value=("Madhu", "", "")), \
                 mock.patch.object(face_identity, "_key", return_value=b"e" * 32), \
                 mock.patch.object(store, "known_face_people", return_value=[]), \
                 mock.patch.object(store, "upsert_face_template",
                                   side_effect=lambda person, row:
                                   stored.update(person=person, row=row)):
                result = face_identity.enroll_person("Madhu", path)
        finally:
            os.unlink(path)
        self.assertTrue(result.startswith("[Success]"))
        self.assertEqual(stored["person"], "Madhu")
        self.assertTrue(stored["row"]["ciphertext"].startswith("v1:"))
        self.assertNotIn("embedding", stored["row"])
        self.assertEqual(stored["row"]["model"], face_identity.MODEL_NAME)

    def test_existing_face_cannot_be_linked_to_a_second_name(self):
        vector = np.zeros(512, dtype=np.float32)
        vector[0] = 1.0
        with tempfile.NamedTemporaryFile(suffix=".jpg") as image, \
             mock.patch.object(face_identity, "detect",
                               return_value=(_face(vector),)), \
             mock.patch.object(face_identity, "_templates", return_value=(
                 ("Madhu", "face_m", vector),)):
            result = face_identity.enroll_person("Another Name", image.name)
        self.assertTrue(result.startswith("[Error]"))
        self.assertIn("Madhu", result)


def _unit(index, weight=1.0, other=None, other_weight=0.0):
    vector = np.zeros(512, dtype=np.float32)
    vector[index] = weight
    if other is not None:
        vector[other] = other_weight
    return vector / np.linalg.norm(vector)


class LinkingToExistingNodes(unittest.TestCase):
    """A face hangs on the person's node that is already in the graph."""

    def resolve(self, rows, name="Madhu"):
        from memory.neo4j_store import store
        with mock.patch.object(store, "find_person_nodes", return_value=rows):
            return face_identity.resolve_person_node(name)

    def test_an_exact_node_is_used_as_it_is(self):
        node, note, problem = self.resolve([{"name": "madhu", "exact": True, "contact_facts": 2}])
        self.assertEqual((node, note, problem), ("madhu", "", ""))

    def test_a_single_contact_that_contains_the_name_is_linked_and_said_so(self):
        node, note, problem = self.resolve(
            [{"name": "madhu mother", "exact": False, "contact_facts": 1}])
        self.assertEqual(node, "madhu mother")
        self.assertIn("existing contact", note)
        self.assertEqual(problem, "")

    def test_several_candidates_are_not_guessed_between(self):
        node, _, problem = self.resolve([
            {"name": "madhu mother", "exact": False, "contact_facts": 1},
            {"name": "madhu kumar", "exact": False, "contact_facts": 1}])
        self.assertIsNone(node)
        self.assertIn("madhu mother", problem)
        self.assertIn("madhu kumar", problem)
        self.assertIn("exact name", problem)

    def test_nobody_found_means_a_new_person_node(self):
        self.assertEqual(self.resolve([]), ("Madhu", "", ""))

    def test_a_graph_failure_still_lets_the_face_be_saved(self):
        from memory.neo4j_store import store
        with mock.patch.object(store, "find_person_nodes", side_effect=RuntimeError("down")):
            self.assertEqual(face_identity.resolve_person_node("Madhu"), ("Madhu", "", ""))

    def _enrol(self, name, node_result):
        from memory.neo4j_store import store
        saved = {}
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as image:
            image.write(b"frame")
        try:
            with mock.patch.object(face_identity, "detect", return_value=(_face(_unit(5)),)), \
                 mock.patch.object(face_identity, "_templates", return_value=()), \
                 mock.patch.object(face_identity, "resolve_person_node", return_value=node_result), \
                 mock.patch.object(face_identity, "_phone_status", return_value="Phone linked."), \
                 mock.patch.object(face_identity, "_key", return_value=b"e" * 32), \
                 mock.patch.object(face_identity, "_upsert_boot_template"), \
                 mock.patch.object(store, "known_face_people", return_value=[]), \
                 mock.patch.object(store, "upsert_face_template",
                                   side_effect=lambda person, row: saved.update(person=person)):
                result = face_identity.enroll_person(name, image.name)
        finally:
            os.unlink(image.name)
        return result, saved

    def test_the_template_is_stored_on_the_resolved_node(self):
        result, saved = self._enrol("Madhu", ("madhu mother", " Linked to the existing contact.", ""))
        self.assertEqual(saved["person"], "madhu mother")
        self.assertTrue(result.startswith("[Success]"))
        self.assertIn("Linked to the existing contact", result)
        self.assertIn("Phone linked", result)

    def test_an_ambiguous_name_saves_nothing(self):
        result, saved = self._enrol("Madhu", (None, "", "[Error] More than one contact could be Madhu."))
        self.assertTrue(result.startswith("[Error]"))
        self.assertEqual(saved, {})

    def test_the_owner_is_enrolled_like_anyone_else_with_no_password(self):
        result, saved = self._enrol("Himanshu", ("Himanshu", "", ""))
        self.assertTrue(result.startswith("[Success]"))
        self.assertEqual(saved["person"], "Himanshu")
        import inspect
        self.assertNotIn("password", inspect.getsource(face_identity.enroll_person).lower())

    def test_someone_beside_the_person_is_never_saved(self):
        with mock.patch.object(face_identity, "detect",
                               return_value=(_face(_unit(1)), _face(_unit(2)))):
            result = face_identity.enroll_person("Himanshu", "frame.jpg")
        self.assertIn("exactly one", result)

    def test_a_face_that_already_belongs_to_someone_else_is_not_relinked(self):
        with mock.patch.object(face_identity, "detect", return_value=(_face(_unit(5)),)), \
             mock.patch.object(face_identity, "_templates",
                               return_value=(("Anu", "t", _unit(5)),)):
            result = face_identity.enroll_person("Madhu", "frame.jpg")
        self.assertTrue(result.startswith("[Error]"))
        self.assertIn("Anu", result)

    def test_a_phone_number_is_linked_without_being_echoed(self):
        from memory.neo4j_store import store
        with mock.patch.object(store, "add_fact") as add, \
             mock.patch.object(store, "facts_for", return_value=[{"object": "x"}]):
            note = face_identity._phone_status("Madhu", "+91 98765 43210")
            self.assertEqual(add.call_args.args[:3], ("Madhu", "HAS_PHONE", "+919876543210"))
        self.assertNotIn("98765", note)
        with mock.patch.object(store, "add_fact") as add:
            note = face_identity._phone_status("Madhu", "12")
        add.assert_not_called()
        self.assertIn("did not look like", note)

    def test_with_no_number_it_says_whether_one_is_already_on_file(self):
        from memory.neo4j_store import store
        with mock.patch.object(store, "facts_for", return_value=[]):
            self.assertIn("No phone number", face_identity._phone_status("Madhu"))
        with mock.patch.object(store, "facts_for", return_value=[{"object": "x"}]):
            self.assertIn("linked", face_identity._phone_status("Madhu"))


class WhoIsThis(unittest.TestCase):
    def match(self, status="recognized", person="Madhu", similarity=0.71):
        return face_identity.FaceMatch(
            status=status, bbox=(0, 0, 10, 10), normalized_box=(0, 0, .1, .1),
            detection_score=.9, person=person if status == "recognized" else "",
            similarity=similarity)

    def ask(self, matches, details=None):
        details = details or {"phones": [], "emails": [], "facts": []}
        with mock.patch.object(face_identity, "identify", return_value=tuple(matches)), \
             mock.patch.object(face_identity, "_details_for", return_value=details):
            return face_identity.who_is_this("frame.jpg")

    def test_a_known_face_is_named_with_the_linked_phone_number(self):
        result = self.ask([self.match()], {"phones": ["+919876543210"], "emails": ["m@x.com"],
                                           "facts": ["studies at amrita"]})
        self.assertTrue(result.startswith("[Success]"))
        for part in ("Madhu", "0.71", "+919876543210", "m@x.com", "studies at amrita"):
            self.assertIn(part, result)

    def test_a_known_face_with_no_number_says_so(self):
        self.assertIn("no phone number on file", self.ask([self.match()]))

    def test_an_unknown_face_is_not_guessed_and_the_way_to_save_it_is_offered(self):
        result = self.ask([self.match("unknown", similarity=0.21)])
        self.assertIn("don't know", result)
        self.assertNotIn("Madhu", result)
        self.assertIn("remember", result)

    def test_two_close_people_are_not_named(self):
        result = self.ask([self.match("ambiguous", similarity=0.55)])
        self.assertIn("not naming", result)
        self.assertNotIn("Madhu", result)

    def test_several_faces_are_each_reported(self):
        result = self.ask([self.match(), self.match("unknown", similarity=0.1)])
        self.assertIn("Face 1: Madhu", result)
        self.assertIn("Face 2:", result)

    def test_no_face_is_an_error_not_a_success(self):
        result = self.ask([])
        self.assertTrue(result.startswith("[Error]"))
        self.assertIn("could not see a clear face", result)

    def test_details_come_from_the_persons_own_node(self):
        from memory.neo4j_store import store
        rows = [
            {"subject": "Madhu", "predicate": "HAS_PHONE", "object": "+911111111111"},
            {"subject": "Madhu", "predicate": "STUDIES_AT", "object": "Amrita"},
            {"subject": "Someone Else", "predicate": "HAS_PHONE", "object": "+922222222222"},
            {"subject": "Madhu", "predicate": "HAS_BANK_ACCOUNT", "object": "1234"},
        ]
        with mock.patch.object(store, "facts_for", return_value=rows):
            details = face_identity._details_for("Madhu")
        self.assertEqual(details["phones"], ["+911111111111"])
        self.assertEqual(details["facts"], ["studies at Amrita"])

    def test_it_is_a_declared_capability_wired_to_an_action(self):
        import capabilities
        import actions
        spec = capabilities.get("WHO_IS_THIS")
        self.assertEqual(spec.origins, ("user",))
        self.assertIn("who's this", spec.aliases)
        self.assertIn("WHO_IS_THIS", actions.VISIBLE_RESULT_ACTIONS)
        import inspect
        self.assertIn("who_is_this", inspect.getsource(actions._run_who_is_this))


class ModelChoice(unittest.TestCase):
    def test_the_environment_can_pick_either_model(self):
        with mock.patch.dict(os.environ, {"VANTA_FACE_MODEL": "buffalo_l"}):
            self.assertEqual(face_identity._choose_model(), "buffalo_l")
        with mock.patch.dict(os.environ, {"VANTA_FACE_MODEL": "antelopev2"}):
            self.assertEqual(face_identity._choose_model(), "antelopev2")

    def test_the_better_model_is_default_only_when_installed(self):
        with mock.patch.dict(os.environ, {"VANTA_FACE_MODEL": ""}):
            with mock.patch.object(face_identity, "_model_installed", return_value=True):
                self.assertEqual(face_identity._choose_model(), "antelopev2")
            with mock.patch.object(face_identity, "_model_installed", return_value=False):
                self.assertEqual(face_identity._choose_model(), "buffalo_l")

    def test_a_notice_appears_when_the_owner_has_only_an_older_template(self):
        row = {"person": "Himanshu", "model": "some_older_model"}
        with mock.patch.object(face_identity, "_primary_rows", return_value=[row]):
            self.assertIn("remember my face", face_identity.migration_notice())
        row["model"] = face_identity.MODEL_NAME
        with mock.patch.object(face_identity, "_primary_rows", return_value=[row]):
            self.assertEqual(face_identity.migration_notice(), "")


class ContextSafetyTests(unittest.TestCase):
    def test_graph_context_excludes_pii_sensitive_and_pending_facts(self):
        from memory.neo4j_store import store
        rows = [
            {"subject": "Himanshu", "predicate": "HAS_PARTNER",
             "object": "Madhu", "sensitivity": "private",
             "review_status": "accepted"},
            {"subject": "Madhu", "predicate": "PRIMARY_EMAIL",
             "object": "secret@example.com", "sensitivity": "private",
             "review_status": "accepted"},
            {"subject": "Madhu", "predicate": "HEALTH_CONDITION",
             "object": "secret", "sensitivity": "sensitive",
             "review_status": "accepted"},
            {"subject": "Madhu", "predicate": "WORKS_AT",
             "object": "Maybe Corp", "sensitivity": "public",
             "review_status": "pending"},
        ]
        with mock.patch.object(store, "facts_for", return_value=rows):
            selected = face_identity._person_knowledge("Madhu")
        self.assertEqual(selected, (("Himanshu", "HAS_PARTNER", "Madhu"),))

    def test_unknown_face_never_receives_a_guessed_name(self):
        match = face_identity.FaceMatch(
            status="ambiguous", bbox=(0, 0, 10, 10),
            normalized_box=(0, 0, 0.1, 0.1), detection_score=0.9,
            person="", similarity=0.7, runner_up=0.68)
        text = face_identity.context_for_matches((match,))
        self.assertIn("unknown (ambiguous match)", text)
        self.assertNotIn("similarity=", text)


class CameraArchitectureTests(unittest.TestCase):
    def test_object_and_face_grounding_both_survive(self):
        packet = mock.Mock(local_context="YOLO: person=1", strategy="objects",
                           send_image_to_vlm=False, detector_ok=True)
        match = face_identity.FaceMatch(
            status="unknown", bbox=(0, 0, 10, 10),
            normalized_box=(0, 0, 0.1, 0.1), detection_score=0.9)
        with mock.patch("vision_pipeline.prepare_image", return_value=packet), \
             mock.patch("face_identity.identify", return_value=(match,)), \
             mock.patch("face_identity.context_for_matches",
                        return_value="FACE: unknown"):
            result = camera_perception.analyze("x.jpg", "abc", "look")
        self.assertIn("YOLO", result.model_context)
        self.assertIn("FACE", result.model_context)
        self.assertFalse(result.send_image_to_vlm)

    def test_semantic_camera_request_preserves_vlm_escalation(self):
        packet = mock.Mock(local_context="", strategy="vlm",
                           send_image_to_vlm=True, detector_ok=True)
        with mock.patch("vision_pipeline.prepare_image", return_value=packet), \
             mock.patch("face_identity.identify", return_value=()), \
             mock.patch("face_identity.context_for_matches", return_value=""):
            result = camera_perception.analyze(
                "x.jpg", "abc", "read this medicine label")
        self.assertTrue(result.send_image_to_vlm)
        self.assertEqual(result.strategy, "vlm")

    def test_one_local_model_failure_does_not_lose_the_other(self):
        match = face_identity.FaceMatch(
            status="unknown", bbox=(0, 0, 10, 10),
            normalized_box=(0, 0, 0.1, 0.1), detection_score=0.9)
        with mock.patch("vision_pipeline.prepare_image",
                        side_effect=RuntimeError("no yolo")), \
             mock.patch("face_identity.identify", return_value=(match,)), \
             mock.patch("face_identity.context_for_matches",
                        return_value="FACE: unknown"):
            result = camera_perception.analyze("x.jpg", "abc", "look")
        self.assertEqual(result.face_context, "FACE: unknown")
        self.assertEqual(len(result.warnings), 1)


class WiringTests(unittest.TestCase):
    def test_declared_actions_have_correct_privacy_controls(self):
        import capabilities
        remember = capabilities.get("REMEMBER_FACE")
        forget = capabilities.get("FORGET_FACE")
        self.assertEqual(remember.origins, ("user",))
        self.assertFalse(remember.requires_confirmation("user"))
        self.assertTrue(forget.requires_confirmation("user"))
        verify = capabilities.get("VERIFY_PRIMARY_FACE")
        self.assertEqual(verify.origins, ("user",))
        self.assertIn("fresh", verify.summary.lower())
        self.assertIn("LIST_KNOWN_FACES", verify.summary)

    def test_graph_templates_are_outside_entity_and_memory_retrieval(self):
        import inspect
        from memory.neo4j_store import GraphStore
        source = inspect.getsource(GraphStore.upsert_face_template)
        self.assertIn(":FaceTemplate", source)
        self.assertNotIn("t:Entity", source)
        self.assertNotIn("t:Memory", source)
        self.assertIn("sensitivity='restricted'", source)

    def test_camera_turn_includes_local_person_context(self):
        source = open("agent.py", encoding="utf-8").read()
        self.assertIn("camera_perception.analyze", source)
        self.assertIn("local_perception = grounded.model_context", source)


if __name__ == "__main__":
    unittest.main()
