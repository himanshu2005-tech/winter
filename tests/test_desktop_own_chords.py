import unittest

import desktop


class OwnChordsAreNotSynthesisable(unittest.TestCase):
    def test_protocol_chords_are_refused_in_any_order_or_with_extra_modifiers(self):
        for combo in ("ctrl+alt+o", "alt+ctrl+o", "ctrl+alt+l", "ctrl+alt+p", "ctrl+alt+e",
                      "ctrl+alt+shift+o", "CTRL+ALT+O"):
            keys = desktop._normalise_keys(combo)
            self.assertIsNotNone(desktop._protected_combo(keys), combo)

    def test_ordinary_shortcuts_still_pass(self):
        for combo in ("ctrl+c", "ctrl+s", "alt+tab", "ctrl+o", "alt+e", "ctrl+alt+t",
                      "win+d", "ctrl+shift+t"):
            self.assertIsNone(desktop._protected_combo(desktop._normalise_keys(combo)), combo)

    def test_existing_protections_remain(self):
        for combo in ("alt+f4", "ctrl+alt+delete", "ctrl+shift+esc", "ctrl+alt+f12"):
            self.assertIsNotNone(desktop._protected_combo(desktop._normalise_keys(combo)), combo)


if __name__ == "__main__":
    unittest.main()
