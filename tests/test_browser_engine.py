# -*- coding: utf-8 -*-
"""Choosing the browser engine, without breaking the one that works.

Playwright's Firefox and the installed stock Firefox are separate backends.
The former uses Playwright's patched build; the latter uses Selenium and is
selected as ``firefox-real``.  Both still need profiles isolated from the
user's everyday Firefox profile.

The launch arguments. "--start-minimized" and "--autoplay-policy=..." are
Chromium command-line switches. Firefox rejects unknown arguments and refuses
to start, so passing them through would turn an engine switch into a browser
that never opens. The equivalents exist, as profile preferences rather than
switches.

The profile directory. A Chromium profile means nothing to Firefox. Pointing
Firefox at it either fails or quietly starts from an empty profile, which
presents as being signed out of everything -- so each engine gets its own.
"""

import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import browser


class EngineSelection(unittest.TestCase):
    def test_chromium_is_the_default(self):
        """The engine holding his real sessions must not change by accident."""
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("WINTER_BROWSER_ENGINE", None)
            self.assertEqual(browser.engine(), "chromium")

    def test_firefox_is_selectable(self):
        with patch.dict(os.environ, {"WINTER_BROWSER_ENGINE": "firefox"}):
            self.assertEqual(browser.engine(), "firefox")

    def test_selection_is_case_and_space_tolerant(self):
        with patch.dict(os.environ, {"WINTER_BROWSER_ENGINE": "  FireFox "}):
            self.assertEqual(browser.engine(), "firefox")

    def test_an_unknown_engine_falls_back_rather_than_failing(self):
        with patch.dict(os.environ, {"WINTER_BROWSER_ENGINE": "netscape"}):
            self.assertEqual(browser.engine(), "chromium")

    def test_every_playwright_engine_is_one_playwright_has(self):
        self.assertEqual(set(browser.PLAYWRIGHT_ENGINES),
                         {"chromium", "firefox", "webkit"})

    def test_stock_firefox_is_a_separate_backend(self):
        self.assertEqual(browser.REAL_ENGINE, "firefox-real")
        self.assertIn(browser.REAL_ENGINE, browser.ENGINES)
        self.assertNotIn(browser.REAL_ENGINE, browser.PLAYWRIGHT_ENGINES)


class ProfilesStayApart(unittest.TestCase):
    def test_each_engine_gets_its_own_profile(self):
        paths = {}
        for name in browser.ENGINES:
            with patch.dict(os.environ, {"WINTER_BROWSER_ENGINE": name}):
                os.environ.pop("WINTER_BROWSER_PROFILE", None)
                paths[name] = browser.profile_dir()
        self.assertEqual(len(set(paths.values())), len(browser.ENGINES),
                         f"engines share a profile directory: {paths}")

    def test_chromium_keeps_the_original_path(self):
        """His existing sessions live there; moving them would log him out."""
        with patch.dict(os.environ, {"WINTER_BROWSER_ENGINE": "chromium"}):
            os.environ.pop("WINTER_BROWSER_PROFILE", None)
            self.assertEqual(browser.profile_dir(), browser.PROFILE_DIR)

    def test_firefox_profile_is_distinct_from_chromium(self):
        with patch.dict(os.environ, {"WINTER_BROWSER_ENGINE": "firefox"}):
            os.environ.pop("WINTER_BROWSER_PROFILE", None)
            self.assertNotEqual(browser.profile_dir(), browser.PROFILE_DIR)

    def test_an_explicit_profile_override_still_wins(self):
        """The test suites point this at a throwaway directory."""
        with patch.dict(os.environ, {"WINTER_BROWSER_ENGINE": "firefox",
                                     "WINTER_BROWSER_PROFILE": r"C:\tmp\p"}):
            self.assertEqual(str(browser.profile_dir()), r"C:\tmp\p")


class ChromiumSwitchesDoNotReachFirefox(unittest.TestCase):
    """The failure that would make an engine switch look like a broken build."""

    def _options_for(self, engine_name):
        seen = {}

        class FakeLauncher:
            def launch_persistent_context(self, profile, **options):
                seen.update(options)
                raise RuntimeError("stop here; the options are what matter")

        class FakePlaywright:
            chromium = FakeLauncher()
            firefox = FakeLauncher()
            webkit = FakeLauncher()

            def stop(self):
                return None

        old_worker, old_failed = browser._worker, browser._real_failed
        browser._worker = None
        browser._real_failed = False
        try:
            with patch.dict(os.environ, {"WINTER_BROWSER_ENGINE": engine_name}), \
                    patch("playwright.sync_api.sync_playwright") as factory:
                factory.return_value.start.return_value = FakePlaywright()
                try:
                    browser.start(visible=False)
                except Exception:
                    pass
        finally:
            browser._worker = old_worker
            browser._real_failed = old_failed
        return seen

    def test_firefox_is_not_given_chromium_switches(self):
        options = self._options_for("firefox")
        self.assertNotIn("args", options,
                         "Chromium switches would stop Firefox starting")

    def test_firefox_gets_preferences_instead(self):
        options = self._options_for("firefox")
        self.assertIn("firefox_user_prefs", options)

    def test_chromium_still_gets_its_switches(self):
        options = self._options_for("chromium")
        self.assertIn("args", options)
        self.assertTrue(any("start-minimized" in arg
                            for arg in options["args"]))

    def test_chromium_is_not_given_firefox_preferences(self):
        self.assertNotIn("firefox_user_prefs", self._options_for("chromium"))

    def test_downloads_are_directed_for_both(self):
        for name in ("chromium", "firefox"):
            with self.subTest(engine=name):
                options = self._options_for(name)
                self.assertTrue(options.get("downloads_path"))
                self.assertTrue(options.get("accept_downloads"))

    def test_a_missing_engine_says_how_to_install_it(self):
        """An uninstalled engine must not read as a broken browser."""
        class Boom:
            def launch_persistent_context(self, profile, **options):
                raise RuntimeError("Executable doesn't exist")

        class FakePlaywright:
            firefox = Boom()

            def stop(self):
                return None

        old_worker, old_failed = browser._worker, browser._real_failed
        browser._worker = None
        browser._real_failed = False
        try:
            with patch.dict(os.environ, {"WINTER_BROWSER_ENGINE": "firefox"}), \
                    patch("playwright.sync_api.sync_playwright") as factory:
                factory.return_value.start.return_value = FakePlaywright()
                result = browser.start(visible=False)
        finally:
            browser._worker = old_worker
            browser._real_failed = old_failed
        self.assertIn("playwright install firefox", str(result))


if __name__ == "__main__":
    unittest.main()
