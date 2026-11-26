# -*- coding: utf-8 -*-
"""
Tests for putting text into a field on a real page.

The bug these exist for: VANTA could not type into anything. BROWSER_FILL_FORM
accepted only refs from a snapshot -- e3, e7 -- but the model reliably writes
fields="username=...|password=..." because that is what the fields are called.
Every key missed, every field failed, and the turn ended with a confident claim
that the login had worked.

Insisting the model change was not working: it had been told "Always snapshot
before choosing a ref" in the catalogue for a long time and did not. So the
action now accepts what it is actually given -- a ref, a name, an id, a label,
a placeholder, or the word for the field's type -- and refs still win when
present, so a snapshot-driven flow is unchanged.

These drive real sites, so they skip when there is no network.
"""

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FORM = "https://httpbin.org/forms/post"


class ControlValueFormattingTests(unittest.TestCase):
    """The browser, not the model, owns each field's declared format."""

    def normalize(self, value, **metadata):
        import browser
        return browser.normalize_control_value(value, **metadata)

    def test_am_pm_becomes_declared_twenty_four_hour_time(self):
        self.assertEqual(
            self.normalize("5AM", placeholder="Pass From Time(24 hrs clock)"),
            "5:00")
        self.assertEqual(
            self.normalize("6PM", placeholder="Pass To Time(24 hrs clock)"),
            "18:00")

    def test_native_time_uses_html_required_padding(self):
        self.assertEqual(self.normalize("5 am", control_type="time"), "05:00")
        self.assertEqual(self.normalize("18", control_type="time"), "18:00")

    def test_invalid_time_is_refused_instead_of_mangled(self):
        with self.assertRaises(ValueError):
            self.normalize("26:70", placeholder="Time (24 hrs clock)")

    def test_date_follows_the_control_contract(self):
        self.assertEqual(
            self.normalize("2026-10-19", placeholder="dd / mm / yyyy"),
            "19/10/2026")
        self.assertEqual(
            self.normalize("19/10/2026", control_type="date"),
            "2026-10-19")

    def test_unrelated_text_is_untouched(self):
        self.assertEqual(self.normalize("Meet at 6PM", name="reason"),
                         "Meet at 6PM")


class ResolverSourceTests(unittest.TestCase):
    """Source-level; these run without a browser."""

    @classmethod
    def setUpClass(cls):
        with open(os.path.join(ROOT, "browser.py"), encoding="utf-8") as handle:
            cls.source = handle.read()

    def test_a_target_can_be_found_without_a_ref(self):
        # Signature deliberately not pinned in full -- it has grown a
        # settle_ms since, and a test that breaks on a new keyword argument
        # is testing the spelling rather than the behaviour.
        self.assertIn("def find_target(page, key, editable=True", self.source)

    def test_every_entry_point_uses_it(self):
        # click/type/press, fill_form, and submit.
        self.assertGreaterEqual(self.source.count("find_target(page"), 4)

    def test_a_typing_target_must_be_typeable(self):
        # get_by_label("search") returned YouTube's Search button, and fill()
        # failed with "Element is not an <input>".
        self.assertIn("def is_typeable(item)", self.source)
        self.assertIn("editable=editable", self.source)

    def test_radio_groups_are_handled_separately(self):
        # select_option raises on a radio; a whole form failed over it.
        self.assertIn('control == "radio"', self.source)

    def test_refs_are_still_preferred(self):
        index_ref = self.source.find('exact_ref = page.locator')
        index_name = self.source.find('[name="{safe}" i]')
        self.assertNotEqual(index_ref, -1)
        self.assertLess(index_ref, index_name,
                        "a snapshot ref must be tried before a guessed name")


class LiveFieldTests(unittest.TestCase):
    """The real thing. Skipped without Playwright or a network."""

    @classmethod
    def setUpClass(cls):
        if os.getenv("WINTER_SKIP_BROWSER_TESTS"):
            raise unittest.SkipTest("browser tests disabled")
        try:
            import playwright  # noqa: F401
        except ImportError:
            raise unittest.SkipTest("playwright is not installed")

        # Its own Chromium profile, for two reasons. Chromium locks a
        # persistent profile, so suites running back to back would
        # intermittently fail to acquire the lock -- about one run in fifteen.
        # And the real profile holds Himanshu's live sessions; a test should
        # not be writing cookies next to his sign-ins.
        os.environ["WINTER_BROWSER_PROFILE"] = os.path.join(
            tempfile.gettempdir(), "vanta_test_profile_fields")

        import browser
        cls.browser = browser
        try:
            cls.browser.start(visible=False)
            if "[Browser Error]" in cls.browser.goto(FORM):
                raise unittest.SkipTest("could not reach the test page")
        except unittest.SkipTest:
            raise
        except Exception as exc:
            raise unittest.SkipTest(f"browser unavailable: {exc}")

    @classmethod
    def tearDownClass(cls):
        try:
            cls.browser.close(all_tabs=True)
        except Exception:
            pass

    def receipt(self, raw):
        self.assertIn("] ", raw, f"not a receipt: {raw[:160]}")
        try:
            return json.loads(raw.split("] ", 1)[1])
        except ValueError:
            self.fail(f"not JSON: {raw[:160]}")

    def values_on_page(self):
        snap = json.loads(self.browser.snapshot().split("] ", 1)[1])
        return {e.get("value", "") for e in snap["elements"] if e.get("value")}

    def test_filling_by_name_without_snapshotting_first(self):
        self.browser.goto(FORM)
        data = self.receipt(self.browser.fill_form({
            "custname": {"kind": "fill", "value": "Himanshu"},
            "custtel": {"kind": "fill", "value": "9999999999"},
        }))
        self.assertTrue(data["ok"], data.get("failed"))
        self.assertEqual(data["failed"], [])
        present = self.values_on_page()
        self.assertIn("Himanshu", present)
        self.assertIn("9999999999", present)

    def test_filling_by_the_type_word(self):
        # "email" is not the name attribute; input[type=email] is the field.
        self.browser.goto(FORM)
        data = self.receipt(self.browser.fill_form(
            {"email": {"kind": "fill", "value": "himanshu@example.com"}}))
        self.assertTrue(data["ok"], data.get("failed"))
        self.assertIn("himanshu@example.com", self.values_on_page())

    def test_filling_by_the_visible_label(self):
        self.browser.goto(FORM)
        data = self.receipt(self.browser.fill_form(
            {"Customer name": {"kind": "fill", "value": "Himanshu Rao"}}))
        self.assertTrue(data["ok"], data.get("failed"))
        self.assertIn("Himanshu Rao", self.values_on_page())

    def test_a_textarea_is_reachable_too(self):
        self.browser.goto(FORM)
        data = self.receipt(self.browser.fill_form(
            {"comments": {"kind": "fill", "value": "deliver after 6pm"}}))
        self.assertTrue(data["ok"], data.get("failed"))
        self.assertIn("deliver after 6pm", self.values_on_page())

    def test_a_radio_group_is_chosen_by_its_option(self):
        self.browser.goto(FORM)
        data = self.receipt(self.browser.fill_form(
            {"size": {"kind": "select", "value": "medium"}}))
        self.assertTrue(data["ok"], data.get("failed"))

    def test_a_checkbox_is_reachable_by_name(self):
        self.browser.goto(FORM)
        data = self.receipt(self.browser.fill_form(
            {"topping": {"kind": "check", "value": "true"}}))
        self.assertTrue(data["ok"], data.get("failed"))

    def test_a_snapshot_ref_still_wins(self):
        self.browser.goto(FORM)
        snap = json.loads(self.browser.snapshot().split("] ", 1)[1])
        first = next(e for e in snap["elements"]
                     if e.get("tag") == "input" and e.get("type") == "text")
        data = self.receipt(self.browser.fill_form(
            {first["ref"]: {"kind": "fill", "value": "By ref"}}))
        self.assertTrue(data["ok"])
        self.assertEqual(data["filled"], [first["ref"]])

    def test_a_field_that_is_not_there_still_fails(self):
        # The resolver must not be so loose it types into anything at hand.
        self.browser.goto(FORM)
        data = self.receipt(self.browser.fill_form(
            {"not_a_real_field_xyz": {"kind": "fill", "value": "x"}}))
        self.assertFalse(data["ok"])
        self.assertEqual(data["filled"], [])


class LiveLoginTests(unittest.TestCase):
    """A username/password form, end to end. This is the original failure."""

    LOGIN = "https://practicetestautomation.com/practice-test-login/"

    @classmethod
    def setUpClass(cls):
        if os.getenv("WINTER_SKIP_BROWSER_TESTS"):
            raise unittest.SkipTest("browser tests disabled")
        try:
            import playwright  # noqa: F401
        except ImportError:
            raise unittest.SkipTest("playwright is not installed")
        import browser
        cls.browser = browser
        try:
            cls.browser.start(visible=False)
            if "[Browser Error]" in cls.browser.goto(cls.LOGIN):
                raise unittest.SkipTest("could not reach the login page")
        except unittest.SkipTest:
            raise
        except Exception as exc:
            raise unittest.SkipTest(f"browser unavailable: {exc}")

    @classmethod
    def tearDownClass(cls):
        try:
            cls.browser.close(all_tabs=True)
        except Exception:
            pass

    def test_logging_in_the_way_the_model_writes_it(self):
        import json as _json
        self.browser.goto(self.LOGIN)
        raw = self.browser.fill_form({
            "username": {"kind": "fill", "value": "student"},
            "password": {"kind": "fill", "value": "Password123"},
        })
        data = _json.loads(raw.split("] ", 1)[1])
        self.assertTrue(data["ok"], data.get("failed"))
        self.assertEqual(sorted(data["filled"]), ["password", "username"])

        submitted = _json.loads(
            self.browser.submit("Submit").split("] ", 1)[1])
        self.assertTrue(submitted["navigated"],
                        "the form did not go through")
        self.assertIn("logged-in-successfully", submitted["url"])


if __name__ == "__main__":
    unittest.main()
