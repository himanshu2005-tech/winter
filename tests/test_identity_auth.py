# -*- coding: utf-8 -*-
import inspect
import queue
import threading
import time
import unittest
from unittest import mock

import identity_auth as auth

PASSWORD = "correct horse battery"


class Challenge(unittest.TestCase):
    def setUp(self):
        auth.reset()
        self.ui = queue.Queue()
        self.ran = threading.Event()
        patcher = mock.patch.dict("os.environ", {"AUTH_PASSWORD": PASSWORD})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(auth.reset)

    def start(self, **kwargs):
        started, note = auth.request("test", self.ran.set, self.ui, **kwargs)
        self.assertTrue(started, note)
        event = self.ui.get_nowait()
        self.assertEqual(event[0], "auth_prompt")
        return event[1]["id"]

    def test_the_right_password_runs_the_callback_once(self):
        token = self.start()
        self.assertEqual(auth.submit(token, PASSWORD, self.ui), "ok")
        self.assertTrue(self.ran.wait(2))
        self.assertEqual(auth.submit(token, PASSWORD, self.ui), "none")   # single use

    def test_a_wrong_password_runs_nothing_and_asks_again(self):
        token = self.start()
        self.assertEqual(auth.submit(token, "guess", self.ui), "wrong")
        self.assertFalse(self.ran.wait(0.1))
        again = self.ui.get_nowait()
        self.assertEqual(again[0], "auth_prompt")
        self.assertEqual(again[1]["id"], token)

    def test_three_wrong_tries_lock_everything_for_a_while(self):
        token = self.start()
        for expected in ("wrong", "wrong", "locked"):
            self.assertEqual(auth.submit(token, "guess", self.ui), expected)
        started, note = auth.request("test", self.ran.set, self.ui)
        self.assertFalse(started)
        self.assertIn("Locked", note)
        self.assertGreater(auth.locked_for(), 250)

    def test_the_lock_holds_even_for_the_right_password(self):
        token = self.start()
        for _ in range(3):
            auth.submit(token, "guess", self.ui)
        self.assertFalse(auth.request("test", self.ran.set, self.ui)[0])
        self.assertFalse(self.ran.is_set())

    def test_a_stale_or_foreign_challenge_is_ignored(self):
        token = self.start()
        self.assertEqual(auth.submit("not-the-token", PASSWORD, self.ui), "none")
        self.assertFalse(self.ran.wait(0.1))
        auth.pending()["expires"] = 0           # expire it
        with auth._lock:
            auth._pending["expires"] = time.time() - 1
        self.assertEqual(auth.submit(token, PASSWORD, self.ui), "expired")
        self.assertFalse(self.ran.wait(0.1))

    def test_cancel_clears_it_and_calls_the_cancel_hook(self):
        cancelled = threading.Event()
        auth.request("test", self.ran.set, self.ui, on_cancel=cancelled.set)
        token = self.ui.get_nowait()[1]["id"]
        self.assertTrue(auth.cancel(token))
        self.assertTrue(cancelled.is_set())
        self.assertEqual(auth.submit(token, PASSWORD, self.ui), "none")

    def test_without_a_password_nothing_can_be_unlocked_with_one(self):
        with mock.patch.dict("os.environ", {"AUTH_PASSWORD": ""}):
            self.assertFalse(auth.configured())
            started, note = auth.request("test", self.ran.set, self.ui)
        self.assertFalse(started)
        self.assertIn("AUTH_PASSWORD", note)
        self.assertTrue(self.ui.empty())

    def test_an_empty_answer_is_never_correct(self):
        with mock.patch.dict("os.environ", {"AUTH_PASSWORD": ""}):
            self.assertFalse(auth._matches(""))
            self.assertFalse(auth._matches(None))

    def test_the_password_said_in_chat_is_recognised_so_it_can_be_dropped(self):
        self.assertTrue(auth.is_password(PASSWORD))
        self.assertTrue(auth.is_password("  Correct Horse Battery! "))
        self.assertFalse(auth.is_password("what is the weather"))
        self.assertFalse(auth.is_password("the password is something else"))
        with mock.patch.dict("os.environ", {"AUTH_PASSWORD": "ab"}):     # too short to guess at
            self.assertFalse(auth.is_password("ab"))


class NeverLeaks(unittest.TestCase):
    def test_the_module_never_prints_or_logs_anything(self):
        source = inspect.getsource(auth)
        self.assertNotIn("print(", source)
        self.assertNotIn("logging", source)

    def test_the_password_is_not_put_in_the_system_prompt(self):
        import agent
        with mock.patch.dict("os.environ", {"AUTH_PASSWORD": "sentinel-do-not-leak"}):
            self.assertNotIn("sentinel-do-not-leak", agent.get_system_prompt())

    def test_the_secret_is_its_own_message_not_a_text_turn(self):
        import ui
        submit = inspect.getsource(ui.WinterUI._on_input_submit)
        self.assertIn('"auth_secret"', submit)
        self.assertLess(submit.index('"auth_secret"'), submit.index('("text", text)'))
        self.assertIn('show="', inspect.getsource(ui.WinterUI._begin_auth_prompt))

    def test_the_agent_handles_the_secret_before_any_normal_input(self):
        import agent
        source = inspect.getsource(agent.poll_input_queue)
        self.assertLess(source.index('mode == "auth_secret"'),
                        source.index("handle_live_control"))
        self.assertLess(source.index("is_password(text)"), source.index("handle_live_control"))
        handler = inspect.getsource(agent._handle_auth_secret)
        for line in handler.splitlines():
            if "print(" in line:
                self.assertNotIn("secret", line)

    def test_a_password_in_chat_never_reaches_the_model(self):
        import agent
        with mock.patch.dict("os.environ", {"AUTH_PASSWORD": PASSWORD}), \
             mock.patch.object(agent, "ui_queue", mock.Mock()) as ui_queue, \
             mock.patch.object(agent, "run_agent_loop") as loop, \
             mock.patch.object(agent, "input_queue", queue.Queue()) as incoming:
            incoming.put(("text", PASSWORD))
            incoming.put(None)                    # ends the loop below via the error path
            worker = threading.Thread(target=self._drain, args=(agent, incoming), daemon=True)
            worker.start()
            worker.join(2)
        loop.assert_not_called()
        said = " ".join(str(call.args) for call in ui_queue.put.call_args_list)
        self.assertIn("dropped", said)
        self.assertNotIn(PASSWORD, said)

    @staticmethod
    def _drain(agent, incoming):
        try:
            agent.poll_input_queue()
        except Exception:
            pass


if __name__ == "__main__":
    unittest.main()
