# -*- coding: utf-8 -*-
"""Browser contracts against deliberately awkward, framework-style controls.

The page is local and synthetic: no production site, account, or hostname is
encoded in browser control.  It combines patterns common to modern editors and
poorly built portals: ARIA widgets, contenteditable, open shadow DOM, and an
SPA submit that succeeds without changing URL.
"""

import http.server
import json
import os
import socketserver
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


HTML = b"""<!doctype html><html><body>
<label>Title <div role="textbox" contenteditable="true" aria-label="Title"></div></label>
<div id="theme" role="combobox" aria-label="Theme" aria-expanded="false"
     onclick="this.setAttribute('aria-expanded','true');document.querySelector('#options').hidden=false">Choose</div>
<div id="options" hidden><div role="option" onclick="theme.textContent=this.textContent;theme.setAttribute('aria-expanded','false');options.hidden=true">Minimal</div></div>
<div id="notify" role="switch" aria-label="Notifications" aria-checked="false"
     onclick="this.setAttribute('aria-checked',this.getAttribute('aria-checked')==='true'?'false':'true')">Notify</div>
<div id="host"></div>
<button aria-label="Save" onclick="document.querySelector('#result').textContent='Saved';this.remove()">Save</button>
<div id="result"></div>
<script>
 const root=document.querySelector('#host').attachShadow({mode:'open'});
 root.innerHTML='<label>Project name <input name="project_name"></label>';
</script></body></html>"""


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(HTML)))
        self.end_headers()
        self.wfile.write(HTML)

    def log_message(self, *args):
        pass


class HostileFormTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if os.getenv("WINTER_SKIP_BROWSER_TESTS"):
            raise unittest.SkipTest("browser tests disabled")
        try:
            import playwright  # noqa: F401
        except ImportError:
            raise unittest.SkipTest("playwright is not installed")
        import browser
        cls.browser_module = browser
        cls.server = socketserver.TCPServer(("127.0.0.1", 0), Handler)
        cls.server_thread = threading.Thread(target=cls.server.serve_forever,
                                             daemon=True)
        cls.server_thread.start()
        cls.old_private = os.environ.get("WINTER_BROWSER_ALLOW_PRIVATE")
        cls.old_profile = os.environ.get("WINTER_BROWSER_PROFILE")
        os.environ["WINTER_BROWSER_ALLOW_PRIVATE"] = "1"
        os.environ["WINTER_BROWSER_PROFILE"] = tempfile.mkdtemp(
            prefix="vanta_hostile_forms_")
        if os.getenv("VANTA_TEST_REAL_FIREFOX"):
            import browser_real
            cls.worker = browser_real.RealFirefoxWorker()
        else:
            cls.worker = browser._BrowserWorker(engine_name="chromium")
        cls.url = f"http://127.0.0.1:{cls.server.server_address[1]}/"
        started = cls.worker.call("start", visible=False)
        if "[Browser Error]" in started:
            raise unittest.SkipTest(started)

    @classmethod
    def tearDownClass(cls):
        try:
            cls.worker.call("close", all_tabs=True)
        except Exception:
            pass
        cls.server.shutdown()
        cls.server.server_close()
        if cls.old_private is None:
            os.environ.pop("WINTER_BROWSER_ALLOW_PRIVATE", None)
        else:
            os.environ["WINTER_BROWSER_ALLOW_PRIVATE"] = cls.old_private
        if cls.old_profile is None:
            os.environ.pop("WINTER_BROWSER_PROFILE", None)
        else:
            os.environ["WINTER_BROWSER_PROFILE"] = cls.old_profile

    def receipt(self, raw):
        self.assertNotIn("[Browser Error]", raw)
        return json.loads(raw.split("] ", 1)[1])

    def reset(self):
        self.receipt(self.worker.call("goto", url=self.url, visible=False))

    def test_rich_text_and_shadow_dom_fields_are_found_and_verified(self):
        self.reset()
        result = self.receipt(self.worker.call("fill_form", fields={
            "Title": {"kind": "fill", "value": "Quarterly plan"},
            "project_name": {"kind": "fill", "value": "Atlas"},
        }))
        self.assertTrue(result["ok"], result.get("failed"))
        self.assertEqual(set(result["verified"]), {"Title", "project_name"})

    def test_custom_dropdown_and_switch_are_verified(self):
        self.reset()
        selected = self.receipt(self.worker.call(
            "select", ref="Theme", value="Minimal"))
        checked = self.receipt(self.worker.call(
            "check", ref="Notifications", checked=True))
        self.assertTrue(selected["verified"])
        self.assertTrue(checked["verified"])

    def test_same_page_submit_counts_only_when_application_state_changes(self):
        self.reset()
        result = self.receipt(self.worker.call("submit", ref="Save"))
        self.assertTrue(result["ok"])
        self.assertTrue(result["changed"])
        self.assertFalse(result["navigated"])

    def test_snapshot_describes_non_native_controls(self):
        self.reset()
        snapshot = json.loads(self.worker.call("snapshot").split("] ", 1)[1])
        roles = {item.get("role") for item in snapshot["elements"]}
        self.assertTrue({"textbox", "combobox", "switch"} <= roles)


if __name__ == "__main__":
    unittest.main()
