import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import runtime_power


class RuntimePowerTests(unittest.TestCase):
    def tearDown(self):
        runtime_power.resume()

    def test_standby_pauses_background_work(self):
        self.assertTrue(runtime_power.enter())
        self.assertTrue(runtime_power.is_standby())
        self.assertFalse(runtime_power.background_allowed())
        self.assertTrue(runtime_power.resume())

    def test_lifecycle_does_not_parse_natural_language(self):
        self.assertFalse(hasattr(runtime_power, "parse_command"))
        self.assertFalse(hasattr(runtime_power, "command_duration"))

    def test_only_the_current_timed_session_can_expire(self):
        runtime_power.enter(0.01)
        token = runtime_power.generation()
        time.sleep(0.02)
        self.assertTrue(runtime_power.expire_if_due(token))
        self.assertFalse(runtime_power.is_standby())
        self.assertFalse(runtime_power.expire_if_due(token))

    def test_transitions_are_idempotent(self):
        self.assertTrue(runtime_power.enter())
        self.assertFalse(runtime_power.enter())
        self.assertTrue(runtime_power.resume())
        self.assertFalse(runtime_power.resume())


if __name__ == "__main__":
    unittest.main()
