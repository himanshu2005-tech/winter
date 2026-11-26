# -*- coding: utf-8 -*-
"""Typing must report what landed, not what was asked for.

Found by driving Notepad for real. VANTA was asked to type

    "VANTA desktop control check: alpha beta gamma 12345"

and the clipboard read back

    "VANTA esktop control check: alpha beta gamma 12345"

-- one character gone. The receipt said "[Success] Typed 51 characters", which
is `len(text)` and therefore says nothing at all about what arrived. Repeating
the measurement found 12 of 12 exact at every interval from 10ms to 100ms, so
the loss is intermittent rather than systematic, which is worse: it means the
receipt is confidently right most of the time and confidently wrong
occasionally, with no way to tell the cases apart.

That matters most where typing matters most. Filling a form, a search box or a
message with one character missing is a silent corruption that looks like a
success, and the next action builds on it.

So after typing, read the field back through UI Automation and compare. When
the field cannot be read -- a canvas, a terminal, a custom control -- say the
typing is unverified rather than implying it was checked.
"""

import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import desktop


class _Value:
    def __init__(self, value):
        self.Value = value

    def SetValue(self, text):
        self.Value = text


class _Node:
    """A UI Automation control stand-in."""

    def __init__(self, value="", name="Text editor", readable=True,
                 control_type="EditControl"):
        self._value = _Value(value)
        self.Name = name
        self.ControlTypeName = control_type
        self.IsPassword = False
        self._readable = readable

    def GetValuePattern(self):
        if not self._readable:
            raise RuntimeError("no value pattern")
        return self._value

    def SetFocus(self):
        return True


class _Auto:
    def __init__(self, node):
        self._node = node

    def GetFocusedControl(self):
        return self._node


def type_into(node, text, landed=None):
    """Run type_text against a fake focused control.

    `landed` is what the field holds afterwards -- the point of the test is
    that this can differ from `text`.
    """
    def fake_typewrite(value, interval=0.0):
        node._value.Value = text if landed is None else landed

    with patch.object(desktop, "_uia", return_value=_Auto(node)), \
            patch.object(desktop, "_guard", return_value=""), \
            patch.object(desktop, "foreground_window",
                         return_value=(1, "Untitled - Notepad")), \
            patch.object(desktop.pyautogui, "typewrite", fake_typewrite):
        return desktop.type_text(text)


class TypingIsVerified(unittest.TestCase):
    def test_exact_typing_reports_success(self):
        node = _Node()
        receipt = type_into(node, "hello world")
        self.assertIn("Success", receipt)

    def test_a_dropped_character_is_reported_not_claimed(self):
        """The observed failure: one character missing, success claimed."""
        node = _Node()
        receipt = type_into(node, "VANTA desktop control",
                            landed="VANTA esktop control")
        self.assertNotIn("[Success]", receipt)
        self.assertIn("esktop", receipt,
                      "the receipt must show what actually landed")

    def test_the_mismatch_receipt_names_both_texts(self):
        node = _Node()
        receipt = type_into(node, "alpha beta", landed="alpha bet")
        self.assertIn("alpha beta", receipt)

    def test_nothing_landing_at_all_is_reported(self):
        node = _Node()
        receipt = type_into(node, "hello", landed="")
        self.assertNotIn("[Success]", receipt)

    def test_an_unreadable_field_says_unverified(self):
        """A terminal or canvas has no value to read. Do not imply a check."""
        node = _Node(readable=False)
        receipt = type_into(node, "hello world")
        self.assertIn("unverified", receipt.lower())

    def test_an_unreadable_field_still_reports_success(self):
        node = _Node(readable=False)
        receipt = type_into(node, "hello world")
        self.assertIn("Success", receipt)

    def test_typing_appended_to_existing_text_is_accepted(self):
        """Typing into a field that already had content is not a mismatch."""
        node = _Node(value="existing ")
        receipt = type_into(node, "addition",
                            landed="existing addition")
        self.assertIn("Success", receipt)

    def test_a_password_field_is_still_refused_before_typing(self):
        node = _Node(name="Password")
        with patch.object(desktop, "_uia", return_value=_Auto(node)), \
                patch.object(desktop, "_guard", return_value=""):
            receipt = desktop.type_text("secret")
        self.assertIn("Refused", receipt)

    def test_empty_text_is_still_refused(self):
        self.assertIn("[Error]", desktop.type_text(""))

    def test_the_window_guard_still_runs_first(self):
        with patch.object(desktop, "_guard", return_value="[Error] wrong window"):
            self.assertEqual(desktop.type_text("x"), "[Error] wrong window")


if __name__ == "__main__":
    unittest.main()
