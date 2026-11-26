import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import mode_inbox
import notifications


class ModeInboxTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def test_records_every_email_once_and_drains_once(self):
        mode_inbox.begin(self.base, "sleep")
        self.assertTrue(mode_inbox.record(
            self.base, "email", "A ? First", source_id="gmail:1"))
        self.assertTrue(mode_inbox.record(
            self.base, "email", "B ? Second", source_id="gmail:2"))
        self.assertFalse(mode_inbox.record(
            self.base, "email", "A ? First", source_id="gmail:1"))
        digest = mode_inbox.drain(self.base, "sleep")
        self.assertEqual(len(digest["emails"]), 2)
        self.assertIn("A ? First", digest["spoken"])
        self.assertIn("B ? Second", digest["spoken"])
        self.assertEqual(mode_inbox.drain(self.base, "sleep")["count"], 0)

    def test_study_holds_notifications_too(self):
        mode_inbox.begin(self.base, "study")
        mode_inbox.record(self.base, "notification", "Build complete", "Tests passed")
        digest = mode_inbox.drain(self.base, "study")
        self.assertEqual(len(digest["notifications"]), 1)
        self.assertIn("Build complete", digest["dashboard"])

    def test_priority_metadata_survives_until_wake_digest(self):
        mode_inbox.begin(self.base, "sleep")
        mode_inbox.record(
            self.base, "email", "Security - New sign-in",
            source_id="gmail:security", priority="critical",
            reason="unrecognized account access")
        digest = mode_inbox.drain(self.base, "sleep")
        self.assertEqual(digest["emails"][0]["priority"], "critical")
        self.assertEqual(digest["emails"][0]["priority_reason"],
                         "unrecognized account access")
        self.assertIn("[CRITICAL]", digest["dashboard"])

    def test_inactive_mode_does_not_capture(self):
        self.assertFalse(mode_inbox.record(
            self.base, "notification", "Ordinary alert"))

    @mock.patch.object(notifications.subprocess, "run")
    @mock.patch.object(mode_inbox, "record", return_value=True)
    def test_routine_toast_is_held_without_display(self, record, run):
        result = notifications.notify("Mail", "Arrived", urgent=False)
        self.assertIn("Held for mode catch-up", result)
        run.assert_not_called()

    @mock.patch.object(notifications.subprocess, "run")
    @mock.patch.object(mode_inbox, "record", return_value=True)
    def test_urgent_toast_still_displays(self, record, run):
        run.return_value.returncode = 0
        run.return_value.stderr = ""
        result = notifications.notify("Alarm", "Wake up", urgent=True)
        self.assertIn("Notified", result)
        run.assert_called_once()


class ModeWiringTests(unittest.TestCase):
    def test_every_real_transition_is_announced_before_it_is_applied(self):
        from pathlib import Path
        source = Path("agent.py").read_text(encoding="utf-8")
        explicit = source.index("_announce_mode_switch(target)")
        applied = source.index("result = manager.switch(target", explicit)
        self.assertLess(explicit, applied)
        self.assertIn(
            'message = f"Switching to {POLICIES[mode].label.title()} mode."',
            source)

    def test_sleep_wakes_on_real_input_and_mail_polling_is_plural(self):
        from pathlib import Path
        source = Path("agent.py").read_text(encoding="utf-8")
        self.assertIn('_wake_from_sleep_if_needed("voice")', source)
        self.assertIn('_wake_from_sleep_if_needed("text" if is_text_input else "voice")', source)
        self.assertIn("maxResults=20", source)
        self.assertIn('BASE_DIR, "email", title', source)
        self.assertIn('verdict = email_briefing.triage(', source)
        self.assertIn('priority == "critical"', source)


if __name__ == "__main__":
    unittest.main()
