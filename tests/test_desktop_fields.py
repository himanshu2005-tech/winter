# -*- coding: utf-8 -*-
"""
Tests for setting a field in a desktop app and knowing whether it took.

The bug these exist for: VANTA was asked to write a poem in Notepad and save
it to the Desktop. Every step reported success -- launch, type, Ctrl+S,
SET_FIELD, CLICK Save -- and the file was saved under the wrong name, the one
Notepad had defaulted to from the poem's first line.

SET_FIELD was calling ValuePattern.SetValue and returning success without
reading anything back. Adding a read-back was not enough either: on the Save As
dialog, SetValue updates the File name edit's own ValuePattern, which then
verifies perfectly, while the dialog goes on saving under the previous name.
The value has to be typed, and checked against a fresh lookup of the control
rather than the object already in hand.

The live test drives real windows, so it is opt-in.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class SetFieldContractTests(unittest.TestCase):
    """Source-level; no windows are opened."""

    @classmethod
    def setUpClass(cls):
        with open(os.path.join(ROOT, "desktop.py"), encoding="utf-8") as handle:
            cls.source = handle.read()
        start = cls.source.find("def set_value(")
        end = cls.source.find("\ndef toggle(", start)
        cls.body = cls.source[start:end]

    def test_it_no_longer_claims_success_unchecked(self):
        # The original returned straight after SetValue.
        self.assertNotIn(
            'pattern.SetValue(str(value))\n        time.sleep(0.1)\n'
            '        return f"[Success]', self.body)

    def test_the_value_is_verified(self):
        self.assertIn("def took():", self.body)
        self.assertIn("visible_value()", self.body)

    def test_verification_uses_a_fresh_lookup(self):
        # Re-reading the control we already hold is what made a failed set
        # look like a successful one.
        self.assertIn("find_control(name", self.body)

    def test_typing_is_attempted_before_setvalue(self):
        typed = self.body.find("pyautogui.typewrite(wanted")
        set_value = self.body.find("GetValuePattern().SetValue(wanted)")
        self.assertNotEqual(typed, -1, "typing path is gone")
        self.assertNotEqual(set_value, -1, "pattern path is gone")
        self.assertLess(typed, set_value,
                        "a file dialog only accepts typed input")

    def test_a_failure_says_what_the_field_actually_reads(self):
        self.assertIn("still reads", self.body)
        self.assertIn("did not take", self.body)

    def test_an_unverifiable_field_is_flagged_not_claimed(self):
        self.assertIn("[Warning]", self.body)
        self.assertIn("unconfirmed", self.body)

    def test_credential_fields_are_still_refused(self):
        self.assertIn("_SENSITIVE_HINTS", self.body)


@unittest.skipUnless(os.getenv("WINTER_DESKTOP_TESTS"),
                     "set WINTER_DESKTOP_TESTS=1 to drive real windows")
class LiveNotepadTests(unittest.TestCase):
    """Notepad, end to end: write something and save it where asked."""

    TARGET = os.path.join(os.path.expanduser("~"), "Desktop",
                          "vanta_field_test.txt")
    BODY = "one line written by a test"

    def setUp(self):
        import queue
        import actions
        self.actions = actions
        self.ui = queue.Queue()
        if os.path.exists(self.TARGET):
            os.remove(self.TARGET)

    def tearDown(self):
        import subprocess
        subprocess.run(["taskkill", "/F", "/IM", "notepad.exe"],
                       capture_output=True)
        if os.path.exists(self.TARGET):
            os.remove(self.TARGET)

    def step(self, xml, pause=1.2):
        import time
        self.actions.execute_actions._last_output = ""
        self.actions.execute_actions(xml, {}, self.ui, ROOT, origin="user")
        time.sleep(pause)
        return str(getattr(self.actions.execute_actions, "_last_output", ""))

    def test_writing_and_saving_to_a_named_path(self):
        import time
        self.step('<ACTION type="LAUNCH_APP" target="notepad" />', 2.5)
        self.step(f'<ACTION type="TYPE" text="{self.BODY}" '
                  'expect_window="Notepad" />', 1.2)
        self.step('<ACTION type="HOTKEY" keys="ctrl+s" />', 2.5)

        result = self.step(
            f'<ACTION type="SET_FIELD" name="File name" value="{self.TARGET}" />',
            1.0)
        self.assertNotIn("[Error]", result, result)

        self.step('<ACTION type="CLICK" name="Save" />', 2.5)
        time.sleep(1.5)

        self.assertTrue(os.path.exists(self.TARGET),
                        f"{self.TARGET} was not created; SET_FIELD claimed "
                        f"{result.strip()!r}")
        with open(self.TARGET, encoding="utf-8", errors="ignore") as handle:
            self.assertIn(self.BODY, handle.read())


if __name__ == "__main__":
    unittest.main()
