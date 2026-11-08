# -*- coding: utf-8 -*-
"""
Tests for what a browser receipt claims.

The bug these exist for: VANTA was asked to log in to a placements portal. It
guessed the field refs instead of snapshotting the page first, so every field
failed -- and the receipt still came back {"ok": true}, because ok was
hardcoded. The model read the flag rather than the detail and announced a
successful login, then described job listings that did not exist.

A receipt is the only evidence an action turn has. If it lies about the
outcome, everything downstream is invention.
"""

import os
import sys
import tempfile
import unittest


class NonIntrusiveBrowserSourceTests(unittest.TestCase):
    def test_managed_browser_starts_minimized(self):
        import inspect
        import browser
        source = inspect.getsource(browser._BrowserWorker._run)
        self.assertIn("--start-minimized", source)

    def test_close_receipt_identifies_owned_media(self):
        import inspect
        import browser
        source = inspect.getsource(browser._BrowserWorker._run)
        self.assertIn("media_stopped", source)
        self.assertIn("closed_url", source)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class ReceiptHonestyTests(unittest.TestCase):
    """Source-level: these run without launching a browser."""

    @classmethod
    def setUpClass(cls):
        with open(os.path.join(ROOT, "browser.py"), encoding="utf-8") as handle:
            cls.source = handle.read()

    def test_fill_form_reports_failure_in_the_flag(self):
        self.assertIn("ok=bool(filled) and not failed", self.source,
                      "fill_form is claiming success again")

    def test_submit_ties_ok_to_an_observed_transition(self):
        # Modern SPAs often submit without changing URL. A content/state
        # fingerprint is still observed evidence; merely dispatching a click
        # is not.
        self.assertIn("ok=changed", self.source,
                      "submit is claiming success without evidence")
        self.assertIn("page_signature(page) != before_signature", self.source)

    def test_a_missing_ref_says_what_refs_exist(self):
        # Three call sites: click/type/press, submit, and fill_form's
        # per-field failures.
        self.assertGreaterEqual(self.source.count("ref_state(page)"), 3)

    def test_ref_state_names_the_snapshot_as_the_source_of_refs(self):
        self.assertIn("refs are created by BROWSER_SNAPSHOT", self.source)

    def test_empty_keys_are_dropped(self):
        self.assertIn("if v is not None", self.source)

    def test_ok_is_not_hardcoded_past_the_default(self):
        # The default stays True for operations that cannot fail silently;
        # what matters is that callers can override it.
        self.assertIn('data.update(extra)', self.source)


class LiveBrowserTests(unittest.TestCase):
    """The real thing, against a public form. Skipped without Playwright."""

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
            tempfile.gettempdir(), "vanta_test_profile_receipts")

        import browser
        cls.browser = browser
        try:
            cls.browser.start(visible=False)
            result = cls.browser.goto("https://httpbin.org/forms/post")
        except Exception as exc:
            raise unittest.SkipTest(f"browser unavailable: {exc}")
        if "[Browser Error]" in result:
            raise unittest.SkipTest("could not reach the test page")

    @classmethod
    def tearDownClass(cls):
        try:
            cls.browser.close(all_tabs=True)
        except Exception:
            pass

    def receipt(self, raw):
        import json
        self.assertIn("] ", raw, f"not a receipt: {raw[:120]}")
        return json.loads(raw.split("] ", 1)[1])

    def test_guessing_field_names_is_reported_as_failure(self):
        # A fresh navigation drops the injected ref attributes, which is the
        # state the portal login was actually in: nothing snapshotted yet.
        self.browser.goto("https://httpbin.org/forms/post")
        # Exactly what the model sent to the placements portal.
        data = self.receipt(self.browser.fill_form({
            "username": {"kind": "fill", "value": "someone"},
            "password": {"kind": "fill", "value": "secret"},
        }))
        self.assertFalse(data["ok"])
        self.assertEqual(data["filled"], [])
        self.assertEqual(len(data["failed"]), 2)
        self.assertIn("BROWSER_SNAPSHOT", data["refs"])

    def test_a_snapshot_gives_refs_that_work(self):
        import json
        snap = json.loads(self.browser.snapshot().split("] ", 1)[1])
        inputs = [e for e in snap["elements"]
                  if e.get("tag") == "input" and e.get("type") == "text"]
        self.assertTrue(inputs, "the test page stopped having a text input")
        data = self.receipt(self.browser.fill_form(
            {inputs[0]["ref"]: {"kind": "fill", "value": "Himanshu"}}))
        self.assertTrue(data["ok"])
        self.assertEqual(data["filled"], [inputs[0]["ref"]])
        self.assertNotIn("refs", data, "null keys are back in the receipt")


if __name__ == "__main__":
    unittest.main()
