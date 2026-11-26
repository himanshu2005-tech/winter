# -*- coding: utf-8 -*-
"""VANTA must never appear as pythonw.exe in native Windows UI."""

import inspect
import os
import unittest
from unittest import mock

import approval_prompt
import blackboard
import control_center
import hud
import rest_screen
import ui
import window_identity
import workbench


class WindowIdentityTests(unittest.TestCase):
    def test_process_has_a_stable_windows_application_id(self):
        self.assertEqual(window_identity.APP_USER_MODEL_ID,
                         "VANTA.PersonalAssistant")
        source = inspect.getsource(window_identity.configure_process)
        self.assertIn("SetCurrentProcessExplicitAppUserModelID", source)

    def test_idle_orb_is_preferred_over_the_legacy_icon(self):
        with mock.patch("window_identity.Path.exists", return_value=True):
            ico, png = window_identity.icon_paths(os.path.dirname(ui.__file__))
        self.assertTrue(ico.endswith("vanta-idle.ico"), ico)
        self.assertTrue(png.endswith("vanta-idle-64.png"), png)

    def test_identity_is_configured_before_the_first_tk_root(self):
        module = inspect.getsource(ui)
        self.assertLess(module.index("window_identity.configure_process()"),
                        module.index("self.root = tk.Tk()"))

    def test_native_result_window_gets_the_vanta_icon_immediately(self):
        source = inspect.getsource(ui.WinterUI._build_info_window)
        self.assertIn("window_identity.apply_to", source)
        # It remains a real native window so minimize/restore still works.
        self.assertIn("self.info_win.overrideredirect(False)", source)
        self.assertIn("window_identity.hide_from_taskbar", source)
        self.assertIn("tool_window=False", source)

    def test_late_icon_refresh_uses_the_same_central_helper(self):
        source = inspect.getsource(control_center.ControlCenterMixin._cc_apply_icon)
        self.assertIn("window_identity.apply_to", source)
        self.assertNotIn('"winter.ico"', source)

    def test_taskbar_style_removes_forced_appwindow(self):
        original = (window_identity.WS_EX_APPWINDOW | 0x20 | 0x80000)
        result = window_identity.hidden_taskbar_exstyle(original)
        self.assertFalse(result & window_identity.WS_EX_APPWINDOW)
        self.assertTrue(result & window_identity.WS_EX_TOOLWINDOW)
        # Click-through/layered bits owned by the caller must survive.
        self.assertTrue(result & 0x20)
        self.assertTrue(result & 0x80000)

    def test_owned_reader_keeps_normal_window_chrome(self):
        original = (window_identity.WS_EX_APPWINDOW |
                    window_identity.WS_EX_TOOLWINDOW | 0x100)
        result = window_identity.hidden_taskbar_exstyle(
            original, tool_window=False)
        self.assertFalse(result & window_identity.WS_EX_APPWINDOW)
        self.assertFalse(result & window_identity.WS_EX_TOOLWINDOW)
        self.assertTrue(result & 0x100)

    def test_map_hook_reapplies_policy_after_deiconify(self):
        class FakeWindow:
            def __init__(self):
                self.callback = None

            def bind(self, event, callback, add=None):
                self.callback = callback
                self.binding = (event, add)

            def after_idle(self, callback):
                callback()

            def winfo_exists(self):
                return True

        window = FakeWindow()
        owner = object()
        with mock.patch("window_identity._apply_taskbar_policy",
                        return_value=True) as apply:
            self.assertTrue(window_identity.hide_from_taskbar(
                window, owner, tool_window=False))
            self.assertEqual(window.binding, ("<Map>", "+"))
            window.callback()
        self.assertEqual(apply.call_count, 2)
        apply.assert_called_with(window, owner, tool_window=False)

    def test_main_ambient_root_is_hidden_before_it_is_ever_mapped(self):
        source = inspect.getsource(ui.WinterUI._build_border_window)
        self.assertLess(source.index("self.root.withdraw()"),
                        source.index("window_identity.hide_from_taskbar"))
        self.assertLess(source.index("window_identity.hide_from_taskbar"),
                        source.index("self.root.deiconify()"))

    def test_every_passive_surface_uses_the_central_taskbar_policy(self):
        builders = (
            ui.WinterUI._build_identity_gate,
            ui.WinterUI._show_light,
            ui.WinterUI._ensure_annotation_canvas,
            ui.WinterUI._build_notch,
            ui.WinterUI._build_input_window,
            control_center.ControlCenterMixin._build_dashboard_window,
            rest_screen.RestScreenMixin._ensure_rest_screen,
            approval_prompt.ApprovalPromptMixin._build_approval_prompt,
            hud.HudMixin._ensure_hud,
            blackboard.BlackboardMixin._ensure_board,
            blackboard.BlackboardMixin._build_pager,
            workbench.WorkbenchMixin._ensure_workbench,
        )
        missing = [function.__qualname__ for function in builders
                   if "hide_from_taskbar" not in inspect.getsource(function)]
        self.assertEqual(missing, [])

    def test_startup_face_gate_claims_identity_before_tk(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        path = os.path.join(root, "scripts", "identity_preflight.py")
        with open(path, encoding="utf-8") as handle:
            source = handle.read()
        self.assertLess(source.index("window_identity.configure_process()"),
                        source.index("self.root = tk.Tk()"))
        self.assertIn("window_identity.hide_from_taskbar(self.root)", source)


if __name__ == "__main__":
    unittest.main()
