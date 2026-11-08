# -*- coding: utf-8 -*-
"""Reading the screen must say which window it read.

Observed twice while driving Notepad. Notepad was the foreground window and
held the text "FINDME-UNIQUE-TOKEN alpha beta". READ_SCREEN_TEXT returned:

    Text on screen (59 lines):
    File Edit Selection View Go Run Terminal Help winter Antigravity IDE ...

It OCR'd the whole desktop and led with the editor behind Notepad. Nothing
about the answer was false -- that text genuinely is on the screen -- but it
answers a different question from the one being asked. "What does this say"
while working in an app means the app, and a reply led by an unrelated
window's menu bar invites exactly the mistake of describing an inactive
window as though it were the active one.

So an unscoped read now covers the foreground window and names it. The whole
desktop is still available, by asking for it.
"""

import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import system_tools


def fake_ocr(text_by_row):
    """An _ocr_screen stand-in returning one item per row."""
    items = [{"text": text, "box": (10, row * 20, 200, row * 20 + 18)}
             for row, text in enumerate(text_by_row)]
    return items, (0, 0), ""


class AnUnscopedReadFollowsTheActiveWindow(unittest.TestCase):
    def test_the_foreground_window_rect_is_used(self):
        seen = {}

        def capture(region="", min_confidence=0.3, timeout=120, *_a, **_k):
            seen["region"] = region
            return fake_ocr(["hello from notepad"])

        with patch.object(system_tools, "_ocr_screen", side_effect=capture), \
                patch.object(system_tools, "_foreground_rect",
                             return_value=((100, 200, 900, 700),
                                           "Untitled - Notepad")):
            system_tools.read_screen_text()
        self.assertEqual(seen["region"], "100,200,900,700")

    def test_the_receipt_names_the_window_it_read(self):
        with patch.object(system_tools, "_ocr_screen",
                          side_effect=lambda *_a, **_k: fake_ocr(["hello"])), \
                patch.object(system_tools, "_foreground_rect",
                             return_value=((0, 0, 800, 600),
                                           "Untitled - Notepad")):
            receipt = system_tools.read_screen_text()
        self.assertIn("Untitled - Notepad", receipt)

    def test_an_explicit_region_is_still_honoured(self):
        seen = {}

        def capture(region="", min_confidence=0.3, timeout=120, *_a, **_k):
            seen["region"] = region
            return fake_ocr(["cropped"])

        with patch.object(system_tools, "_ocr_screen", side_effect=capture), \
                patch.object(system_tools, "_foreground_rect",
                             return_value=((0, 0, 10, 10), "Notepad")):
            system_tools.read_screen_text(region="5,6,7,8")
        self.assertEqual(seen["region"], "5,6,7,8")

    def test_the_whole_desktop_is_still_reachable(self):
        seen = {}

        def capture(region="", min_confidence=0.3, timeout=120, *_a, **_k):
            seen["region"] = region
            return fake_ocr(["everything"])

        for word in ("screen", "all", "desktop"):
            with self.subTest(word=word):
                with patch.object(system_tools, "_ocr_screen",
                                  side_effect=capture), \
                        patch.object(system_tools, "_foreground_rect",
                                     return_value=((0, 0, 10, 10), "X")):
                    receipt = system_tools.read_screen_text(region=word)
                self.assertEqual(seen["region"], "")
                self.assertIn("whole screen", receipt.lower())

    def test_an_unknown_foreground_falls_back_to_the_whole_screen(self):
        """Better the old behaviour than no answer."""
        seen = {}

        def capture(region="", min_confidence=0.3, timeout=120, *_a, **_k):
            seen["region"] = region
            return fake_ocr(["fallback"])

        with patch.object(system_tools, "_ocr_screen", side_effect=capture), \
                patch.object(system_tools, "_foreground_rect",
                             return_value=(None, "")):
            receipt = system_tools.read_screen_text()
        self.assertEqual(seen["region"], "")
        self.assertIn("fallback", receipt)

    def test_a_degenerate_rect_falls_back_rather_than_reading_nothing(self):
        """A minimised or zero-size window must not yield an empty answer."""
        seen = {}

        def capture(region="", min_confidence=0.3, timeout=120, *_a, **_k):
            seen["region"] = region
            return fake_ocr(["fallback"])

        with patch.object(system_tools, "_ocr_screen", side_effect=capture), \
                patch.object(system_tools, "_foreground_rect",
                             return_value=((50, 50, 55, 52), "Tiny")):
            system_tools.read_screen_text()
        self.assertEqual(seen["region"], "")

    def test_an_ocr_error_is_still_returned(self):
        with patch.object(system_tools, "_ocr_screen",
                          side_effect=lambda *_a, **_k: ([], (0, 0),
                                                       "[Error] no engine")), \
                patch.object(system_tools, "_foreground_rect",
                             return_value=((0, 0, 100, 100), "X")):
            self.assertIn("[Error]", system_tools.read_screen_text())

    def test_no_text_found_says_which_window_was_looked_at(self):
        with patch.object(system_tools, "_ocr_screen",
                          side_effect=lambda *_a, **_k: ([], (0, 0), "")), \
                patch.object(system_tools, "_foreground_rect",
                             return_value=((0, 0, 100, 100), "Calculator")):
            receipt = system_tools.read_screen_text()
        self.assertIn("Calculator", receipt)


if __name__ == "__main__":
    unittest.main()
