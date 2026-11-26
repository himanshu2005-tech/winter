import json
import os
import unittest
from unittest import mock

import camera_awareness
import visual_director


class VisualDirectorGateTests(unittest.TestCase):
    def setUp(self):
        self.director = visual_director.VisualDirector()
        self.env = mock.patch.dict(os.environ, {
            "VANTA_VISUAL_STABLE_SAMPLES": "2",
            "VANTA_VISUAL_MIN_REVIEW_SECONDS": "30",
            "VANTA_VISUAL_PERIODIC_SECONDS": "900",
            "VANTA_VISUAL_TOPIC_COOLDOWN_SECONDS": "600",
            "VANTA_VISUAL_MAX_INTERRUPTS_PER_HOUR": "3",
        })
        self.env.start()
        self.addCleanup(self.env.stop)

    @staticmethod
    def scene(objects=None, mode="assistant", present=True):
        return {
            "person_present": present,
            "primary_user_present": True if present else None,
            "recognized_people": ["Himanshu"] if present else [],
            "unknown_face_count": 0,
            "objects": objects or {},
            "scene_changes": ["changed"],
            "mode": mode,
            "study_present_seconds": 0,
            "study_breaks": 0,
        }

    def test_a_changed_scene_must_be_stable_before_semantic_review(self):
        scene = self.scene({"cup": 1, "book": 1})
        self.assertIsNone(self.director.consider(
            scene, events=("new_object:cup",), now=100))
        request = self.director.consider(scene, events=(), now=101)
        self.assertIsNotNone(request)
        self.assertEqual(request.reason, "stable_scene_change")

    def test_no_person_sleep_and_standby_are_quiet(self):
        self.assertIsNone(self.director.consider(
            self.scene(present=False), events=("left",), now=100))
        self.assertIsNone(self.director.consider(
            self.scene(mode="sleep"), events=("changed",), now=200))
        self.assertIsNone(self.director.consider(
            self.scene(mode="standby"), events=("changed",), now=300))

    def test_same_scene_does_not_become_a_timer_narration(self):
        scene = self.scene({"book": 1})
        self.director.consider(scene, events=("new_object:book",), now=100)
        self.assertIsNotNone(self.director.consider(scene, now=101))
        self.assertIsNone(self.director.consider(scene, now=400))
        self.assertIsNotNone(self.director.consider(scene, now=1002))

    def test_quiet_and_low_confidence_decisions_never_interrupt(self):
        quiet = visual_director.VisualDecision(
            decision="quiet", summary="reading", confidence=.99)
        weak = visual_director.VisualDecision(
            decision="ask", message="Are you eating?", topic_key="activity",
            confidence=.4)
        self.assertFalse(self.director.allow(quiet, now=100))
        self.assertFalse(self.director.allow(weak, now=100))

    def test_topic_cooldown_similarity_and_hourly_budget_prevent_nagging(self):
        first = visual_director.VisualDecision(
            decision="suggest", message="A useful, brief suggestion.",
            topic_key="focus", confidence=.9, cooldown_minutes=20)
        self.assertTrue(self.director.allow(first, now=100))
        self.assertFalse(self.director.allow(first, now=800))
        similar = visual_director.VisualDecision(
            decision="suggest", message="A useful brief suggestion!",
            topic_key="another", confidence=.9, cooldown_minutes=5)
        self.assertFalse(self.director.allow(similar, now=1400))

    def test_only_one_visual_question_waits_at_a_time(self):
        first = visual_director.VisualDecision(
            decision="ask", message="Would you like me to account for this?",
            topic_key="one", confidence=.9, cooldown_minutes=5)
        second = visual_director.VisualDecision(
            decision="ask", message="Should I adjust the current plan?",
            topic_key="two", confidence=.9, cooldown_minutes=5)
        self.assertTrue(self.director.allow(first, now=100))
        self.assertFalse(self.director.allow(second, now=200))
        self.director.note_user_turn()
        self.assertTrue(self.director.allow(second, now=201))


class VisualDirectorModelTests(unittest.TestCase):
    def test_assessment_uses_mode_scene_and_graph_context(self):
        scene = VisualDirectorGateTests.scene(
            {"bowl": 1, "chair": 1}, mode="health")
        request = visual_director.ReviewRequest(
            "stable_scene_change", "abc", ("new_object:bowl",))
        raw = json.dumps({
            "decision": "ask",
            "summary": "A meal may have started.",
            "message": "Would you like me to note this meal?",
            "topic_key": "meal-context",
            "confidence": .82,
            "cooldown_minutes": 45,
            "interrupt_now": False,
        })
        call = mock.Mock(return_value=raw)
        with mock.patch.object(visual_director, "relevant_graph_context",
                               return_value="Himanshu --PREFERS--> light lunch"):
            result = visual_director.assess(
                "aW1hZ2U=", scene, request, call_llm=call)
        self.assertEqual(result.decision, "ask")
        self.assertEqual(result.topic_key, "meal-context")
        prompt = call.call_args.args[0][0]["content"][0]["text"]
        self.assertIn('"mode": "health"', prompt)
        self.assertIn('"bowl": 1', prompt)
        self.assertIn("Himanshu --PREFERS--> light lunch", prompt)
        self.assertIn("Default to quiet", prompt)

    def test_unverified_scene_cannot_retrieve_personal_graph_context(self):
        scene = VisualDirectorGateTests.scene({"book": 1})
        scene["primary_user_present"] = None
        with mock.patch("memory.neo4j_store.store.search_facts") as search:
            self.assertEqual(visual_director.relevant_graph_context(scene), "")
        search.assert_not_called()

    def test_camera_scene_payload_is_structured_and_contains_no_pixels(self):
        state = camera_awareness.AwarenessState(
            present=True, himanshu_present=True,
            recognized_people=("Himanshu",),
            object_labels=("person", "cup", "cup"))
        scene = visual_director.scene_payload(
            state, mode="assistant", events=("new_object:cup",))
        self.assertEqual(scene["objects"], {"person": 1, "cup": 2})
        self.assertNotIn("image", scene)
        self.assertNotIn("frame", scene)


if __name__ == "__main__":
    unittest.main()
