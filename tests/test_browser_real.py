import functools
import http.server
import json
import os
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import browser
import browser_real

SCHEMA = ("CREATE TABLE moz_cookies (id INTEGER PRIMARY KEY, originAttributes TEXT "
          "NOT NULL DEFAULT '', name TEXT, value TEXT, host TEXT, path TEXT, "
          "expiry INTEGER, lastAccessed INTEGER, creationTime INTEGER, "
          "isSecure INTEGER, isHttpOnly INTEGER, inBrowserElement INTEGER DEFAULT 0, "
          "sameSite INTEGER DEFAULT 0, schemeMap INTEGER DEFAULT 0, "
          "CONSTRAINT moz_uniqueid UNIQUE (name, host, path, originAttributes))")


def make_db(path, rows):
    con = sqlite3.connect(path)
    con.execute(SCHEMA)
    for name, value, host, accessed in rows:
        con.execute(
            "INSERT INTO moz_cookies (originAttributes,name,value,host,path,expiry,"
            "lastAccessed,creationTime,isSecure,isHttpOnly) VALUES "
            "('',?,?,?,'/',9999999999,?,1,1,1)", (name, value, host, accessed))
    con.commit()
    con.close()


def read(path):
    con = sqlite3.connect(path)
    try:
        return {(r[0], r[1]): (r[2], r[3]) for r in con.execute(
            "SELECT name, host, value, lastAccessed FROM moz_cookies")}
    finally:
        con.close()


class CookieMerge(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.src = os.path.join(self.dir.name, "src.sqlite")
        self.dst = os.path.join(self.dir.name, "profile", "cookies.sqlite")

    def tearDown(self):
        self.dir.cleanup()

    def test_first_run_copies_his_cookies_across(self):
        make_db(self.src, [("sid", "A", ".portal.edu", 10)])
        added, updated = browser_real.merge_cookies(self.src, self.dst)
        self.assertEqual((added, updated), (1, 0))
        self.assertEqual(read(self.dst)[("sid", ".portal.edu")], ("A", 10))

    def test_vantas_own_cookies_survive_the_merge(self):
        os.makedirs(os.path.dirname(self.dst))
        make_db(self.dst, [("captcha_ok", "mine", ".portal.edu", 50)])
        make_db(self.src, [("sid", "A", ".portal.edu", 10)])
        added, _ = browser_real.merge_cookies(self.src, self.dst)
        rows = read(self.dst)
        self.assertEqual(added, 1)
        self.assertIn(("captcha_ok", ".portal.edu"), rows)
        self.assertIn(("sid", ".portal.edu"), rows)

    def test_the_newer_copy_of_a_cookie_wins(self):
        os.makedirs(os.path.dirname(self.dst))
        make_db(self.dst, [("sid", "old", ".a.com", 10), ("keep", "vanta-newer", ".b.com", 90)])
        make_db(self.src, [("sid", "fresh", ".a.com", 40), ("keep", "his-older", ".b.com", 20)])
        _, updated = browser_real.merge_cookies(self.src, self.dst)
        rows = read(self.dst)
        self.assertEqual(updated, 1)
        self.assertEqual(rows[("sid", ".a.com")][0], "fresh")
        self.assertEqual(rows[("keep", ".b.com")][0], "vanta-newer")

    def test_saved_passwords_are_never_touched(self):
        import inspect
        source = inspect.getsource(browser_real)
        for forbidden in ("logins.json", "key4.db"):
            self.assertNotIn(f'"{forbidden}"', source)
            self.assertNotIn(f"'{forbidden}'", source)

    def test_seeding_never_raises(self):
        with mock.patch("browser_login.default_profile", side_effect=OSError("boom")):
            self.assertIn("could not", browser_real.seed_sessions(Path(self.dir.name)))
        with mock.patch("browser_login.default_profile", return_value=None):
            self.assertIn("no Firefox profile", browser_real.seed_sessions(Path(self.dir.name)))


class EngineSelection(unittest.TestCase):
    def tearDown(self):
        browser._worker = None
        browser._real_failed = False

    def test_the_real_engine_is_known_and_selectable(self):
        with mock.patch.dict(os.environ, {"WINTER_BROWSER_ENGINE": "firefox-real"}):
            self.assertEqual(browser.engine(), "firefox-real")

    def test_a_missing_selenium_falls_back_to_playwright_not_to_nothing(self):
        browser._worker = None
        browser._real_failed = False
        with mock.patch.dict(os.environ, {"WINTER_BROWSER_ENGINE": "firefox-real"}), \
                mock.patch.dict("sys.modules", {"browser_real": None}):
            worker = browser._make_worker()
        self.assertIsInstance(worker, browser._BrowserWorker)
        self.assertEqual(worker.engine_name, "firefox")
        self.assertTrue(browser._real_failed)

    def test_the_playwright_fallback_never_uses_the_synthetic_engine_name(self):
        worker = browser._BrowserWorker(engine_name="firefox-real")
        self.assertEqual(worker.engine_name, "firefox")
        # Other live-browser suites deliberately point the profile at a
        # temporary directory.  This contract is about the production default,
        # so do not let a previous suite's override decide the assertion.
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("WINTER_BROWSER_PROFILE", None)
            self.assertEqual(browser.profile_dir(worker.engine_name),
                             browser.PROFILE_DIR.with_name(
                                 browser.PROFILE_DIR.name + "_firefox"))

    def test_the_binary_can_be_overridden(self):
        with tempfile.NamedTemporaryFile(suffix=".exe", delete=False) as fake:
            pass
        try:
            with mock.patch.dict(os.environ, {"WINTER_FIREFOX_PATH": fake.name}):
                self.assertEqual(browser_real.firefox_binary(), fake.name)
        finally:
            os.remove(fake.name)

    def test_frames_are_classified_like_the_playwright_worker(self):
        self.assertEqual(browser_real._frame_kind(""), "blank")
        self.assertEqual(browser_real._frame_kind("about:blank"), "blank")
        self.assertEqual(browser_real._frame_kind("https://www.google.com/recaptcha/api2"), "captcha")
        self.assertEqual(browser_real._frame_kind("https://portal.edu/login"), "content")


PAGES = {
    "index.html": ("<html><head><title>Portal</title></head><body><h1>Placements</h1>"
                   "<span id=go onclick=\"document.getElementById('b').click()\">LOGIN</span>"
                   "<button id=b style=display:none onclick=\"document.title='clicked'\">h</button>"
                   "<iframe src=/form.html width=400 height=300></iframe>"
                   "<a href=/jobs.html>Jobs list</a></body></html>"),
    "form.html": ("<html><body><form onsubmit=\"document.body.insertAdjacentHTML("
                  "'beforeend','<p>submitted '+document.getElementById('u').value+'</p>');"
                  "return false;\"><label for=u>Username</label><input id=u name=username>"
                  "<input type=password name=password>"
                  "<select name=role><option>Student</option><option>Admin</option></select>"
                  "<input type=checkbox name=agree> Agree"
                  "<input type=radio name=size value=small> <input type=radio name=size value=large>"
                  "<button type=submit>Sign in</button></form></body></html>"),
    "jobs.html": "<html><head><title>Jobs</title></head><body><h2>Open jobs</h2>"
                 "<ul><li>AI Intern at Cornixe</li></ul></body></html>",
    "upload.html": ("<html><head><title>Application</title></head><body>"
                    "<h2>Apply for Graduate Engineer</h2>"
                    "<section><button type=button>Add resume</button>"
                    "<label for=resume>Resume or CV</label>"
                    "<input id=resume name=resume_file type=file style='display:none' "
                    "onchange=\"document.getElementById('picked').textContent=this.files[0].name\">"
                    "<div id=picked></div></section></body></html>"),
}


@unittest.skipUnless(browser_real.firefox_binary(), "Firefox is not installed")
class EndToEnd(unittest.TestCase):
    """A real Firefox, headless, against a local portal-shaped site."""

    @classmethod
    def setUpClass(cls):
        try:
            import selenium  # noqa: F401
        except ImportError:
            raise unittest.SkipTest("selenium is not installed")
        cls.site = tempfile.TemporaryDirectory()
        for name, body in PAGES.items():
            Path(cls.site.name, name).write_text(body, encoding="utf-8")

        class Quiet(http.server.SimpleHTTPRequestHandler):
            def log_message(self, *args):
                pass

        handler = functools.partial(Quiet, directory=cls.site.name)
        cls.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.httpd.server_address[1]}"
        cls.profile = tempfile.mkdtemp(prefix="vanta_real_test_")
        cls.env = mock.patch.dict(os.environ, {
            "WINTER_BROWSER_ENGINE": "firefox-real",
            "WINTER_BROWSER_PROFILE": cls.profile,
            "WINTER_BROWSER_ALLOW_PRIVATE": "1",
            "WINTER_BROWSER_IMPORT_COOKIES": "0"})
        cls.env.start()
        browser._worker = None
        browser._real_failed = False
        started = browser.start(visible=False)
        if "Browser Error" in started:
            cls.tearDownClass()
            raise unittest.SkipTest(f"Firefox would not start here: {started[:120]}")

    @classmethod
    def tearDownClass(cls):
        try:
            browser.close(all_tabs=True)
        except Exception:
            pass
        browser._worker = None
        cls.env.stop()
        cls.httpd.shutdown()
        cls.site.cleanup()

    def test_a_portal_in_an_iframe_can_be_read_filled_and_submitted(self):
        self.assertIn('"ok": true', browser.goto(self.base + "/index.html"))
        snapshot = json.loads(browser.snapshot().split("] ", 1)[1])
        names = {(e["tag"], e.get("name")) for e in snapshot["elements"]}
        self.assertIn(("span", "LOGIN"), names)
        self.assertIn(("input", "Username"), names)
        framed = [e for e in snapshot["elements"] if e.get("frame")]
        self.assertTrue(framed, "elements inside the iframe were not found")
        filled = browser.fill_form({
            "username": {"kind": "fill", "value": "CH.SC.U4AIE23028"},
            "password": {"kind": "fill", "value": "pw"},
            "role": {"kind": "select", "value": "Admin"},
            "agree": {"kind": "check", "value": "true"},
            "size": {"kind": "fill", "value": "large"}})
        self.assertIn('"failed": []', filled)
        browser.submit("Sign in")
        self.assertIn('"found"', browser.wait(text="submitted CH.SC.U4AIE23028", ms=4000))

    def test_a_hidden_button_driven_by_a_visible_span_is_clickable(self):
        browser.goto(self.base + "/index.html")
        self.assertIn('"ok": true', browser.click("LOGIN"))
        self.assertIn("clicked", browser.tabs())

    def test_clicking_a_link_reports_the_navigation_and_the_page_is_readable(self):
        browser.goto(self.base + "/index.html")
        receipt = browser.click("Jobs list")
        self.assertIn('"navigated": true', receipt)
        self.assertIn("AI Intern at Cornixe", browser.snapshot())

    def test_a_missing_field_says_what_to_do(self):
        browser.goto(self.base + "/jobs.html")
        self.assertIn("Could not find", browser.click("no-such-control-xyz"))

    def test_the_browser_is_firefox_not_nightly(self):
        # The point of the engine: a stock Firefox, not Playwright's build.
        from selenium.webdriver.common.by import By  # noqa: F401
        worker = browser._worker
        self.assertIsInstance(worker, browser_real.RealFirefoxWorker)

    def test_targeted_snapshot_finds_and_scrolls_to_resume_controls(self):
        browser.goto(self.base + "/upload.html")
        snapshot = json.loads(
            browser.snapshot(query="resume upload").split("] ", 1)[1])
        self.assertEqual(snapshot["query"], "resume upload")
        self.assertTrue(snapshot.get("focused_ref"))
        names = {str(item.get("name") or "").lower()
                 for item in snapshot["elements"]}
        self.assertTrue(any("resume" in name for name in names), names)
        self.assertTrue(any(item.get("type") == "file"
                            for item in snapshot["elements"]))

    def test_hidden_resume_upload_is_verified_by_selected_filename(self):
        browser.goto(self.base + "/upload.html")
        resume = Path(self.site.name, "Himanshu-Resume.pdf")
        resume.write_bytes(b"synthetic resume")
        receipt = json.loads(
            browser.upload("Add resume", str(resume)).split("] ", 1)[1])
        self.assertTrue(receipt["ok"], receipt)
        self.assertTrue(receipt["verified"], receipt)
        self.assertIn(resume.name, receipt["observed_files"])
        self.assertEqual(receipt["file"], resume.name)

    def test_missing_upload_file_is_refused_before_touching_the_page(self):
        browser.goto(self.base + "/upload.html")
        receipt = json.loads(
            browser.upload("Add resume", str(Path(self.site.name, "missing.pdf")))
            .split("] ", 1)[1])
        self.assertFalse(receipt["ok"])
        self.assertFalse(receipt["verified"])
        self.assertIn("does not exist", receipt["error"])


if __name__ == "__main__":
    unittest.main()
