# -*- coding: utf-8 -*-
"""
Tests for seeing into iframes, and for recognising a bot check.

The bug these exist for: VANTA was asked to log in to placements.haveloc.com.
It navigated there, then reported that the form could not be submitted and the
button could not be clicked. That was true, and the reason it gave was wrong.

The page has five frames. BROWSER_SNAPSHOT searched only the main one, so it
returned zero elements -- an empty page with no explanation. Any site that puts
its login form in an iframe was invisible in exactly the same way.

The page is also behind a reCAPTCHA. That is a control asking for a person, so
the frames are reported as blockers and never offered as something to click:
VANTA says who has to act, and Himanshu completes it himself.
"""

import json
import os
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

INNER = """<!doctype html><html><body>
  <form id="login">
    <label for="u">Username</label><input id="u" name="username" type="text">
    <label for="p">Password</label><input id="p" name="password" type="password">
    <button type="button" id="go">Sign in</button>
  </form>
</body></html>"""

OUTER = """<!doctype html><html><body>
  <h1>Portal</h1><iframe src="inner.html" width="420" height="220"></iframe>
</body></html>"""


class FrameSourceTests(unittest.TestCase):
    """Source-level; no browser needed."""

    @classmethod
    def setUpClass(cls):
        with open(os.path.join(ROOT, "browser.py"), encoding="utf-8") as handle:
            cls.source = handle.read()

    def test_the_snapshot_walks_every_frame(self):
        self.assertIn("for frame in page.frames:", self.source)
        self.assertIn("def scan_frames():", self.source)

    def test_targets_are_searched_across_frames(self):
        self.assertIn("def content_frames(page)", self.source)
        self.assertIn("for scope in content_frames(page):", self.source)

    def test_a_captcha_frame_is_never_searched_for_something_to_click(self):
        # content_frames returns only 'content' frames, so a bot check is not
        # a place VANTA goes looking for a button.
        self.assertIn('if frame_kind(frame) == "content":', self.source)

    def test_a_captcha_is_reported_as_a_blocker(self):
        self.assertIn('"kind": "captcha"', self.source)
        self.assertIn("a human must complete this", self.source)

    def test_the_known_providers_are_named(self):
        for host in ("google.com/recaptcha", "hcaptcha.com",
                     "challenges.cloudflare.com"):
            self.assertIn(host, self.source)

    def test_an_empty_page_is_retried_before_being_believed(self):
        # A bot check's iframes attach a second or two after load.
        self.assertIn("page.wait_for_timeout(2500)", self.source)

    def test_an_empty_page_explains_itself(self):
        self.assertIn("No interactive element was found in any frame",
                      self.source)


class LiveIframeTests(unittest.TestCase):
    """A form inside an iframe, served locally."""

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
            tempfile.gettempdir(), "vanta_test_profile_frames")


        import http.server
        import socketserver

        cls.dir = os.path.join(tempfile.gettempdir(), "vanta_iframe_suite")
        os.makedirs(cls.dir, exist_ok=True)
        for name, body in (("inner.html", INNER), ("outer.html", OUTER)):
            with open(os.path.join(cls.dir, name), "w", encoding="utf-8") as fh:
                fh.write(body)

        directory = cls.dir

        class Handler(http.server.SimpleHTTPRequestHandler):
            def __init__(self, *a, **kw):
                super().__init__(*a, directory=directory, **kw)

            def log_message(self, *a):
                pass

        cls.httpd = socketserver.TCPServer(("127.0.0.1", 0), Handler)
        cls.port = cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

        # A loopback page is refused unless this is set; it is set here only
        # so the test can serve its own fixture.
        cls.saved = os.environ.get("WINTER_BROWSER_ALLOW_PRIVATE")
        os.environ["WINTER_BROWSER_ALLOW_PRIVATE"] = "1"

        import browser
        cls.browser = browser
        try:
            cls.browser.start(visible=False)
            result = cls.browser.goto(
                f"http://127.0.0.1:{cls.port}/outer.html")
        except Exception as exc:
            raise unittest.SkipTest(f"browser unavailable: {exc}")
        if "[Browser Error]" in result:
            raise unittest.SkipTest(f"could not serve fixture: {result[:120]}")

    @classmethod
    def tearDownClass(cls):
        try:
            cls.browser.close(all_tabs=True)
        except Exception:
            pass
        try:
            cls.httpd.shutdown()
            cls.httpd.server_close()
        except Exception:
            pass
        os.environ.pop("WINTER_BROWSER_ALLOW_PRIVATE", None)
        if cls.saved is not None:
            os.environ["WINTER_BROWSER_ALLOW_PRIVATE"] = cls.saved

    def snap(self):
        return json.loads(self.browser.snapshot().split("] ", 1)[1])

    def test_iframe_elements_appear_in_the_snapshot(self):
        elements = self.snap()["elements"]
        self.assertGreaterEqual(len(elements), 3,
                                "the iframe's form is still invisible")
        inner = [e for e in elements if e.get("frame")]
        self.assertEqual(len(inner), 3)

    def test_each_iframe_element_says_which_frame_it_is_in(self):
        inner = [e for e in self.snap()["elements"] if e.get("frame")]
        self.assertTrue(all("inner.html" in e["frame"] for e in inner))

    def test_refs_stay_unique_across_frames(self):
        refs = [e["ref"] for e in self.snap()["elements"]]
        self.assertEqual(len(refs), len(set(refs)))

    def test_filling_a_form_inside_an_iframe(self):
        self.browser.goto(f"http://127.0.0.1:{self.port}/outer.html")
        raw = self.browser.fill_form({
            "username": {"kind": "fill", "value": "CH.SC.U4AIE23028"},
            "password": {"kind": "fill", "value": "not-a-real-password"},
        })
        data = json.loads(raw.split("] ", 1)[1])
        self.assertTrue(data["ok"], data.get("failed"))
        self.assertEqual(sorted(data["filled"]), ["password", "username"])

        values = {e.get("name"): e.get("value")
                  for e in self.snap()["elements"] if e.get("value")}
        self.assertIn("CH.SC.U4AIE23028", values.values())

    def test_clicking_a_button_inside_an_iframe(self):
        raw = self.browser.click("Sign in")
        self.assertIn("] ", raw, raw[:140])
        self.assertTrue(json.loads(raw.split("] ", 1)[1])["ok"])


if __name__ == "__main__":
    unittest.main()
