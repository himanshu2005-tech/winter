# -*- coding: utf-8 -*-
"""The browser opens once, and never again after the window is gone.

"First time its opening and when i close it and ask it to open for other
task it is not." Reproduced exactly:

    1 start    : {"ok": true, "operation": "start", "tab": "t1"}
    2 goto     : {"ok": true, "url": "https://example.com/"}
    3 close tab: {"ok": true, "operation": "close", "tab": "t1"}
    4 start    : [Browser Error] BrowserContext.new_page: Protocol error
                 (Browser.newPage): can't access property
                 "delayedStartupPromise", window is null
    5 goto     : [Browser Error] ... window is null

Closing the last tab leaves the context object alive with no window behind
it. Firefox cannot open a page from that, and `ensure_started` tested only
`context is not None` -- which was true -- so nothing ever rebuilt it. The
browser was dead for the remainder of the process, and every later task got
the same error or nothing at all.

The same thing happens when Himanshu closes the window himself, which is what
his transcript shows: a turn that opened the browser, a window he shut, and
then three BROWSER_STARTs in the next turn that produced no page, ending in
"I couldn't open the portal in this session because the browser closed
unexpectedly".

Two fixes, because either alone leaves a hole. The session is torn down when
its last tab goes, so no corpse is left behind; and holding a context is no
longer taken as proof that it works, so a window closed from outside the API
is noticed and rebuilt.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import browser

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def worker_source():
    with open(os.path.join(ROOT, "browser.py"), encoding="utf-8") as handle:
        return handle.read()


class HoldingAContextIsNotProofItWorks(unittest.TestCase):
    """`context is not None` and "can open a page" are different questions."""

    def setUp(self):
        self.source = worker_source()

    def test_there_is_a_usability_check(self):
        self.assertIn("def context_is_usable(", self.source)

    def test_starting_consults_it(self):
        start = self.source.index("def ensure_started(")
        block = self.source[start:start + 500]
        self.assertIn("context_is_usable()", block,
                      "the early return still trusts a non-None context")

    def test_a_dead_context_is_discarded_rather_than_reused(self):
        start = self.source.index("def ensure_started(")
        block = self.source[start:start + 700]
        self.assertIn("discard_context(", block)

    def test_the_check_counts_open_pages(self):
        start = self.source.index("def context_is_usable(")
        block = self.source[start:self.source.index("def ensure_started(")]
        self.assertIn("is_closed()", block)

    def test_a_raising_context_counts_as_unusable(self):
        """If the browser process died, touching .pages throws rather than
        returning an empty list."""
        start = self.source.index("def context_is_usable(")
        block = self.source[start:self.source.index("def ensure_started(")]
        self.assertIn("except Exception", block)
        self.assertIn("return False", block)


class ClosingTheLastTabEndsTheSession(unittest.TestCase):
    def setUp(self):
        self.source = worker_source()

    def test_the_single_tab_close_counts_what_is_left(self):
        # Anchored on something only the close branch contains: the first
        # get_page call in the file belongs to goto.
        start = self.source.index('closed_title = page.title()')
        block = self.source[start - 400:start + 1200]
        self.assertIn("remaining", block)

    def test_it_tears_down_when_nothing_is_left(self):
        # Anchored on something only the close branch contains: the first
        # get_page call in the file belongs to goto.
        start = self.source.index('closed_title = page.title()')
        block = self.source[start - 400:start + 1200]
        self.assertIn("discard_context(", block)

    def test_the_receipt_says_the_session_ended(self):
        """Closing one tab ending the whole session is surprising, so it is
        reported rather than left to be discovered."""
        # Anchored on something only the close branch contains: the first
        # get_page call in the file belongs to goto.
        start = self.source.index('closed_title = page.title()')
        block = self.source[start - 400:start + 1200]
        self.assertIn("session_ended", block)

    def test_closing_all_tabs_uses_the_same_teardown(self):
        """Two teardown paths drift; one does not."""
        start = self.source.index('elif args.get("all_tabs", False):')
        block = self.source[start:start + 300]
        self.assertIn("discard_context()", block)
        self.assertNotIn("playwright.stop()", block)


class TheTeardownIsThorough(unittest.TestCase):
    def setUp(self):
        self.source = worker_source()

    def test_it_forgets_both_handles(self):
        start = self.source.index("def discard_context(")
        block = self.source[start:self.source.index("def context_is_usable(")]
        self.assertIn("context = playwright = None", block)

    def test_it_forgets_the_tab_names(self):
        """Stale ids would resolve to pages from a browser that is gone."""
        start = self.source.index("def discard_context(")
        block = self.source[start:self.source.index("def context_is_usable(")]
        self.assertIn("page_ids.clear()", block)

    def test_a_failure_to_close_cleanly_does_not_stop_the_teardown(self):
        """The point is to get rid of it; an error closing something already
        broken must not leave the handles in place."""
        start = self.source.index("def discard_context(")
        block = self.source[start:self.source.index("def context_is_usable(")]
        self.assertIn("except Exception", block)


@unittest.skipUnless(os.getenv("VANTA_TEST_REAL_BROWSER") == "1",
                     "launches Firefox; set VANTA_TEST_REAL_BROWSER=1")
class AgainstARealBrowser(unittest.TestCase):
    """The reproduction itself. Skipped by default because it launches
    Firefox twice and takes the better part of a minute."""

    @classmethod
    def setUpClass(cls):
        import tempfile
        os.environ["WINTER_BROWSER_ENGINE"] = "firefox"
        os.environ["WINTER_BROWSER_PROFILE"] = tempfile.mkdtemp(
            prefix="vanta_reopen_test_")
        os.environ["WINTER_BROWSER_IMPORT_COOKIES"] = "0"

    def test_it_reopens_after_the_last_tab_is_closed(self):
        self.assertIn('"ok": true', browser.start(visible=False))
        browser.goto("https://example.com", visible=False)
        browser.close()                       # the last tab
        again = browser.start(visible=False)
        self.assertIn('"ok": true', again, again)
        self.assertNotIn("window is null", again)
        navigated = browser.goto("https://example.com", visible=False)
        self.assertIn("example.com", navigated)
        browser.close(all_tabs=True)

    def test_it_reopens_after_a_full_close(self):
        browser.start(visible=False)
        browser.close(all_tabs=True)
        again = browser.start(visible=False)
        self.assertIn('"ok": true', again, again)
        browser.close(all_tabs=True)


if __name__ == "__main__":
    unittest.main()
