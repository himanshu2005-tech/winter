# -*- coding: utf-8 -*-
import json
import threading
import unittest
import urllib.error
import urllib.request

import console_shell
import console_window as cw
import soul
import web_style


class Soul(unittest.TestCase):
    def test_render_size_and_mode(self):
        image = soul.Soul("idle").render(36, "#161618")
        self.assertEqual(image.size, (36, 36))
        self.assertEqual(image.mode, "RGB")

    def test_state_changes_glide_instead_of_cutting(self):
        mind = soul.Soul("idle")
        start = mind.params["energy"]
        mind.set_state("listening")
        mind.step(0.016)
        one_frame = mind.params["energy"]
        self.assertGreater(one_frame, start)
        self.assertLess(one_frame, soul.MOODS["listening"]["energy"])
        for _ in range(300):
            mind.step(0.016)
        self.assertAlmostEqual(mind.params["energy"], soul.MOODS["listening"]["energy"], places=2)

    def test_glide_is_independent_of_frame_rate(self):
        fast, slow = soul.Soul("idle"), soul.Soul("idle")
        fast.set_state("thinking")
        slow.set_state("thinking")
        for _ in range(60):
            fast.step(1 / 60)
        for _ in range(10):
            slow.step(1 / 10)
        self.assertAlmostEqual(fast.params["speed"], slow.params["speed"], places=3)

    def test_unknown_state_falls_back_to_idle_and_huge_dt_is_clamped(self):
        mind = soul.Soul("nonsense")
        mind.set_state("also nonsense")
        self.assertEqual(mind.state, "idle")
        mind.step(1e9)
        self.assertLessEqual(mind.t, 0.25 * soul.MOODS["idle"]["speed"] + 1e-9)

    def test_level_is_clamped(self):
        mind = soul.Soul("speaking")
        mind.set_level(7)
        mind.step(1.0)
        self.assertLessEqual(mind.level, 1.0)
        mind.set_level(-3)
        mind.set_level(None)
        self.assertEqual(mind._level_target, 0.0)

    def test_separable_lobes_match_the_direct_formula(self):
        # The fast path draws each round lobe as an outer product of two 1-D
        # curves; it has to equal exp(-(dx^2+dy^2)/2s^2) evaluated per pixel.
        import numpy as np
        axis = (np.arange(40, dtype="float32") + 0.5) / 40 * 2 - 1
        cx, cy, sigma = 0.21, -0.13, 0.27
        x, y = np.meshgrid(axis, axis)
        direct = np.exp(-((x - cx) ** 2 + (y - cy) ** 2) / (2 * sigma ** 2))
        k = -1.0 / (2 * sigma * sigma)
        fast = np.exp((axis - cy) ** 2 * k)[:, None] * np.exp((axis - cx) ** 2 * k)[None, :]
        self.assertTrue(np.allclose(direct, fast, atol=1e-6))

    def test_still_frames_repeat_exactly_and_the_static_picture_is_cached(self):
        mind = soul.Soul("idle")
        first = list(mind.render(36, "#161618").getdata())
        again = list(mind.render(36, "#161618").getdata())
        self.assertEqual(first, again)
        self.assertIn((36, "#161618", 2), soul._GEOMETRY)

    def test_notch_repaints_one_photo_instead_of_allocating_each_frame(self):
        import inspect
        import ui
        source = inspect.getsource(ui.WinterUI._draw_mark)
        self.assertIn("photo.paste(image)", source)

    @staticmethod
    def _settle(mind, frames=400):
        for _ in range(frames):
            mind.step(1 / 40)
        return mind

    @staticmethod
    def _saturation(mind):
        return sum(max(c) - min(c) for c in mind.palette) / len(mind.palette)

    @staticmethod
    def _brightness(image):
        return sum(image.convert("L").getdata()) / (image.width * image.height)

    def test_verified_idle_is_alive_and_unverified_is_dull(self):
        alive = self._settle(soul.Soul("idle"))
        dull = soul.Soul("idle")
        dull.set_guarded(True)
        self._settle(dull)
        # Moves, breathes and glows far more when the owner is matched...
        self.assertGreater(alive.params["speed"], dull.params["speed"] * 3)
        self.assertGreater(alive.params["breath"], dull.params["breath"] * 4)
        self.assertGreater(alive.params["energy"], dull.params["energy"] * 2.5)
        # ...is colourful rather than grey...
        self.assertGreater(self._saturation(alive), self._saturation(dull) * 2.5)
        # ...and the difference is plain in the picture itself, not a subtlety.
        self.assertGreater(self._brightness(alive.render(36, "#161618")),
                           self._brightness(dull.render(36, "#161618")) * 1.5)

    def test_dull_glides_back_to_alive(self):
        mind = soul.Soul("idle")
        mind.set_guarded(True)
        self._settle(mind)
        mind.set_guarded(False)
        self._settle(mind, 800)
        reference = self._settle(soul.Soul("idle"))
        self.assertAlmostEqual(mind.params["energy"], reference.params["energy"], places=2)
        self.assertAlmostEqual(self._saturation(mind), self._saturation(reference), places=2)

    def test_busy_moods_keep_their_own_colour_when_unverified(self):
        thinking, guarded = soul.Soul("thinking"), soul.Soul("thinking")
        guarded.set_guarded(True)
        for _ in range(300):
            thinking.step(1 / 40)
            guarded.step(1 / 40)
        gap_busy = sum(abs(a - b) for x, y in zip(thinking.palette, guarded.palette)
                       for a, b in zip(x, y))
        idle, idle_guarded = soul.Soul("idle"), soul.Soul("idle")
        idle_guarded.set_guarded(True)
        for _ in range(300):
            idle.step(1 / 40)
            idle_guarded.step(1 / 40)
        gap_idle = sum(abs(a - b) for x, y in zip(idle.palette, idle_guarded.palette)
                       for a, b in zip(x, y))
        self.assertLess(gap_busy, gap_idle)

    def test_pulse_swells_then_settles(self):
        mind = soul.Soul("idle")
        mind.pulse()
        mind.step(0.05)
        self.assertGreater(mind.level, 0.2)
        for _ in range(120):
            mind.step(0.05)
        self.assertLess(mind.level, 0.02)

    def test_the_orb_is_not_flat(self):
        image = soul.Soul("idle").render(48, "#161618")
        self.assertGreater(len(image.getcolors(maxcolors=48 * 48)), 40)


class Design(unittest.TestCase):
    def test_shared_style_has_both_themes_and_reduced_motion(self):
        css = web_style.BASE_CSS
        self.assertIn('[data-theme="light"]', css)
        self.assertIn("prefers-reduced-motion", css)
        self.assertIn(".static", css)

    def test_console_polls_with_the_header_not_the_url(self):
        js = console_shell.CONSOLE_JS
        self.assertIn("X-Report-Token", js)
        self.assertNotIn("innerHTML=S.latest_html+", js)

    def test_every_nav_page_has_a_view(self):
        js = console_shell.CONSOLE_JS
        for page in ("Home", "Control", "Chat", "Activity", "Output", "Brain"):
            self.assertIn(f"{page}:function", js)


class Bridge(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.bridge = cw.ConsoleBridge(sinks={
            "command": lambda text: self.calls.append(("command", text)),
            "launch": lambda target: self.calls.append(("launch", target)),
        })
        self.bridge.data["modes"] = [{"id": "study", "label": "Study", "blurb": ""}]

    def wait(self):
        for thread in threading.enumerate():
            if thread.name.startswith("vanta-console-"):
                thread.join(2)

    def test_unknown_and_malformed_operations_are_refused(self):
        self.assertFalse(self.bridge.control("format_disk", {})[0])
        self.assertFalse(self.bridge.control("command", "not a dict")[0])
        self.assertFalse(self.bridge.control("volume", {"level": "loud"})[0])
        self.assertEqual(self.calls, [])

    def test_commands_go_through_the_normal_input_path(self):
        self.assertTrue(self.bridge.control("command", {"text": "  what time is it  "})[0])
        self.wait()
        self.assertEqual(self.calls, [("command", "what time is it")])
        self.assertFalse(self.bridge.control("command", {"text": "   "})[0])

    def test_mode_must_be_a_known_mode(self):
        self.assertFalse(self.bridge.control("mode", {"name": "root"})[0])
        self.assertTrue(self.bridge.control("mode", {"name": "study"})[0])
        self.wait()
        self.assertEqual(self.calls, [("command", "study mode")])

    def test_scripts_cannot_be_launched(self):
        for target in ("evil.bat", "C:\\x\\run.PS1", "a.vbs", "setup.msi"):
            self.assertFalse(self.bridge.control("launch", {"target": target})[0], target)
        self.assertTrue(self.bridge.control("launch", {"target": "calc.exe"})[0])
        self.wait()
        self.assertEqual(self.calls, [("launch", "calc.exe")])

    def test_typed_message_is_shown_once(self):
        self.bridge.control("command", {"text": "hello"})
        self.bridge.user_said("hello")
        self.assertEqual([m["role"] for m in self.bridge.snapshot()["chat"]], ["you"])

    def test_answers_are_rendered_and_headline_has_no_markdown(self):
        self.bridge.answered("# Today\n\nYou have **two** things.")
        state = self.bridge.snapshot()
        self.assertEqual(state["headline"], "Today")
        self.assertIn("<strong>two</strong>", state["latest_html"])
        self.assertNotIn("<script", state["latest_html"])

    def test_history_is_bounded(self):
        for i in range(cw.MAX_ACTIVITY + 50):
            self.bridge.activity(f"event {i}")
        for i in range(cw.MAX_CHAT + 50):
            self.bridge.answered(f"answer {i}")
        state = self.bridge.snapshot()
        self.assertEqual(len(state["activity"]), cw.MAX_ACTIVITY)
        self.assertEqual(len(state["chat"]), cw.MAX_CHAT)

    def test_goto_counts_up_so_the_same_page_can_be_requested_again(self):
        self.bridge.goto("Chat")
        first = self.bridge.snapshot()["goto"]["n"]
        self.bridge.goto("Chat")
        self.assertEqual(self.bridge.snapshot()["goto"]["n"], first + 1)


class Server(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sent = []
        cls.bridge = cw.ConsoleBridge(sinks={"command": cls.sent.append})
        cls.server = cw.ConsoleServer(cls.bridge)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.server.port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def req(self, path, token=True, host=None, method="GET", body=None):
        request = urllib.request.Request(self.base + path, method=method,
                                         data=json.dumps(body).encode() if body is not None else None)
        if token:
            request.add_header("X-Report-Token", self.server.token)
        if host:
            request.add_header("Host", host)
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()

    def test_page_and_data_need_the_token(self):
        self.assertEqual(self.req("/console", token=False)[0], 403)
        self.assertEqual(self.req(f"/console?t={self.server.token}", token=False)[0], 200)
        self.assertEqual(self.req("/api/console", token=False)[0], 403)
        self.assertEqual(self.req(f"/api/console?t={self.server.token}", token=False)[0], 403)
        self.assertEqual(self.req("/api/console")[0], 200)

    def test_foreign_host_is_refused(self):
        self.assertEqual(self.req("/api/console", host="evil.example")[0], 421)
        self.assertEqual(self.req("/api/control", method="POST", host="evil.example",
                                  body={"op": "command", "args": {"text": "x"}})[0], 421)

    def test_control_needs_the_token_and_an_allowed_op(self):
        post = {"op": "command", "args": {"text": "hi"}}
        self.assertEqual(self.req("/api/control", method="POST", token=False, body=post)[0], 403)
        self.assertEqual(self.req("/api/control", method="POST", body={"op": "rm", "args": {}})[0], 400)
        self.assertEqual(self.req("/api/control", method="POST", body=post)[0], 200)

    def test_version_moves_only_when_something_changed(self):
        first, _ = self.server.refresh()
        again, _ = self.server.refresh()
        self.assertEqual(first, again)
        self.bridge.activity("something happened")
        later, _ = self.server.refresh()
        self.assertGreater(later, first)

    def test_other_methods_and_paths_are_refused(self):
        self.assertEqual(self.req("/api/console", method="DELETE")[0], 405)
        self.assertEqual(self.req("/nope")[0], 404)

    def test_binds_to_loopback_only(self):
        self.assertEqual(self.server.server_address[0], "127.0.0.1")


class Wiring(unittest.TestCase):
    def test_the_dashboard_toggle_prefers_the_web_console_with_a_fallback(self):
        import inspect
        import ui
        toggle = inspect.getsource(ui.WinterUI._toggle_dashboard)
        self.assertIn("_web_console(toggle=True)", toggle)
        self.assertIn("self.dashboard_visible", toggle)           # the Tk window remains
        self.assertIn("VANTA_CONSOLE_WINDOW", inspect.getsource(ui.WinterUI._console_enabled))

    def test_the_window_geometry_fits_small_screens(self):
        for size in ((1366, 768), (1920, 1080), (2560, 1440), (1024, 700)):
            w, h, x, y = cw.window_geometry(*size)
            self.assertGreaterEqual(x, 0)
            self.assertGreaterEqual(y, 0)
            self.assertLessEqual(x + w, size[0])
            self.assertLessEqual(y + h, size[1])


if __name__ == "__main__":
    unittest.main()
