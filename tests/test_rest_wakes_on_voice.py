import inspect
import unittest
from unittest import mock

import screen_rest


class VoiceEndsTheDarkScreen(unittest.TestCase):
    def setUp(self):
        screen_rest._resting = True
        screen_rest._suspended_until = 0.0

    def tearDown(self):
        screen_rest._resting = False
        screen_rest._suspended_until = 0.0

    def test_speaking_brings_the_screen_back_and_holds_off_the_blackout(self):
        import agent
        queue = mock.Mock()
        with mock.patch.object(agent, "ui_queue", queue):
            agent._wake_the_screen("wake word")
        self.assertFalse(screen_rest.is_resting())
        self.assertTrue(screen_rest.suspended())
        queue.put.assert_any_call(("rest_end", "wake word"))

    def test_nothing_happens_when_the_screen_was_not_resting(self):
        import agent
        screen_rest._resting = False
        queue = mock.Mock()
        with mock.patch.object(agent, "ui_queue", queue):
            agent._wake_the_screen("a request")
        queue.put.assert_not_called()
        self.assertFalse(screen_rest.suspended())

    def test_both_the_name_and_any_request_wake_it(self):
        import agent
        self.assertIn("_wake_the_screen(", inspect.getsource(agent.wakeword_worker))
        self.assertIn("_wake_the_screen(", inspect.getsource(agent.run_agent_loop))


if __name__ == "__main__":
    unittest.main()
