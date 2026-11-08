# -*- coding: utf-8 -*-
"""
Broad coverage of browser control: navigation, form controls, tabs, waiting,
and the guards that refuse a URL.

Two bugs these exist for. When field resolution was added, click, type, press,
fill_form and submit were moved onto it -- and select, check and upload were
left behind on the ref-only path. A dropdown or a file input could still be
reached only from a snapshot, which is the exact gap the resolver was written
to close. The old error text ("take a fresh snapshot") was what gave it away.

The fixture is served from loopback, which the URL guard refuses by default.
The test opts in for its own page and puts the setting back afterwards.
"""

import json
import os
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

PAGE_HTML = """<!doctype html><html><body>
  <h1>Control Page</h1>
  <a id="lnk" href="next.html">Go to next page</a>
  <label for="c">Country</label>
  <select id="c" name="country">
    <option value="in">India</option>
    <option value="uk">United Kingdom</option>
    <option value="jp">Japan</option>
  </select>
  <label for="f">Attachment</label>
  <input id="f" name="attachment" type="file">
  <section aria-label="Resume documents">
    <button id="add-resume" type="button">Add resume</button>
    <input id="resume-file" name="resume_file" type="file" hidden>
  </section>
  <label for="t">Notes</label>
  <textarea id="t" name="notes"></textarea>
  <label for="k">Subscribe</label>
  <input id="k" name="subscribe" type="checkbox">
  <button id="b" type="button"
     onclick="document.getElementById('out').textContent='clicked'">Press me</button>
  <div id="out"></div>
</body></html>"""

NEXT_HTML = ("<!doctype html><html><body><h1>Second Page</h1>"
             "<p>arrived here</p></body></html>")


class UrlGuardTests(unittest.TestCase):
    """These refuse before any network call, so they need no browser."""

    def setUp(self):
        self.saved = os.environ.pop("WINTER_BROWSER_ALLOW_PRIVATE", None)

    def tearDown(self):
        os.environ.pop("WINTER_BROWSER_ALLOW_PRIVATE", None)
        if self.saved is not None:
            os.environ["WINTER_BROWSER_ALLOW_PRIVATE"] = self.saved

    def validate(self, url):
        import browser
        return browser._validate_url(url)

    def test_a_non_http_scheme_is_refused(self):
        with self.assertRaises(ValueError):
            self.validate("file:///C:/Windows/win.ini")

    def test_credentials_in_a_url_are_refused(self):
        with self.assertRaises(ValueError):
            self.validate("https://user:pass@example.com")

    def test_a_relative_url_is_refused(self):
        with self.assertRaises(ValueError):
            self.validate("/just/a/path")

    def test_loopback_is_refused_by_default(self):
        with self.assertRaises(ValueError):
            self.validate("http://127.0.0.1:8080/page.html")

    def test_loopback_is_allowed_when_opted_in(self):
        os.environ["WINTER_BROWSER_ALLOW_PRIVATE"] = "1"
        self.assertTrue(self.validate("http://127.0.0.1:8080/page.html"))


class LiveControlTests(unittest.TestCase):
    """Navigation, controls, tabs and waiting against a served fixture."""

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
            tempfile.gettempdir(), "vanta_test_profile_control")


        import http.server
        import socketserver

        cls.dir = os.path.join(tempfile.gettempdir(), "vanta_control_suite")
        os.makedirs(cls.dir, exist_ok=True)
        for name, body in (("page.html", PAGE_HTML), ("next.html", NEXT_HTML)):
            with open(os.path.join(cls.dir, name), "w", encoding="utf-8") as fh:
                fh.write(body)
        cls.upload = os.path.join(cls.dir, "attach.txt")
        with open(cls.upload, "w", encoding="utf-8") as fh:
            fh.write("vanta upload test")

        directory = cls.dir

        class Handler(http.server.SimpleHTTPRequestHandler):
            def __init__(self, *a, **kw):
                super().__init__(*a, directory=directory, **kw)

            def log_message(self, *a):
                pass

        cls.httpd = socketserver.TCPServer(("127.0.0.1", 0), Handler)
        cls.port = cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        cls.page = f"http://127.0.0.1:{cls.port}/page.html"

        cls.saved = os.environ.get("WINTER_BROWSER_ALLOW_PRIVATE")
        os.environ["WINTER_BROWSER_ALLOW_PRIVATE"] = "1"

        import browser
        cls.browser = browser
        try:
            cls.browser.start(visible=False)
            result = cls.browser.goto(cls.page)
        except Exception as exc:
            raise unittest.SkipTest(f"browser unavailable: {exc}")
        if "[Browser Error]" in result:
            raise unittest.SkipTest(f"could not serve fixture: {result[:120]}")

    @classmethod
    def tearDownClass(cls):
        for close in (lambda: cls.browser.close(all_tabs=True),
                      cls.httpd.shutdown, cls.httpd.server_close):
            try:
                close()
            except Exception:
                pass
        os.environ.pop("WINTER_BROWSER_ALLOW_PRIVATE", None)
        if cls.saved is not None:
            os.environ["WINTER_BROWSER_ALLOW_PRIVATE"] = cls.saved

    def setUp(self):
        self.browser.goto(self.page)

    def receipt(self, raw):
        self.assertIn("] ", raw, f"not a receipt: {raw[:160]}")
        try:
            return json.loads(raw.split("] ", 1)[1])
        except ValueError:
            self.fail(f"not JSON: {raw[:160]}")

    def snap(self):
        return self.receipt(self.browser.snapshot())

    # -- navigation ------------------------------------------------- #

    def test_clicking_a_link_navigates(self):
        self.browser.click("Go to next page")
        snap = self.snap()
        self.assertIn("next.html", snap["url"])
        self.assertIn("arrived here", snap["text"])

    def test_an_unresolvable_host_is_an_error(self):
        result = self.browser.goto(
            "https://winter-should-not-resolve-xyz.invalid")
        self.assertIn("[Browser Error]", result)

    # -- form controls ---------------------------------------------- #

    def test_a_select_is_set_by_option_label(self):
        data = self.receipt(self.browser.select("country", "Japan"))
        self.assertTrue(data["ok"], data)
        chosen = [e for e in self.snap()["elements"] if e.get("tag") == "select"]
        self.assertTrue(chosen)
        self.assertIn(chosen[0].get("value"), ("jp", "Japan"))

    def test_a_select_is_reachable_without_a_snapshot_ref(self):
        # select was left on the ref-only path when everything else moved.
        result = self.browser.select("country", "United Kingdom")
        self.assertNotIn("take a fresh snapshot", result)
        self.assertTrue(self.receipt(result)["ok"])

    def test_a_checkbox_is_reachable_by_name(self):
        data = self.receipt(self.browser.check("subscribe", True))
        self.assertTrue(data["ok"], data)

    def test_uploading_a_file_by_field_name(self):
        data = self.receipt(self.browser.upload("attachment", self.upload))
        self.assertTrue(data["ok"], data)
        self.assertTrue(data["verified"], data)
        self.assertEqual(data["file"], os.path.basename(self.upload))

    def test_hidden_file_input_is_reached_from_its_visible_trigger(self):
        data = self.receipt(self.browser.upload("Add resume", self.upload))
        self.assertTrue(data["ok"], data)
        self.assertTrue(data["verified"], data)
        self.assertEqual(data["file"], os.path.basename(self.upload))
        self.assertEqual(data["observed_files"], [os.path.basename(self.upload)])

    def test_targeted_snapshot_finds_offscreen_or_hidden_upload_controls(self):
        payload = json.loads(
            self.browser.snapshot(query="resume upload").split("] ", 1)[1])
        names = {str(item.get("name") or "").casefold()
                 for item in payload["elements"]}
        self.assertIn("add resume", names)
        self.assertTrue(any(item.get("type") == "file"
                            for item in payload["elements"]))
        self.assertTrue(payload.get("focused_ref"))

    def test_upload_refuses_a_missing_local_file_before_touching_the_page(self):
        data = self.receipt(self.browser.upload(
            "Add resume", os.path.join(self.dir, "missing-resume.pdf")))
        self.assertFalse(data["ok"], data)
        self.assertIn("does not exist", data.get("error", ""))

    def test_typing_into_a_textarea_by_name(self):
        data = self.receipt(self.browser.type_text("notes", "some notes here"))
        self.assertTrue(data["ok"], data)
        values = {e.get("value") for e in self.snap()["elements"]}
        self.assertIn("some notes here", values)

    # -- buttons and keys ------------------------------------------- #

    def test_clicking_a_button_runs_its_handler(self):
        self.browser.click("Press me")
        self.assertIn("clicked", self.snap()["text"])

    def test_pressing_a_key_in_a_field(self):
        data = self.receipt(self.browser.press("notes", "Tab"))
        self.assertTrue(data["ok"], data)

    def test_something_absent_is_reported_as_a_failure(self):
        result = self.browser.click("no such control anywhere on this page")
        self.assertIn("[Browser Error]", result)

    # -- waiting ----------------------------------------------------- #

    def test_waiting_for_text_that_is_present(self):
        self.assertTrue(self.receipt(self.browser.wait(text="Control Page"))["ok"])

    def test_waiting_for_absent_text_fails_rather_than_hanging(self):
        import time
        start = time.perf_counter()
        result = self.browser.wait(
            text="text that is not on this page at all", ms=1500)
        elapsed = time.perf_counter() - start
        self.assertIn("[Browser Error]", result)
        self.assertLess(elapsed, 10,
                        "the wait is unbounded again; it used to burn the "
                        "context default of 30s every time")

    def test_a_found_wait_says_what_it_matched(self):
        data = self.receipt(self.browser.wait(text="Control Page", ms=5000))
        self.assertTrue(data["ok"])
        self.assertIn("Control Page", data.get("found", ""))

    # -- tabs --------------------------------------------------------- #

    def test_tabs_can_be_listed(self):
        raw = self.browser.tabs()
        self.assertIn("[Browser Tabs]", raw)
        self.assertGreaterEqual(len(json.loads(raw.split("] ", 1)[1])), 1)

    def test_navigating_a_named_tab(self):
        tabs = json.loads(self.browser.tabs().split("] ", 1)[1])
        data = self.receipt(self.browser.goto(
            f"http://127.0.0.1:{self.port}/next.html", tab=tabs[0]["tab"]))
        self.assertTrue(data["ok"], data)

    def test_an_unknown_tab_id_is_an_error(self):
        self.assertIn("[Browser Error]", self.browser.goto(self.page, tab="t99"))


if __name__ == "__main__":
    unittest.main()
