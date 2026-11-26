# -*- coding: utf-8 -*-
"""Native JavaScript dialogs and in-page modals remain controllable."""

import json
import os
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


PAGE = b"""<!doctype html><html><body>
<div role="dialog" aria-label="Example modal">
  <h2>Choose a plan</h2><button>Continue</button>
</div>
<button onclick="alert('Saved successfully')">Show alert</button>
<button onclick="document.getElementById('out').textContent =
  confirm('Proceed with change?') ? 'confirmed' : 'cancelled'">Show confirm</button>
<button onclick="document.getElementById('out').textContent =
  prompt('What name?', '') || 'empty'">Show prompt</button>
<p id="out">waiting</p>
</body></html>"""


class _PageHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(PAGE)))
        self.end_headers()
        self.wfile.write(PAGE)

    def log_message(self, _format, *args):
        pass


def _payload(raw):
    if "] " not in raw:
        raise AssertionError(raw)
    return json.loads(raw.split("] ", 1)[1])


class DialogContractTests(unittest.TestCase):
    def test_dialog_modes_are_closed_and_normalised(self):
        import browser
        self.assertEqual(browser.normalize_dialog_mode("OK"), "accept")
        self.assertEqual(browser.normalize_dialog_mode("cancel"), "dismiss")
        self.assertEqual(browser.normalize_dialog_mode("inspect"), "status")
        with self.assertRaises(ValueError):
            browser.normalize_dialog_mode("run arbitrary javascript")

    def test_dialog_is_a_registered_action_with_validated_arguments(self):
        import actions
        import action_bus
        import action_schema
        import autonomy
        self.assertTrue(action_bus.BUS.handles("BROWSER_DIALOG"))
        self.assertIn("BROWSER_DIALOG", autonomy.KNOWN_ACTIONS)
        self.assertIsNone(action_schema.validate(
            "BROWSER_DIALOG", {"type": "BROWSER_DIALOG", "mode": "accept"}))


class LiveDialogTests(unittest.TestCase):
    """Uses a local page; skips only when no managed-browser binary exists."""

    @classmethod
    def setUpClass(cls):
        if os.getenv("WINTER_SKIP_BROWSER_TESTS"):
            raise unittest.SkipTest("browser tests disabled")
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _PageHandler)
        cls.server_thread = threading.Thread(
            target=cls.server.serve_forever, daemon=True)
        cls.server_thread.start()
        cls.old_private = os.environ.get("WINTER_BROWSER_ALLOW_PRIVATE")
        cls.old_profile = os.environ.get("WINTER_BROWSER_PROFILE")
        os.environ["WINTER_BROWSER_ALLOW_PRIVATE"] = "1"
        os.environ["WINTER_BROWSER_PROFILE"] = os.path.join(
            tempfile.gettempdir(), "vanta_test_profile_dialogs")
        import browser
        cls.browser = browser
        cls.url = f"http://127.0.0.1:{cls.server.server_port}/"
        try:
            raw = cls.browser.start(visible=False)
            if "[Browser Error]" in raw:
                raise unittest.SkipTest(raw)
            raw = cls.browser.goto(cls.url, visible=False)
            if "[Browser Error]" in raw:
                raise unittest.SkipTest(raw)
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

    def setUp(self):
        self.browser.goto(self.url, visible=False)

    def test_html_modal_text_is_part_of_the_snapshot(self):
        snap = _payload(self.browser.snapshot())
        self.assertTrue(any(
            item.get("kind") == "dialog" and "Choose a plan" in item.get("text", "")
            for item in snap.get("overlays", [])), snap)

    def test_information_alert_is_observed_without_freezing_the_page(self):
        result = _payload(self.browser.click("Show alert"))
        dialogs = result.get("dialogs") or [result.get("browser_dialog", {})]
        self.assertTrue(any("Saved successfully" in item.get("message", "")
                            for item in dialogs), result)
        # Selenium may leave an unarmed dialog for an explicit decision;
        # Playwright safely accepts a plain informational alert immediately.
        if result.get("browser_dialog"):
            settled = _payload(self.browser.dialog("accept"))
            self.assertEqual(settled["dialog"]["handled"], "accept")

    def test_unarmed_confirm_is_never_silently_accepted(self):
        result = _payload(self.browser.click("Show confirm"))
        if result.get("browser_dialog"):
            self.assertEqual(result["requires_action"], "BROWSER_DIALOG")
            self.browser.dialog("dismiss")
        else:
            self.assertEqual(result["dialogs"][-1]["handled"], "dismiss")

    def test_armed_confirm_and_prompt_are_handled_with_receipts(self):
        armed = _payload(self.browser.dialog("accept"))
        self.assertEqual(armed["armed_for_next_dialog"], "accept")
        accepted = _payload(self.browser.click("Show confirm"))
        observed = accepted.get("dialogs") or [accepted.get("dialog", {})]
        self.assertTrue(any(item.get("handled") == "accept" for item in observed),
                        accepted)

        self.browser.dialog("accept", text="Himanshu")
        prompted = _payload(self.browser.click("Show prompt"))
        observed = prompted.get("dialogs") or [prompted.get("dialog", {})]
        self.assertTrue(any(item.get("handled") == "accept" for item in observed),
                        prompted)
        self.assertIn("Himanshu", _payload(self.browser.snapshot()).get("text", ""))


if __name__ == "__main__":
    unittest.main()
