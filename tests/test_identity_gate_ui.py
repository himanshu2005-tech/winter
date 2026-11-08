"""Regression tests for the lifecycle authentication surface.

These tests deliberately avoid opening a real Tk window.  The failure they
cover happened before any face result was rendered: Windows/Tk rejected the
combination of override-redirect and fullscreen, leaving a cached, partially
built Toplevel whose missing children crashed every subsequent camera frame.
"""

import inspect
import os
import sys
import unittest
from unittest import mock

from PIL import Image, ImageDraw

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ui


class _Widget:
    def __init__(self, *args, window_scaling=1.0, **kwargs):
        self.calls = []
        self.exists = True
        self.destroyed = False
        self.window_scaling = window_scaling

    def _call(self, name, *args, **kwargs):
        self.calls.append((name, args, kwargs))

    def withdraw(self): self._call("withdraw")
    def configure(self, *args, **kwargs): self._call("configure", *args, **kwargs)
    def geometry(self, value): self._call("geometry", value)
    def overrideredirect(self, value): self._call("overrideredirect", value)
    def attributes(self, *args): self._call("attributes", *args)
    def place(self, *args, **kwargs): self._call("place", *args, **kwargs)
    def pack(self, *args, **kwargs): self._call("pack", *args, **kwargs)
    def pack_propagate(self, value): self._call("pack_propagate", value)
    def create_oval(self, *args, **kwargs): self._call("create_oval", *args, **kwargs)
    def winfo_exists(self): return self.exists
    def winfo_screenwidth(self): return 1920
    def winfo_screenheight(self): return 1080
    def _get_window_scaling(self): return self.window_scaling

    def destroy(self):
        self.destroyed = True
        self.exists = False
        self._call("destroy")


def _bare_ui():
    instance = ui.WinterUI.__new__(ui.WinterUI)
    instance.root = object()
    instance.FONT_DISPLAY = "Segoe UI"
    instance.FONT_TEXT = "Segoe UI"
    instance._identity_gate_win = None
    instance._identity_gate_photo = None
    instance._identity_gate_canvas = None
    instance._identity_gate_percent = None
    instance._identity_gate_message = None
    instance._identity_gate_title = None
    instance._abort_identity_gate = lambda: None
    return instance


class IdentityGateWindowTests(unittest.TestCase):

    def _patch_widgets(self, window, frame_factory=_Widget):
        return (
            mock.patch("ui.ctk.CTkToplevel", return_value=window),
            mock.patch("ui.ctk.CTkFrame", side_effect=frame_factory),
            mock.patch("ui.ctk.CTkLabel", side_effect=_Widget),
            mock.patch("ui.ctk.CTkButton", side_effect=_Widget),
            mock.patch("ui.ctk.CTkFont", return_value=object()),
            mock.patch("ui.tk.Canvas", side_effect=_Widget),
        )

    def test_borderless_virtual_screen_does_not_use_incompatible_fullscreen(self):
        instance = _bare_ui()
        window = _Widget()
        patches = self._patch_widgets(window)
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
            instance._build_identity_gate()

        self.assertIs(instance._identity_gate_win, window)
        self.assertIsNotNone(instance._identity_gate_percent)
        self.assertIsNotNone(instance._identity_gate_message)
        self.assertIn(("geometry", ("1920x1080+0+0",), {}), window.calls)
        attribute_calls = [args for name, args, _ in window.calls
                           if name == "attributes"]
        self.assertNotIn(("-fullscreen", True), attribute_calls)
        self.assertIn(("-topmost", True), attribute_calls)

    def test_windows_display_scaling_keeps_the_gate_physically_centered(self):
        instance = _bare_ui()
        window = _Widget(window_scaling=1.25)
        patches = self._patch_widgets(window)
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
            instance._build_identity_gate()

        # CTk will multiply these logical dimensions by 1.25, producing an
        # exact 1920x1080 physical window whose relx=.5 card is at (960, 540).
        self.assertIn(("geometry", ("1536x864+0+0",), {}), window.calls)

    def test_gate_uses_one_background_without_a_grey_card(self):
        instance = _bare_ui()
        window = _Widget()
        frames = []

        def frame_factory(*args, **kwargs):
            frames.append(kwargs)
            return _Widget(*args, **kwargs)

        patches = self._patch_widgets(window, frame_factory=frame_factory)
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
            instance._build_identity_gate()

        self.assertEqual(frames[0].get("fg_color"), "transparent")
        self.assertEqual(frames[0].get("border_width"), 0)

    def test_gate_has_one_minimal_identity_prompt_on_pure_black(self):
        source = inspect.getsource(ui.WinterUI._build_identity_gate)
        self.assertIn('gate_bg = "#000000"', source)
        self.assertIn('text="Verifying"', source)
        self.assertNotIn("VANTA  /  IDENTITY", source)
        self.assertNotIn("Processed locally", source)
        self.assertNotIn("Look toward the camera", source)

    def test_failed_construction_is_destroyed_and_can_be_retried(self):
        instance = _bare_ui()
        failed_window = _Widget()
        patches = self._patch_widgets(
            failed_window, frame_factory=lambda *a, **k: (_ for _ in ()).throw(
                RuntimeError("frame creation failed")))
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
            with self.assertRaises(RuntimeError):
                instance._build_identity_gate()

        self.assertTrue(failed_window.destroyed)
        self.assertIsNone(instance._identity_gate_win)

        healthy_window = _Widget()
        patches = self._patch_widgets(healthy_window)
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
            instance._build_identity_gate()

        self.assertIs(instance._identity_gate_win, healthy_window)
        self.assertIsNotNone(instance._identity_gate_canvas)

    def test_detected_face_is_centered_in_the_preview_crop(self):
        source = Image.new("RGB", (400, 200), "black")
        ImageDraw.Draw(source).rectangle((40, 50, 120, 150), fill="red")
        preview = ui.centered_face_preview(
            source, (.10, .25, .30, .75), size=100)
        self.assertEqual(preview.size, (100, 100))
        red, green, blue = preview.getpixel((50, 50))
        self.assertGreater(red, 180)
        self.assertLess(green, 60)
        self.assertLess(blue, 60)


if __name__ == "__main__":
    unittest.main()
