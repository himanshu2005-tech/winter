import os
import queue
import time
import unittest
from unittest import mock

import numpy as np

import authentication_gate as gate
import face_identity
import identity_auth
import runtime_power


class AuthenticationGateTests(unittest.TestCase):
    def setUp(self):
        gate.reset_for_tests()
        identity_auth.reset()
        gate.configure(os.getcwd(), queue.Queue())

    def tearDown(self):
        gate.reset_for_tests()
        identity_auth.reset()

    def test_starts_closed_and_unlocks_only_after_evidence(self):
        gate.require("startup")
        self.assertTrue(gate.awaiting())
        self.assertFalse(gate.is_unlocked())
        self.assertTrue(gate.unlock("test face"))
        self.assertTrue(gate.is_unlocked())
        self.assertFalse(gate.awaiting())

    def test_wrong_telegram_password_is_consumed_without_unlocking(self):
        gate.require("startup")
        with mock.patch.dict(os.environ, {"AUTH_PASSWORD": "correct"}), \
             mock.patch("providers.telegram_bot.send_telegram_message"):
            handled = gate.handle_telegram("wrong", 7, 7)
        self.assertTrue(handled)
        self.assertFalse(gate.is_unlocked())

    def test_only_the_paired_chat_can_unlock(self):
        gate.require("startup")
        with mock.patch.dict(os.environ, {"AUTH_PASSWORD": "correct"}), \
             mock.patch("providers.telegram_bot.send_telegram_message"):
            self.assertTrue(gate.handle_telegram("correct", 8, 7))
            self.assertFalse(gate.is_unlocked())
            self.assertTrue(gate.handle_telegram("correct", 7, 7))
        self.assertTrue(gate.is_unlocked())

    def test_password_never_enters_an_agent_queue(self):
        gate.require("startup")
        sent = []
        with mock.patch.dict(os.environ, {"AUTH_PASSWORD": "correct"}), \
             mock.patch("providers.telegram_bot.send_telegram_message",
                        side_effect=sent.append):
            gate.handle_telegram("correct", 7, 7)
        self.assertTrue(gate.is_unlocked())
        self.assertTrue(sent)
        self.assertNotIn("correct", " ".join(sent).lower())

    def test_primary_verification_scores_only_primary_templates(self):
        face = mock.Mock(embedding=mock.sentinel.embedding,
                         detection_score=.91, normalized_box=(.1, .1, .8, .8))
        rows = (("Madhu", "m", mock.sentinel.madhu),
                ("Himanshu", "h", mock.sentinel.himanshu))
        with mock.patch.object(face_identity, "detect", return_value=(face,)), \
             mock.patch.object(face_identity, "_templates", return_value=rows), \
             mock.patch.object(face_identity, "_score_people",
                               return_value=[(.72, "Himanshu", "h")]):
            result = face_identity.verify_primary("unused.jpg")
        self.assertTrue(result["matched"])
        self.assertEqual(result["similarity"], .72)

    def test_action_dispatch_is_fail_closed_while_gate_waits(self):
        import actions
        gate.require("startup")
        ui = queue.Queue()
        actions.execute_actions(
            '<ACTION type="CALCULATE" expression="2+2" />', {}, ui,
            os.getcwd(), origin="user")
        self.assertIn("lifecycle authentication",
                      actions.execute_actions._last_output.lower())
        self.assertFalse(actions.execute_actions._results[0]["ok"])

    def test_leaving_standby_immediately_requests_a_new_gate(self):
        runtime_power.enter()
        with mock.patch.object(gate, "require_async") as require:
            self.assertTrue(runtime_power.resume())
        require.assert_called_once_with("standby ended")

    def test_live_preview_continues_while_face_inference_runs(self):
        class Stream:
            ok = True

            def __enter__(self): return self
            def __exit__(self, *args): return None
            def read(self):
                return np.zeros((120, 160, 3), dtype=np.uint8)

        gate.require("startup")
        result = {
            "face_found": True, "matched": True, "similarity": .73,
            "box": (.2, .1, .8, .9),
        }
        with mock.patch("camera.live_capture", return_value=Stream()), \
             mock.patch("camera.encode_preview", return_value="frame"), \
             mock.patch("face_identity.verify_primary_image",
                        return_value=result):
            self.assertTrue(gate._live_session(time.monotonic()))

        self.assertTrue(gate.is_unlocked())
        events = []
        while not gate._ui_queue.empty():
            events.append(gate._ui_queue.get_nowait())
        frames = [payload for kind, payload in events
                  if kind == "identity_gate_frame"]
        self.assertTrue(frames)
        self.assertEqual(frames[-1]["box"], result["box"])

    def test_boot_preflight_uses_cache_verifier_not_graph_verifier(self):
        class Stream:
            ok = True
            def __enter__(self): return self
            def __exit__(self, *args): return None
            def read(self):
                return np.zeros((120, 160, 3), dtype=np.uint8)

        gate.configure(os.getcwd(), queue.Queue(), boot_only=True,
                       allow_remote=False)
        gate.require("startup preflight")
        result = {
            "face_found": True, "matched": True, "similarity": .78,
            "box": (.2, .1, .8, .9),
        }
        with mock.patch("camera.live_capture", return_value=Stream()), \
             mock.patch("camera.encode_preview", return_value="frame"), \
             mock.patch("face_identity.verify_primary_image_boot",
                        return_value=result) as boot, \
             mock.patch("face_identity.verify_primary_image",
                        side_effect=AssertionError("graph verifier used")):
            self.assertTrue(gate._live_session(time.monotonic()))
        self.assertGreaterEqual(boot.call_count, 2)


if __name__ == "__main__":
    unittest.main()
