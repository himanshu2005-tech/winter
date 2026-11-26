import base64
from io import BytesIO
import os
import unittest
from unittest import mock

from PIL import Image

import cognitive_pipeline
import vision_pipeline


def tiny_image():
    buffer = BytesIO()
    Image.new("RGB", (32, 24), "white").save(buffer, "JPEG")
    return base64.b64encode(buffer.getvalue()).decode()


class StrategySelection(unittest.TestCase):
    def test_objects_are_local_and_textual_screens_use_the_vlm(self):
        self.assertEqual(
            vision_pipeline.choose_strategy("how many people can you see"),
            "objects")
        self.assertEqual(
            vision_pipeline.choose_strategy("read and explain this diagram"),
            "vlm")

    def test_mixed_or_generic_screen_questions_use_both(self):
        self.assertEqual(
            vision_pipeline.choose_strategy("where is the person in this diagram"),
            "hybrid")
        self.assertEqual(
            vision_pipeline.choose_strategy("what is on my screen"),
            "hybrid")

    def test_invalid_model_choice_is_safely_reclassified(self):
        self.assertEqual(
            vision_pipeline.choose_strategy("read this page", "magic"),
            "vlm")


class PacketRouting(unittest.TestCase):
    @mock.patch("vision.take_screenshot_b64")
    @mock.patch("vision_pipeline.detect")
    def test_object_question_sends_structure_not_pixels(self, detect, capture):
        capture.return_value = (tiny_image(), "")
        detect.return_value = ((vision_pipeline.Detection(
            "person", .91, (1, 2, 10, 20), (.03, .08, .31, .83)),), True)
        packet = vision_pipeline.prepare_screen(
            "how many people are visible", ".", "auto")
        self.assertEqual(packet.strategy, "objects")
        self.assertFalse(packet.send_image_to_vlm)
        self.assertIn("person=1", packet.local_context)

    @mock.patch("vision.take_screenshot_b64")
    @mock.patch("vision_pipeline.detect", return_value=((), False))
    def test_detector_failure_falls_back_to_vlm(self, _detect, capture):
        capture.return_value = (tiny_image(), "")
        packet = vision_pipeline.prepare_screen(
            "how many people are visible", ".", "objects")
        self.assertEqual(packet.strategy, "vlm")
        self.assertTrue(packet.send_image_to_vlm)

    @mock.patch("vision.take_screenshot_b64")
    @mock.patch("vision_pipeline.detect", return_value=((), True))
    def test_hybrid_keeps_both_evidence_paths(self, _detect, capture):
        capture.return_value = (tiny_image(), "")
        packet = vision_pipeline.prepare_screen(
            "what is on my screen", ".", "hybrid")
        self.assertEqual(packet.strategy, "hybrid")
        self.assertTrue(packet.send_image_to_vlm)
        self.assertIn("YOLO26s", packet.local_context)

    @mock.patch("vision_pipeline.detect", return_value=((), True))
    def test_dark_miss_is_exposed_as_uncertain_to_the_model(self, _detect):
        buffer = BytesIO()
        Image.new("RGB", (32, 24), (5, 5, 5)).save(buffer, "JPEG")
        encoded = base64.b64encode(buffer.getvalue()).decode()
        packet = vision_pipeline.prepare_image(
            "how many people can you see", encoded, "objects")
        self.assertLess(packet.luminance, 58)
        self.assertIn("LIGHTING: low", packet.local_context)
        self.assertIn("uncertain", packet.local_context)


class CognitiveVisionDecision(unittest.TestCase):
    def test_router_choice_survives_validation(self):
        route = cognitive_pipeline._validate({
            "mode_decision": "keep", "target_mode": "assistant",
            "interaction": "respond", "task_kind": "observation",
            "planning_depth": "direct", "needs_screen": True,
            "vision_strategy": "hybrid", "confidence": 1,
            "retrieval": {"graph": False},
        }, "assistant")
        self.assertEqual(route.vision_strategy, "hybrid")

    def test_no_screen_forces_no_vision_work(self):
        route = cognitive_pipeline._validate({
            "mode_decision": "keep", "target_mode": "assistant",
            "interaction": "respond", "task_kind": "conversation",
            "planning_depth": "direct", "needs_screen": False,
            "vision_strategy": "hybrid", "confidence": 1,
            "retrieval": {"graph": False},
        }, "assistant")
        self.assertEqual(route.vision_strategy, "none")


class Configuration(unittest.TestCase):
    def test_requested_model_is_yolo26_small(self):
        self.assertEqual(vision_pipeline.MODEL_NAME, "yolo26s.pt")

    def test_cpu_is_default_even_when_cuda_appears_available(self):
        # Merely importing/probing a mismatched cuDNN stack can terminate a
        # Windows process below Python's exception boundary.  GPU use must be
        # an explicit deployment decision, never an availability guess.
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("VANTA_YOLO_DEVICE", None)
            with mock.patch.dict("sys.modules", {"torch": mock.Mock()}):
                self.assertEqual(vision_pipeline._device(), "cpu")

    def test_validated_gpu_can_be_selected_explicitly(self):
        with mock.patch.dict(os.environ, {"VANTA_YOLO_DEVICE": "0"}):
            self.assertEqual(vision_pipeline._device(), "0")

    def test_agent_uses_adaptive_pipeline(self):
        source = open(os.path.join(os.path.dirname(__file__), "..", "agent.py"),
                      encoding="utf-8").read()
        self.assertIn("from vision_pipeline import prepare_screen", source)
        self.assertIn("vision_strategy", source)

    def test_dark_frames_get_a_local_detection_variant(self):
        dark = Image.new("RGB", (64, 48), (8, 8, 8))
        enhanced, luminance = vision_pipeline._low_light_variant(dark)
        self.assertIsNotNone(enhanced)
        self.assertLess(luminance, 58)
        self.assertEqual(enhanced.size, dark.size)

    def test_normal_frames_are_not_needlessly_processed_twice(self):
        normal = Image.new("RGB", (64, 48), (150, 150, 150))
        enhanced, luminance = vision_pipeline._low_light_variant(normal)
        self.assertIsNone(enhanced)
        self.assertGreater(luminance, 58)


if __name__ == "__main__":
    unittest.main()
