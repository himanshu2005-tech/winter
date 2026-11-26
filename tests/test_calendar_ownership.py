import queue
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import actions


class CalendarOwnershipTests(unittest.TestCase):
    def setUp(self):
        self.ui = queue.Queue()

    @mock.patch("providers.google_api.add_calendar_event")
    def test_background_job_cannot_be_put_on_calendar(self, add):
        actions.execute_actions(
            '<ACTION type="ADD_CALENDAR_EVENT" date="2026-10-01" '
            'summary="Check product price" purpose="system_job" />',
            {}, self.ui, ".", origin="user")
        add.assert_not_called()
        self.assertIn("Calendar Policy", actions.execute_actions._last_output)
        self.assertIn("durable WATCH", actions.execute_actions._last_output)

    @mock.patch("providers.google_api.add_calendar_event",
                return_value="Event created")
    def test_human_reminder_is_allowed(self, add):
        actions.execute_actions(
            '<ACTION type="ADD_CALENDAR_EVENT" date="2026-10-06" '
            'summary="Amazon sale starts in two days" '
            'purpose="user_reminder" />',
            {}, self.ui, ".", origin="user")
        add.assert_called_once()
        self.assertIn("Event created", actions.execute_actions._last_output)

    @mock.patch("providers.google_api.add_calendar_event")
    def test_explicit_bulk_batch_needs_no_separate_approval(self, add):
        response = "".join(
            '<ACTION type="ADD_CALENDAR_EVENT" date="2026-10-0%s" '
            'summary="Real commitment %s" purpose="user_commitment" />' % (day, day)
            for day in range(1, 5))
        actions.execute_actions(response, {}, self.ui, ".", origin="user")
        self.assertEqual(add.call_count, 4)

    def test_planner_routes_machine_work_to_system_watch(self):
        from pathlib import Path
        planner = Path("planner.py").read_text(encoding="utf-8")
        self.assertIn("Calendar ownership is human, not computational", planner)
        self.assertIn("schedule a durable", planner)


if __name__ == "__main__":
    unittest.main()
