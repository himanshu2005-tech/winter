"""Deterministic tests for the local raised-finger wake channel."""

from pathlib import Path
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import gesture_wake


class _Category:
    def __init__(self, name, score):
        self.category_name = name
        self.score = score


class _Result:
    def __init__(self, gestures):
        self.gestures = gestures


class PointingUpGateTests(unittest.TestCase):
    def test_requires_several_confident_frames(self):
        gate = gesture_wake.PointingUpGate(
            threshold=.72, confirmations=3, releases=2, cooldown=0)
        self.assertFalse(gate.update("Pointing_Up", .90, now=1))
        self.assertFalse(gate.update("Pointing_Up", .91, now=2))
        self.assertTrue(gate.update("Pointing_Up", .92, now=3))

    def test_low_confidence_pose_is_not_a_wake(self):
        gate = gesture_wake.PointingUpGate(
            threshold=.72, confirmations=2, releases=2, cooldown=0)
        for tick in range(8):
            self.assertFalse(gate.update("Pointing_Up", .71, now=tick))

    def test_holding_a_finger_fires_only_once(self):
        gate = gesture_wake.PointingUpGate(
            threshold=.7, confirmations=2, releases=2, cooldown=0)
        fired = [gate.update("Pointing_Up", .95, now=tick)
                 for tick in range(10)]
        self.assertEqual(fired.count(True), 1)

    def test_lowering_then_raising_rearms(self):
        gate = gesture_wake.PointingUpGate(
            threshold=.7, confirmations=2, releases=2, cooldown=0)
        self.assertFalse(gate.update("Pointing_Up", .95, now=1))
        self.assertTrue(gate.update("Pointing_Up", .95, now=2))
        self.assertFalse(gate.update("None", .99, now=3))
        self.assertFalse(gate.update("None", .99, now=4))
        self.assertFalse(gate.update("Pointing_Up", .95, now=5))
        self.assertTrue(gate.update("Pointing_Up", .95, now=6))

    def test_other_canned_gestures_do_not_activate(self):
        gate = gesture_wake.PointingUpGate(
            threshold=.7, confirmations=2, releases=1, cooldown=0)
        for label in ("Open_Palm", "Thumb_Up", "Victory", "Closed_Fist"):
            self.assertFalse(gate.update(label, .99, now=10))


class GestureSetGateTests(unittest.TestCase):
    def test_each_supported_pose_emits_its_own_event(self):
        gate = gesture_wake.GestureSetGate(
            threshold=.7, confirmations=2, releases=1, cooldown=0)
        for offset, label in enumerate(gate.LABELS):
            start = offset * 20
            # Fist/thumb consent poses deliberately need two extra frames.
            result = ""
            for tick in range(5):
                result = gate.update(label, .95, now=start + tick) or result
            self.assertEqual(result, label)
            gate.update("None", .99, now=start + 10)

    def test_a_held_pose_never_repeats(self):
        gate = gesture_wake.GestureSetGate(
            threshold=.7, confirmations=2, releases=2, cooldown=0)
        fired = [gate.update("Open_Palm", .95, now=tick)
                 for tick in range(10)]
        self.assertEqual(fired.count("Open_Palm"), 1)

    def test_noise_below_threshold_fires_nothing(self):
        gate = gesture_wake.GestureSetGate(threshold=.8, confirmations=2)
        for label in gate.LABELS:
            self.assertEqual(gate.update(label, .79), "")


class ResultParsingTests(unittest.TestCase):
    def test_reads_the_highest_ranked_category(self):
        result = _Result([[_Category("Pointing_Up", .87)]])
        self.assertEqual(gesture_wake.top_gesture(result),
                         ("Pointing_Up", .87))

    def test_empty_result_is_safe(self):
        self.assertEqual(gesture_wake.top_gesture(_Result([])), ("", 0.0))
        self.assertEqual(gesture_wake.top_gesture(None), ("", 0.0))


class InstallationTests(unittest.TestCase):
    def test_official_model_is_present_with_expected_hash(self):
        base = Path(__file__).resolve().parents[1]
        path = gesture_wake.model_path(base)
        self.assertTrue(path.is_file())
        self.assertEqual(gesture_wake._sha256(path), gesture_wake.MODEL_SHA256)

    def test_agent_registers_gesture_as_a_wake_channel(self):
        source = (Path(__file__).resolve().parents[1] / "agent.py").read_text(
            encoding="utf-8", errors="replace")
        self.assertIn("gesture_wake.start(BASE_DIR, _on_gesture_wake", source)
        self.assertIn('_record_until_silence("raised finger")', source)
        self.assertIn('set_state(STATE_LISTENING, "Listening")', source)


if __name__ == "__main__":
    unittest.main()
