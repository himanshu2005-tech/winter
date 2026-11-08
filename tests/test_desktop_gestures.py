# -*- coding: utf-8 -*-
"""High-end canvas gestures must be safe, expressive, and observable."""

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import desktop


class _AbortAfterTwoFrames:
    def __init__(self):
        self.calls = 0

    def is_set(self):
        self.calls += 1
        return self.calls >= 2


class CanvasGestureTests(unittest.TestCase):
    def _patches(self, signatures=None):
        signatures = signatures or [
            (bytes([0]) * 64, "Blender"),
            (bytes([20]) * 64, "Blender"),
        ]
        return (
            mock.patch.object(desktop, "_guard", return_value=None),
            mock.patch.object(desktop, "to_screen",
                              side_effect=lambda x, y, _space: (int(x), int(y))),
            mock.patch.object(desktop, "_visual_signature",
                              side_effect=signatures),
            mock.patch.object(desktop.time, "sleep"),
            mock.patch.object(desktop.pyautogui, "moveTo"),
            mock.patch.object(desktop.pyautogui, "keyDown"),
            mock.patch.object(desktop.pyautogui, "keyUp"),
            mock.patch.object(desktop.pyautogui, "mouseDown"),
            mock.patch.object(desktop.pyautogui, "mouseUp"),
        )

    def test_shift_middle_drag_supports_blender_pan_and_verifies_change(self):
        patches = self._patches()
        with patches[0], patches[1], patches[2], patches[3], \
                patches[4], patches[5] as down, patches[6] as up, \
                patches[7] as mouse_down, patches[8] as mouse_up:
            receipt = desktop.drag(10, 20, 80, 90, duration=.05,
                                   button="middle", modifiers="shift")
        down.assert_called_once_with("shift")
        up.assert_called_once_with("shift")
        mouse_down.assert_called_once_with(button="middle")
        mouse_up.assert_called_once_with(button="middle")
        self.assertIn("[Success]", receipt)
        self.assertIn("shift+middle", receipt)
        self.assertIn("visual change", receipt)

    def test_abort_releases_mouse_and_every_modifier(self):
        patches = self._patches()
        with patches[0], patches[1], patches[2], patches[3], \
                patches[4], patches[5], patches[6] as up, \
                patches[7], patches[8] as mouse_up, \
                mock.patch.object(desktop.pyautogui, "position",
                                  return_value=(20, 30)):
            receipt = desktop.drag(10, 20, 80, 90, duration=2,
                                   button="right", modifiers="ctrl+shift",
                                   interrupt_event=_AbortAfterTwoFrames())
        mouse_up.assert_called_once_with(button="right")
        self.assertEqual([call.args[0] for call in up.call_args_list],
                         ["shift", "ctrl"])
        self.assertIn("[Cancelled]", receipt)

    def test_no_visual_change_is_not_claimed_as_success(self):
        same = bytes([4]) * 64
        patches = self._patches([(same, "Blender"), (same, "Blender")])
        with patches[0], patches[1], patches[2], patches[3], patches[4], \
                patches[5], patches[6], patches[7], patches[8]:
            receipt = desktop.drag(1, 2, 3, 4, duration=.05)
        self.assertIn("[Warning]", receipt)
        self.assertIn("no measurable visual change", receipt)


if __name__ == "__main__":
    unittest.main()
