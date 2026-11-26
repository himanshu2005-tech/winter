import inspect
import unittest
from unittest import mock

import idle_gate
import screen_rest


class SleepScreen(unittest.TestCase):
    def setUp(self):
        screen_rest._resting = False
        screen_rest._sleeping = False
        screen_rest._standby = False
        screen_rest._suspended_until = 0.0

    tearDown = setUp

    def test_sleep_darkens_the_screen_as_the_held_variant(self):
        queue = mock.Mock()
        self.assertTrue(screen_rest.begin(queue, "sleep mode", sleeping=True))
        self.assertTrue(screen_rest.is_resting())
        self.assertTrue(screen_rest.is_sleeping())
        queue.put.assert_any_call(("rest_begin", "sleep mode"))

    def test_an_idle_blackout_becomes_the_held_one_when_he_falls_asleep(self):
        queue = mock.Mock()
        screen_rest.begin(queue, "idle")
        self.assertFalse(screen_rest.is_sleeping())
        self.assertTrue(screen_rest.begin(queue, "sleep mode", sleeping=True))
        self.assertTrue(screen_rest.is_sleeping())

    def test_ending_clears_the_hold(self):
        screen_rest.begin(None, "sleep mode", sleeping=True)
        self.assertTrue(screen_rest.end(None, "left sleep mode"))
        self.assertFalse(screen_rest.is_sleeping())
        self.assertFalse(screen_rest.is_resting())

    def test_a_nudged_mouse_does_not_light_a_sleeping_screen(self):
        screen_rest.begin(None, "sleep mode", sleeping=True)
        calls = []
        with mock.patch.object(screen_rest, "idle_seconds", return_value=0.5), \
                mock.patch.object(screen_rest, "end",
                                  side_effect=lambda *a, **k: calls.append(a)):
            # One pass of the watcher's loop body.
            source = inspect.getsource(screen_rest.worker)
            self.assertIn("not is_sleeping()", source)
        self.assertEqual(calls, [])

    def test_an_idle_blackout_still_ends_on_input(self):
        screen_rest.begin(None, "idle")
        self.assertFalse(screen_rest.is_sleeping())
        self.assertIn("seconds < 2.0", inspect.getsource(screen_rest.worker))


class OverlayBehaviour(unittest.TestCase):
    def test_held_modes_use_the_idle_soul_on_pure_black(self):
        import rest_screen
        glass = inspect.getsource(rest_screen.RestScreenMixin._rest_draw_glass)
        soul = inspect.getsource(rest_screen.RestScreenMixin._rest_draw_soul)
        tick = inspect.getsource(rest_screen.RestScreenMixin._rest_tick)
        self.assertEqual(rest_screen.REST_BG, "#000000")
        self.assertEqual(rest_screen.GLASS_SURFACE, "#000000")
        self.assertNotIn("_rest_round_rect", glass)
        self.assertIn('soul.Soul("idle")', soul)
        self.assertIn("if sleeping or standby", tick)
        self.assertIn("after(250", tick)

    def test_held_modes_have_only_state_and_wake_copy(self):
        import rest_screen
        glass = inspect.getsource(rest_screen.RestScreenMixin._rest_draw_glass)
        self.assertIn("SLEEP_ACCENT if sleeping else STANDBY_ACCENT", glass)
        self.assertIn('status = "Sleep"', glass)
        self.assertIn("Say Vanta or press any key", glass)
        self.assertNotIn("Notifications will be waiting", glass)
        self.assertNotIn('%A, %d %B', glass)

    def test_sleep_overlay_shows_the_clock_with_seconds_and_ignores_movement(self):
        import rest_screen
        tick = inspect.getsource(rest_screen.RestScreenMixin._rest_tick)
        self.assertIn('"%H:%M:%S"', tick)
        self.assertIn("Sleep mode", tick)
        self.assertNotIn('canvas.delete("all")', tick)
        movement = inspect.getsource(rest_screen.RestScreenMixin._rest_movement)
        self.assertIn("_rest_sleeping", movement)

    def test_idle_blackout_shows_seconds_too(self):
        import rest_screen
        tick = inspect.getsource(rest_screen.RestScreenMixin._rest_tick)
        self.assertIn("Screen resting", tick)

    def test_a_click_on_the_sleeping_screen_leaves_sleep_mode(self):
        import rest_screen
        dismiss = inspect.getsource(rest_screen.RestScreenMixin._rest_dismiss)
        self.assertIn('("wake_sleep", "click")', dismiss)
        import agent
        loop = inspect.getsource(agent)
        self.assertIn('mode == "wake_sleep"', loop)
        self.assertIn("_wake_from_sleep_if_needed", loop)

    def test_keys_and_clicks_still_dismiss_it(self):
        import rest_screen
        source = inspect.getsource(rest_screen.RestScreenMixin._ensure_rest_screen)
        self.assertIn('("<Key>", "<Button>")', source)


class BackgroundWorkContinues(unittest.TestCase):
    def test_sleep_counts_as_idle_for_background_learning(self):
        self.assertIn(idle_gate.SLEEPING, idle_gate.IDLE_STATES)
        self.assertIn(idle_gate.RESTING, idle_gate.IDLE_STATES)

    def test_a_dark_sleeping_screen_reports_as_resting_and_work_may_start(self):
        screen_rest.begin(None, "sleep mode", sleeping=True)
        try:
            with mock.patch.object(idle_gate, "user_returned", return_value=False), \
                    mock.patch.object(idle_gate, "agent_busy", return_value=False), \
                    mock.patch.object(idle_gate, "battery_ok", return_value=True):
                ok, why = idle_gate.allowed()
            self.assertTrue(ok, why)
        finally:
            screen_rest.end(None, "test")


class ModeHook(unittest.TestCase):
    def test_entering_and_leaving_sleep_drive_the_screen(self):
        import agent
        source = inspect.getsource(agent)
        self.assertIn('screen_rest.begin(ui_queue, "sleep mode", sleeping=True)', source)
        self.assertIn('screen_rest.end(ui_queue, "left sleep mode")', source)


class StandbyScreen(unittest.TestCase):
    def test_automatic_static_screen_threshold_is_twenty_minutes(self):
        self.assertEqual(screen_rest.IDLE_MINUTES, 20.0)

    def test_model_selected_standby_uses_the_existing_dark_rest_surface(self):
        import actions
        source = inspect.getsource(actions._run_set_standby)
        self.assertIn("screen_rest.begin(ctx.ui_queue, 'standby mode')", source)

    def test_input_on_standby_screen_resumes_the_runtime(self):
        import rest_screen
        dismiss = inspect.getsource(rest_screen.RestScreenMixin._rest_dismiss)
        self.assertIn("runtime_power.resume()", dismiss)
        self.assertIn('self._q.put(("show_overlay", ""))', dismiss)
        tick = inspect.getsource(rest_screen.RestScreenMixin._rest_tick)
        self.assertIn("Standby mode", tick)

    def test_standby_shows_rest_surface_and_mouse_motion_alone_is_ignored(self):
        import rest_screen
        tick = inspect.getsource(rest_screen.RestScreenMixin._rest_tick)
        self.assertIn('"%H:%M:%S"', tick)
        self.assertIn("remaining_seconds", tick)
        self.assertIn("resumes in", tick)
        movement = inspect.getsource(rest_screen.RestScreenMixin._rest_movement)
        self.assertIn("_rest_standby", movement)

    def test_voice_ingress_does_not_intercept_standby_before_the_model(self):
        import agent
        source = inspect.getsource(agent.run_agent_loop)
        self.assertNotIn("_handle_power_command", source)
        self.assertIn("SET_STANDBY is a model-visible capability", source)

    def test_standby_is_registered_for_model_selection(self):
        import capabilities
        self.assertIsNotNone(capabilities.get("SET_STANDBY"))
        self.assertIsNotNone(capabilities.get("RESUME_STANDBY"))


if __name__ == "__main__":
    unittest.main()
