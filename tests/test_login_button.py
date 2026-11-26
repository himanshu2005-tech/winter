# -*- coding: utf-8 -*-
"""The college portal's login is one button, and VANTA could not see it.

my.amrita.edu asks for one click. VANTA opened it, clicked, waited, and said:

    "Sir, the My Amrita login page is open, but its sign-in control isn't
    accessible to the browser. Please sign in manually in the open window."

-- about a session the cookie import had already signed in. Three separate
faults in one snapshot, measured against the real page:

    BUTTON#submitBtn   "Sign In"   0 x 0     <- offered as e1
    SPAN.font-lg       "LOGIN"     142 x 51  <- never collected
    A.btn-circle       ""          50 x 50   <- offered with no name

*The hidden button was offered.* `#submitBtn` is a zero-size submit driven by
script. Playwright resolved it, waited the full thirty seconds for it to
become actionable, and failed -- which is what "isn't accessible to the
browser" actually meant.

*The real control was invisible to the selector.* The only element carrying
the word LOGIN is a <span onclick="$('#submitBtn').click()">, and the selector
listed tag names and ARIA roles but not [onclick]. The page that is "just a
button you click" had no button in the snapshot.

*The icon had no name.* The gold arrow is an <a> with no text, so it came back
as "" and the model had nothing to go on.

And a fourth, in the click itself: a flat 250ms wait afterwards, while the
sign-in starts an SSO redirect chain. The snapshot landed mid-redirect.
"""

import os
import ast
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import actions
import browser

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def snapshot_script():
    """The collector, as the worker builds it."""
    with open(os.path.join(ROOT, "browser.py"), encoding="utf-8") as handle:
        source = handle.read()
    def text(node):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            return text(node.left) + text(node.right)
        if isinstance(node, ast.Name) and node.id == 'STABLE_REFERENCES_JS':
            return browser.STABLE_REFERENCES_JS
        raise AssertionError('Unrecognised snapshot script construction')

    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Assign) and any(
                isinstance(target, ast.Name) and target.id == 'script' for target in node.targets):
            value = text(node.value)
            if value.startswith('(els, params)'):
                return value
    raise AssertionError('Snapshot collector not found')


def snapshot_selector():
    with open(os.path.join(ROOT, "browser.py"), encoding="utf-8") as handle:
        source = handle.read()
    start = source.index("selector = (")
    return source[start:source.index(")", start)]


class TheSnapshotOffersOnlyRealControls(unittest.TestCase):
    def test_zero_size_elements_are_filtered_out(self):
        """Offering one is offering something that can never be clicked."""
        script = snapshot_script()
        self.assertIn("getBoundingClientRect", script)
        self.assertIn("r.width > 0 && r.height > 0", script)

    def test_the_filter_runs_before_the_cap(self):
        """Otherwise 180 slots can fill with hidden elements."""
        script = snapshot_script()
        self.assertLess(script.index("filter"), script.index("slice(0, 180)"))


class TheSelectorSeesClickHandlers(unittest.TestCase):
    def test_onclick_elements_are_collected(self):
        self.assertIn("[onclick]", snapshot_selector())

    def test_the_semantic_tags_are_still_collected(self):
        selector = snapshot_selector()
        for part in ("a,", "button", "input", "textarea", "select",
                     "[role=button]", "[role=textbox]"):
            with self.subTest(part=part):
                self.assertIn(part, selector)


class AnIconStillGetsAName(unittest.TestCase):
    def test_the_name_falls_back_through_the_usual_hiding_places(self):
        script = snapshot_script()
        for source in ("alt", "title", "onclick"):
            with self.subTest(source=source):
                self.assertIn(source, script)

    def test_a_handler_naming_another_element_borrows_its_text(self):
        """The gold arrow's onclick is $('#submitBtn').click(), and
        #submitBtn says "Sign In"."""
        script = snapshot_script()
        self.assertIn("getElementById", script)

    def test_the_fallback_is_last(self):
        """A real label must always win over a guess."""
        script = snapshot_script()
        name_line = [line for line in script.split("\n")
                     if "const name =" in line][0]
        self.assertLess(name_line.index("aria-label"),
                        name_line.index("fallback"))


class AClickWaitsForWhereItGoes(unittest.TestCase):
    def test_the_settle_helper_exists(self):
        with open(os.path.join(ROOT, "browser.py"), encoding="utf-8") as handle:
            source = handle.read()
        self.assertIn("def settle_after(", source)

    def test_a_click_no_longer_just_waits_a_flat_moment(self):
        with open(os.path.join(ROOT, "browser.py"), encoding="utf-8") as handle:
            source = handle.read()
        click = source[source.index('if operation == "click":'):]
        click = click[:click.index('elif operation in {"select"')]
        self.assertIn("settle_after", click)
        self.assertIn("click_control(page, locator)", click)
        tree = ast.parse(source)
        helper = next(node for node in ast.walk(tree)
                      if isinstance(node, ast.FunctionDef) and node.name == 'click_control')
        self.assertIn('CLICK_DISPATCH_TIMEOUT_MS', ast.get_source_segment(source, helper))
        self.assertNotIn("page.wait_for_timeout(250)", click)

    def test_it_waits_longer_for_things_that_navigate(self):
        self.assertGreater(browser.NAVIGATION_SETTLE_MS,
                           browser.SHORT_SETTLE_MS)

    def test_script_driven_visual_controls_are_treated_as_navigation(self):
        """The portal's LOGIN is a span whose onclick drives a hidden submit."""
        with open(os.path.join(ROOT, "browser.py"), encoding="utf-8") as handle:
            source = handle.read()
        settle = source[source.index("def settle_after("):]
        settle = settle[:settle.index("def receipt(")]
        navigation_hint = source[source.index("def interaction_may_navigate("):]
        navigation_hint = navigation_hint[:navigation_hint.index("def settle_after(")]
        self.assertIn('get_attribute("onclick")', navigation_hint)
        self.assertIn("or handler or href", navigation_hint)

    def test_navigation_intent_is_read_before_the_click_detaches_the_node(self):
        with open(os.path.join(ROOT, "browser.py"), encoding="utf-8") as handle:
            source = handle.read()
        click = source[source.index('elif operation in {"click", "type", "press"}'):]
        click = click[:click.index('elif operation in {"select"')]
        self.assertLess(click.index("interaction_may_navigate(locator)"),
                        click.index("click_control(page, locator)"))

    def test_an_in_page_click_stays_cheap(self):
        """Every click must not pay for a redirect that is never coming."""
        self.assertLessEqual(browser.SHORT_SETTLE_MS, 500)

    def test_the_whole_redirect_chain_is_allowed_to_land(self):
        """An SSO handshake is several hops; stopping at the first means
        reading a page that is about to be replaced."""
        with open(os.path.join(ROOT, "browser.py"), encoding="utf-8") as handle:
            source = handle.read()
        settle = source[source.index("def settle_after("):]
        settle = settle[:settle.index("def receipt(")]
        self.assertIn("networkidle", settle)
        self.assertIn("domcontentloaded", settle)

    def test_the_receipt_says_the_page_moved(self):
        with open(os.path.join(ROOT, "browser.py"), encoding="utf-8") as handle:
            source = handle.read()
        self.assertIn("came_from", source)

    def test_the_call_timeout_outlasts_the_settling(self):
        """Otherwise the caller gives up while the worker is still waiting."""
        allowance = browser.call_timeout("click")
        longest = (browser.NAVIGATION_SETTLE_MS + 2 * browser.LOAD_SETTLE_MS)
        self.assertGreater(allowance, longest / 1000.0)


class LookingDoesNotSpendTheBudget(unittest.TestCase):
    def test_the_observation_actions_are_declared(self):
        self.assertIn("BROWSER_SNAPSHOT", actions.OBSERVATION_ONLY_ACTIONS)
        self.assertIn("BROWSER_READ", actions.OBSERVATION_ONLY_ACTIONS)

    def test_nothing_that_changes_anything_is_in_the_set(self):
        for name in ("BROWSER_CLICK", "BROWSER_FILL_FORM", "BROWSER_SUBMIT",
                     "BROWSER_GOTO", "BROWSER_TYPE", "BROWSER_UPLOAD"):
            with self.subTest(action=name):
                self.assertNotIn(name, actions.OBSERVATION_ONLY_ACTIONS)

    def test_the_loop_gives_the_step_back(self):
        with open(os.path.join(ROOT, "agent.py"), encoding="utf-8") as handle:
            source = handle.read()
        self.assertIn("observation_extensions", source)
        self.assertIn("OBSERVATION_ONLY_ACTIONS", source)

    def test_it_is_bounded(self):
        """A turn that only ever looks is a turn in a loop."""
        with open(os.path.join(ROOT, "agent.py"), encoding="utf-8") as handle:
            source = handle.read()
        self.assertIn("max_observation_extensions", source)
        self.assertRegex(source, r"max_observation_extensions = \d+")

    def test_only_a_step_that_did_nothing_else_counts(self):
        """A reply that snapshots and then clicks has done work."""
        with open(os.path.join(ROOT, "agent.py"), encoding="utf-8") as handle:
            source = handle.read()
        start = source.index("if observation_extensions < max_observation")
        block = source[start:start + 900]
        self.assertIn("<= OBSERVATION_ONLY_ACTIONS", block,
                      "it should be a subset test, so a mixed step still "
                      "spends its budget")

    def test_the_subset_rule_behaves(self):
        """The decision itself, run against the set."""
        observation = actions.OBSERVATION_ONLY_ACTIONS
        pattern = (r'<\s*ACTION\s+[^>]*type\s*=\s*["\']([A-Za-z0-9_]+)["\']')

        def refunded(reply):
            proposed = {n.upper() for n in re.findall(pattern, reply,
                                                      re.IGNORECASE)}
            return bool(proposed) and proposed <= observation

        self.assertTrue(refunded('<ACTION type="BROWSER_SNAPSHOT" />'))
        self.assertFalse(refunded(
            '<ACTION type="BROWSER_SNAPSHOT" /><ACTION type="BROWSER_CLICK" '
            'ref="e1" />'))
        self.assertFalse(refunded('<ACTION type="BROWSER_CLICK" ref="e1" />'))
        self.assertFalse(refunded("<SAY>nothing to do</SAY>"))


if __name__ == "__main__":
    unittest.main()
