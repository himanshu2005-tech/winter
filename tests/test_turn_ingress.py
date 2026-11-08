import unittest

import turn_ingress


class TurnIngressTests(unittest.TestCase):
    def setUp(self):
        turn_ingress.reset()

    def test_simultaneous_duplicate_is_rejected(self):
        self.assertTrue(turn_ingress.accept("I am watching a film", "voice", 10))
        self.assertFalse(turn_ingress.accept(" I AM  watching a film ", "voice", 10.2))

    def test_repetition_later_is_a_real_turn(self):
        self.assertTrue(turn_ingress.accept("hello", "voice", 10))
        self.assertTrue(turn_ingress.accept("hello", "voice", 12))

    def test_sources_do_not_suppress_each_other(self):
        self.assertTrue(turn_ingress.accept("stop", "voice", 10))
        self.assertTrue(turn_ingress.accept("stop", "text", 10.1))


if __name__ == "__main__":
    unittest.main()
