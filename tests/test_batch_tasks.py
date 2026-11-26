import json
import os
import queue
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import actions


class BatchTaskTests(unittest.TestCase):
    def test_batch_creates_each_task_in_one_action(self):
        payload = json.dumps([
            {"title": "Monday review", "due": "2026-10-12"},
            {"title": "Tuesday review", "due": "2026-10-13"},
        ])
        response = f'<ACTION type="ADD_TASKS" list="My Tasks">{payload}</ACTION>'
        with mock.patch("providers.google_api.add_google_task",
                        side_effect=["Task created: one", "Task created: two"]) as add:
            actions.reset_task_ledger("add my weekly tasks")
            actions.execute_actions(response, {}, queue.Queue(), ".", origin="user")
        self.assertEqual(add.call_count, 2)
        self.assertIn("Created 2/2", actions.execute_actions._last_output)

    def test_batch_rejects_empty_payload(self):
        actions.reset_task_ledger("add tasks")
        actions.execute_actions('<ACTION type="ADD_TASKS">[]</ACTION>', {},
                                queue.Queue(), ".", origin="user")
        self.assertIn("[Error]", actions.execute_actions._last_output)


if __name__ == "__main__":
    unittest.main()
