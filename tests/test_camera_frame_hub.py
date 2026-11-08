"""The gesture recognizer must share, not compete for, the Windows camera."""

import os
import sys
import unittest

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import camera


class FrameHubTests(unittest.TestCase):
    def test_latest_is_a_copy_not_shared_mutable_memory(self):
        hub = camera.FrameHub()
        original = np.full((8, 10, 3), 42, dtype=np.uint8)
        hub._publish(original, 1)
        first, index = hub.latest(max_age=5)
        self.assertEqual(index, 1)
        first[:] = 0
        second, _ = hub.latest(max_age=5)
        self.assertTrue(np.all(second == 42))

    def test_stale_or_absent_frames_are_not_returned(self):
        hub = camera.FrameHub()
        frame, index = hub.latest(max_age=.01)
        self.assertIsNone(frame)
        self.assertIsNone(index)

    def test_mark_down_clears_the_last_frame_and_reports_cause(self):
        hub = camera.FrameHub()
        hub._publish(np.zeros((2, 2, 3), dtype=np.uint8), 0)
        hub._mark_down("camera disconnected")
        frame, _ = hub.latest(max_age=5)
        self.assertIsNone(frame)
        self.assertIn("disconnected", hub.status()["error"])


if __name__ == "__main__":
    unittest.main()
