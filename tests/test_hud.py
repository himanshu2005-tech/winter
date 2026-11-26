# -*- coding: utf-8 -*-
"""
Tests for the card that stays on his screen.

"I'm studying" should put a list up; "I finished the derivatives one" should
tick it. There were three screen surfaces already and none fit: the blackboard
takes the whole display, Control Center panels need a window opened, and the
notch holds one line.

Nearly all of this tests hud_state rather than hud, on purpose. The failures
that matter here are logical -- ticking the wrong line, losing the list on a
restart, a model re-sending the same list and stacking duplicates -- and none
of them need a screen to catch. The rendering is deliberately thin because it
is the only part that cannot be checked this way.
"""

import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import hud_state


class _Fresh(unittest.TestCase):
    def setUp(self):
        self.base = tempfile.mkdtemp(prefix="hud-test-")
        hud_state.reset()

    def tearDown(self):
        hud_state.reset()
        shutil.rmtree(self.base, ignore_errors=True)

    def study_list(self):
        return hud_state.show(self.base, "Tonight's revision", [
            "Revise derivatives and the chain rule",
            "Finish the DBMS normalisation sheet",
            "Read the RL review slides",
            "Pack the laptop charger",
        ])


class ShowingTests(_Fresh):

    def test_a_list_goes_up(self):
        card = self.study_list()
        self.assertEqual(len(card["items"]), 4)
        self.assertTrue(all(not item["done"] for item in card["items"]))

    def test_plain_strings_and_dicts_both_work(self):
        card = hud_state.show(self.base, "Mixed",
                              ["plain", {"text": "already done", "done": True}])
        self.assertFalse(card["items"][0]["done"])
        self.assertTrue(card["items"][1]["done"])

    def test_re_showing_replaces_rather_than_stacks(self):
        """A model told to "update the list" sends the whole list again.

        Three near-identical study checklists on screen is worse than none.
        """
        self.study_list()
        self.study_list()
        self.assertEqual(len(hud_state.cards()), 1)

    def test_the_screen_does_not_fill_up(self):
        for number in range(8):
            hud_state.show(self.base, f"Card {number}", ["one"])
        self.assertLessEqual(len(hud_state.cards()), hud_state.MAX_CARDS)

    def test_empty_items_are_dropped_not_rendered_blank(self):
        card = hud_state.show(self.base, "Sparse", ["real", "", "   ", None])
        self.assertEqual([item["text"] for item in card["items"]], ["real"])


class TickingTests(_Fresh):
    """He will not say the line back word for word."""

    def test_loose_phrasing_ticks_the_right_item(self):
        self.study_list()
        for said, expected in (
            ("done with the derivatives one", "Revise derivatives and the chain rule"),
            ("finished the normalisation sheet", "Finish the DBMS normalisation sheet"),
            ("packed the charger", "Pack the laptop charger"),
        ):
            receipt = hud_state.check(self.base, said)
            self.assertIn("Ticked", receipt, said)
            self.assertIn(expected, receipt, said)

    def test_the_exact_line_always_wins(self):
        self.study_list()
        receipt = hud_state.check(self.base, "Read the RL review slides")
        self.assertIn("Read the RL review slides", receipt)

    def test_ambiguity_ticks_nothing_and_says_why(self):
        """Guessing is how the wrong thing gets marked done.

        Two faults were found here. Matching card-by-card meant an ambiguous
        newest card fell through to an older one and ticked a unique match
        there. And "the" counted as a match token, so "the review" scored
        "Read the RL review slides" above the actual review items -- a filler
        word choosing the line, confidently.
        """
        hud_state.show(self.base, "Reviews",
                       ["RL review Monday", "SDP review Tuesday"])
        receipt = hud_state.check(self.base, "the review")
        self.assertTrue(receipt.startswith("[Error]"), receipt)
        self.assertIn("RL review Monday", receipt)
        self.assertIn("SDP review Tuesday", receipt)
        for card in hud_state.cards():
            for item in card["items"]:
                self.assertFalse(item["done"], "nothing should have been ticked")

    def test_ambiguity_across_two_cards_still_refuses(self):
        hud_state.show(self.base, "A", ["read the chapter"])
        hud_state.show(self.base, "B", ["read the slides"])
        receipt = hud_state.check(self.base, "read")
        self.assertTrue(receipt.startswith("[Error]"), receipt)

    def test_filler_words_do_not_decide(self):
        hud_state.show(self.base, "L", ["submit the form", "the other thing"])
        # "the" must carry no weight at all.
        self.assertNotIn("the", hud_state._tokens("the and for with that"))

    def test_an_unknown_item_says_so(self):
        self.study_list()
        self.assertIn("Nothing on screen matches",
                      hud_state.check(self.base, "buy milk"))

    def test_ticking_twice_is_not_an_error(self):
        self.study_list()
        hud_state.check(self.base, "packed the charger")
        again = hud_state.check(self.base, "packed the charger")
        self.assertIn("already ticked", again)

    def test_it_can_be_unticked(self):
        self.study_list()
        hud_state.check(self.base, "packed the charger")
        receipt = hud_state.check(self.base, "packed the charger", done=False)
        self.assertIn("Ticked", receipt)
        card = hud_state.cards()[0]
        charger = next(i for i in card["items"] if "charger" in i["text"])
        self.assertFalse(charger["done"])

    def test_ticking_with_nothing_on_screen_says_so(self):
        self.assertIn("Nothing is on screen",
                      hud_state.check(self.base, "anything"))

    def test_the_count_left_is_reported(self):
        # So the reply can say what remains without being asked.
        self.study_list()
        self.assertIn("3 left", hud_state.check(self.base, "derivatives"))


class PersistenceTests(_Fresh):
    """A half-done study list is worth keeping across a restart."""

    def test_it_survives_a_restart(self):
        self.study_list()
        hud_state.check(self.base, "derivatives")
        hud_state.reset()
        self.assertEqual(hud_state.cards(), [])
        self.assertEqual(hud_state.load(self.base), 1)
        card = hud_state.cards()[0]
        self.assertEqual(card["title"], "Tonight's revision")
        done = [item for item in card["items"] if item["done"]]
        self.assertEqual(len(done), 1, "the tick must survive too")

    def test_a_missing_store_is_not_an_error(self):
        self.assertEqual(hud_state.load(self.base), 0)

    def test_a_corrupt_store_is_not_an_error(self):
        path = hud_state.store_path(self.base)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("{ not json")
        self.assertEqual(hud_state.load(self.base), 0)
        # And it recovers rather than staying broken.
        self.study_list()
        self.assertEqual(len(hud_state.cards()), 1)

    def test_temp_files_are_per_process(self):
        import inspect
        self.assertIn("os.getpid()", inspect.getsource(hud_state._save))


class ClearingTests(_Fresh):

    def test_one_card_can_go(self):
        self.study_list()
        hud_state.show(self.base, "Reviews", ["RL review"])
        hud_state.hide(self.base, "reviews")
        self.assertEqual([c["title"] for c in hud_state.cards()],
                         ["Tonight's revision"])

    def test_everything_can_go(self):
        self.study_list()
        hud_state.hide(self.base)
        self.assertEqual(hud_state.cards(), [])

    def test_clearing_something_absent_says_so(self):
        self.assertIn("[Error]", hud_state.hide(self.base, "nothing-here"))


class ModelVisibilityTests(_Fresh):
    """The model has to know what he can already see."""

    def test_the_summary_shows_state(self):
        self.study_list()
        hud_state.check(self.base, "derivatives")
        summary = hud_state.summary()
        self.assertIn("1/4 done", summary)
        self.assertIn("[x] Revise derivatives", summary)
        self.assertIn("[ ] Read the RL review slides", summary)

    def test_it_tells_the_model_not_to_read_the_list_aloud(self):
        # A list he is looking at does not need reciting.
        self.study_list()
        self.assertIn("rather than repeating it", hud_state.summary())

    def test_an_empty_screen_costs_nothing(self):
        self.assertEqual(hud_state.summary(), "")


class WiringTests(unittest.TestCase):

    def test_overlay_height_follows_wrapped_content(self):
        import hud
        self.assertEqual(hud.fitted_height(237, 1080), 237)
        self.assertEqual(hud.fitted_height(2000, 1080), 1024)
        self.assertEqual(hud.fitted_height(12, 1080), 72)

    def test_renderer_measures_widgets_instead_of_guessing_rows(self):
        import inspect
        import hud
        source = inspect.getsource(hud.HudMixin._hud_refresh)
        self.assertIn("winfo_reqheight", source)
        self.assertNotIn("rows * 26", source)

    def test_multiline_show_action_is_parsed_and_displayed(self):
        """The model naturally uses a body for lists; it must not be UNKNOWN."""
        import queue
        import actions
        base = tempfile.mkdtemp(prefix="hud-action-test-")
        hud_state.reset()
        try:
            response = (
                '<ACTION type="SHOW_ON_SCREEN" title="Revision">\n'
                'Derivatives\nDBMS normalisation\n</ACTION>')
            actions.execute_actions(response, {}, queue.Queue(), base,
                                    origin="user")
            self.assertIn("[Success]", actions.execute_actions._last_output)
            self.assertEqual(
                [item["text"] for item in hud_state.cards()[0]["items"]],
                ["Derivatives", "DBMS normalisation"])
        finally:
            hud_state.reset()
            shutil.rmtree(base, ignore_errors=True)

    def test_all_four_contract_layers_agree(self):
        """Dispatcher, registry, catalogue, schema.

        Any one missing and the action is invisible or refused -- the failure
        mode behind SET_ALARM's recurrence, QUIET_FOR's schema key and the
        RUN_PYTHON gate.
        """
        import io as _io
        import action_schema
        import agent
        import autonomy
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with _io.open(os.path.join(root, "actions.py"), encoding="utf-8") as handle:
            dispatch = handle.read()
        for name in ("SHOW_ON_SCREEN", "TICK_ON_SCREEN", "CLEAR_SCREEN"):
            self.assertIn(name, autonomy.KNOWN_ACTIONS, name)
            self.assertIn(name, action_schema.ACTION_SPECS, name)
            self.assertTrue(any(name in text for text in
                                agent.ACTION_CATALOGS.values()), name)
            self.assertIn(name, dispatch, name)

    def test_the_registry_surfaces_it_when_he_says_he_is_studying(self):
        """A capability the ranking never returns may as well not exist."""
        import action_registry
        import agent
        action_registry.configure(agent.ACTION_CATALOGS)
        allowed = {"SYSTEM", "MACHINE", "CONTROL_CENTER", "SELF",
                   "WORKSPACE", "MEMORY", "DISCOVERY"}
        found = action_registry.search(".", "i am studying tonight", allowed, 8)
        self.assertIn("SHOW_ON_SCREEN", [entry["name"] for entry in found])

    def test_the_registry_surfaces_ticking_when_he_reports_progress(self):
        import action_registry
        import agent
        action_registry.configure(agent.ACTION_CATALOGS)
        allowed = {"SYSTEM", "MACHINE", "CONTROL_CENTER", "SELF",
                   "WORKSPACE", "MEMORY", "DISCOVERY"}
        found = action_registry.search(
            ".", "i finished the derivatives one", allowed, 8)
        self.assertIn("TICK_ON_SCREEN", [entry["name"] for entry in found][:4])

    def test_the_overlay_never_takes_focus(self):
        """It appears while he is typing. Stealing the caret would end it."""
        import io as _io
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with _io.open(os.path.join(root, "hud.py"), encoding="utf-8") as handle:
            source = handle.read()
        self.assertIn("overrideredirect(True)", source)
        self.assertIn('"-topmost", True', source)
        self.assertNotIn("focus_force", source)
        self.assertNotIn("grab_set", source)

    def test_the_overlay_holds_no_state_of_its_own(self):
        # State lives in hud_state so it can be tested without a screen.
        import io as _io
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with _io.open(os.path.join(root, "hud.py"), encoding="utf-8") as handle:
            source = handle.read()
        self.assertIn("hud_state.cards()", source)

    def test_it_is_registered_so_vanta_does_not_see_its_own_overlay(self):
        import io as _io
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with _io.open(os.path.join(root, "hud.py"), encoding="utf-8") as handle:
            source = handle.read()
        self.assertIn("register_winter_hwnd", source)


if __name__ == "__main__":
    unittest.main()
