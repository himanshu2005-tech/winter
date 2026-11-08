import unittest
from unittest import mock

import numpy as np

import autonomy
import protocol_manager as pm
import turn_context


class GuestTurn(unittest.TestCase):
    def tearDown(self):
        turn_context.end()

    def test_flag_is_per_turn_and_resets(self):
        turn_context.begin("local", "user")
        self.assertFalse(turn_context.is_guest())
        turn_context.set_guest(True, "face unclear")
        self.assertTrue(turn_context.is_guest())
        self.assertEqual(turn_context.guest_reason(), "face unclear")
        turn_context.begin("local", "user")
        self.assertFalse(turn_context.is_guest())

    def test_personal_actions_are_refused_for_a_guest_but_harmless_ones_are_not(self):
        turn_context.begin("local", "user")
        turn_context.set_guest(True, "dark room")
        for name in ("READ_EMAILS", "READ_CALENDAR", "LOOKUP_CONTACTS", "DRIVE_LIST",
                     "PEOPLE_SEARCH", "SEARCH_VAULT", "TAKE_SCREENSHOT", "GET_SELECTION",
                     "BROWSER_GOTO", "GET_PASSWORD"):
            ok, message = autonomy.action_allowed(name, "user")
            self.assertFalse(ok, name)
            # The refusal is read by whoever is at the desk; it must not
            # tell them how to lift the lock.
            for secret in ("Ctrl", "Alt+O", "it's me", "its me", "override"):
                self.assertNotIn(secret.lower(), message.lower(), name)
        for name in ("GET_WEATHER", "SET_TIMER"):
            ok, _ = autonomy.action_allowed(name, "user")
            self.assertTrue(ok, name)

    def test_the_owner_is_not_affected(self):
        turn_context.begin("local", "user")
        ok, _ = autonomy.action_allowed("READ_EMAILS", "user")
        self.assertTrue(ok)

    def test_the_system_prompt_of_a_guest_carries_nothing_personal(self):
        import agent
        secret = "SECRET-PROFILE: Himanshu studies at Amrita, lives in Chennai"
        with mock.patch("memory.core_profile.get_core_profile_summary", return_value=secret):
            turn_context.begin("local", "user")
            owner = agent.get_system_prompt()
            turn_context.set_guest(True, "unclear")
            guest = agent.get_system_prompt()
        self.assertIn("SECRET-PROFILE", owner)
        self.assertNotIn("SECRET-PROFILE", guest)

    def test_personal_context_appends_are_guarded_in_the_turn(self):
        import inspect
        import agent
        ingress = inspect.getsource(agent.run_agent_loop)
        inner = inspect.getsource(agent._inner_run_agent_loop)
        self.assertLess(
            ingress.index("_establish_turn_identity(origin)"),
            ingress.index("_inner_run_agent_loop("),
            "identity policy must settle before routing or retrieval")
        self.assertLess(
            inner.index("apply_resource_policy("),
            inner.index("_collect_context(cognitive_route)"))

        from types import SimpleNamespace
        from memory import prompt_builder
        secret = "PRIVATE-CONTEXT-MUST-NOT-LEAK"
        turn_context.begin("local", "user")
        turn_context.set_guest(True, "test guest")
        bundle = SimpleNamespace(graph_text=secret, vector_text=secret)
        with mock.patch.object(prompt_builder.session_context,
                               "get_focus_state", return_value={"x": secret}), \
             mock.patch.object(prompt_builder.session_context,
                               "get_recent_chat", return_value=secret), \
             mock.patch.object(prompt_builder.session_context,
                               "get_rolling_summary", return_value=secret):
            rendered = prompt_builder.build_context("hello", bundle)
        self.assertNotIn(secret, rendered)
        self.assertIn("GUEST-SAFE", rendered)


class ProtocolShortcuts(unittest.TestCase):
    def tearDown(self):
        pm.deactivate()

    def test_short_phrases_activate_and_end(self):
        P = pm.Protocol
        cases = {"it's me": ("activate", P.OWNER_OVERRIDE), "Its me!": ("activate", P.OWNER_OVERRIDE),
                 "owner override": ("activate", P.OWNER_OVERRIDE), "/owner": ("activate", P.OWNER_OVERRIDE),
                 "lockdown": ("activate", P.LOCKDOWN), "public mode": ("activate", P.PUBLIC_SPACE),
                 "end protocol": ("end", None), "/end": ("end", None)}
        for text, expected in cases.items():
            self.assertEqual(pm.parse_command(text), expected, text)

    def test_ordinary_sentences_never_activate_anything(self):
        for text in ("what is a lockdown", "I need the lockdown report", "owner", "public",
                     "end", "tell me about the owner", "is it me or the camera"):
            self.assertIsNone(pm.parse_command(text), text)

    def test_activation_and_expiry(self):
        message = pm.activate(pm.Protocol.OWNER_OVERRIDE)
        self.assertTrue(pm.identity_bypass_active())
        self.assertIn("30 minutes", message)
        with mock.patch("protocol_manager.time.time", return_value=__import__("time").time() + 31 * 60):
            self.assertFalse(pm.identity_bypass_active())

    def test_ending_when_nothing_is_active_is_harmless(self):
        self.assertEqual(pm.run_command(("end", None)), "No protocol was active.")

    def test_hotkeys_are_ctrl_alt_letters(self):
        self.assertEqual(pm.HOTKEYS[0x4F][1], pm.Protocol.OWNER_OVERRIDE)
        self.assertEqual(pm.HOTKEYS[0x4C][1], pm.Protocol.LOCKDOWN)
        self.assertEqual(pm.HOTKEYS[0x50][1], pm.Protocol.PUBLIC_SPACE)
        self.assertEqual(pm.HOTKEYS[0x45][0], "end")

    def test_a_remote_message_cannot_confirm_the_owner_at_the_machine(self):
        import agent
        with mock.patch.object(agent, "ui_queue", mock.Mock()):
            message = agent._run_protocol_command(
                ("activate", pm.Protocol.OWNER_OVERRIDE), "telegram")
        self.assertIn("only be started at this computer", message)
        self.assertFalse(pm.identity_bypass_active())

    def test_with_a_password_set_the_claim_alone_activates_nothing(self):
        import agent
        import identity_auth
        queue = mock.Mock()
        identity_auth.reset()
        with mock.patch.object(agent, "ui_queue", queue), \
             mock.patch.dict("os.environ", {"AUTH_PASSWORD": "correct horse"}):
            self.assertTrue(agent.handle_live_control("I am Himanshu", source="text"))
            self.assertFalse(pm.identity_bypass_active())
            prompts = [call.args[0] for call in queue.put.call_args_list
                       if call.args[0][0] == "auth_prompt"]
            self.assertEqual(len(prompts), 1)
            token = prompts[0][1]["id"]
            # A wrong password does not unlock it...
            self.assertEqual(identity_auth.submit(token, "nope", queue), "wrong")
            self.assertFalse(pm.identity_bypass_active())
            # ...the right one does, once its callback has run.
            self.assertEqual(identity_auth.submit(token, "correct horse", queue), "ok")
            for _ in range(100):
                if pm.identity_bypass_active():
                    break
                __import__("time").sleep(0.02)
            self.assertTrue(pm.identity_bypass_active())
        identity_auth.reset()

    def test_the_hotkey_and_the_no_password_case_still_work(self):
        import agent
        import identity_auth
        with mock.patch.object(agent, "ui_queue", mock.Mock()):
            with mock.patch.dict("os.environ", {"AUTH_PASSWORD": "correct horse"}):
                agent._run_protocol_command(("activate", pm.Protocol.OWNER_OVERRIDE), "keys")
                self.assertTrue(pm.identity_bypass_active())
            pm.deactivate()
            with mock.patch.dict("os.environ", {"AUTH_PASSWORD": ""}):
                agent._run_protocol_command(("activate", pm.Protocol.OWNER_OVERRIDE), "text")
                self.assertTrue(pm.identity_bypass_active())
        identity_auth.reset()

    def test_ambient_speech_cannot_activate_but_can_end(self):
        import agent
        with mock.patch.object(agent, "ui_queue", mock.Mock()), \
             mock.patch.dict("os.environ", {"AUTH_PASSWORD": ""}):
            self.assertFalse(agent.handle_live_control("it's me", source="wake microphone"))
            self.assertFalse(pm.identity_bypass_active())
            self.assertTrue(agent.handle_live_control("it's me", source="wake phrase"))
            self.assertTrue(pm.identity_bypass_active())
            self.assertTrue(agent.handle_live_control("end protocol", source="wake microphone"))
            self.assertFalse(pm.identity_bypass_active())


class DarkCamera(unittest.TestCase):
    def test_a_dark_stack_is_lifted_to_a_usable_level_without_blowing_out(self):
        import camera
        import cv2
        rng = np.random.default_rng(0)
        dark = np.clip(rng.normal(6, 2, (120, 160, 3)), 0, 255).astype("float32")
        out = camera.lift_dark_stack(cv2, dark)
        self.assertEqual(out.dtype, np.uint8)
        self.assertGreater(out.mean(), 60)
        self.assertLess(out.mean(), 160)

    def test_a_bright_frame_is_left_alone(self):
        import camera
        import cv2
        bright = np.full((60, 80, 3), 140.0, dtype="float32")
        out = camera.lift_dark_stack(cv2, bright)
        self.assertAlmostEqual(float(out.mean()), 140.0, delta=1.0)

    def test_burst_only_happens_in_the_dark(self):
        import camera
        import cv2

        class Handle:
            reads = 0

            def read(self):
                Handle.reads += 1
                return True, np.full((20, 20, 3), 200, dtype="uint8")

            def set(self, *a):
                return True

        frame = np.full((20, 20, 3), 200, dtype="uint8")
        out = camera._brighten_if_dark(cv2, Handle(), frame)
        self.assertEqual(Handle.reads, 0)
        self.assertIs(out, frame)

    def test_the_tone_mapper_lifts_a_dark_picture(self):
        from PIL import Image
        import vision_pipeline
        rng = np.random.default_rng(1)
        arr = np.clip(rng.normal(7, 2, (120, 160, 3)), 0, 255).astype("uint8")
        enhanced, luma = vision_pipeline._low_light_variant(Image.fromarray(arr))
        self.assertIsNotNone(enhanced)
        self.assertGreater(vision_pipeline.image_luminance(enhanced), luma * 3)


if __name__ == "__main__":
    unittest.main()
