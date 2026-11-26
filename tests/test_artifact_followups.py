# -*- coding: utf-8 -*-
import os
import queue
import tempfile
import unittest
from unittest.mock import patch

import actions
import artifact_context
import outcome_compiler
import outcome_planner
import outcomes


class ArtifactFollowupTests(unittest.TestCase):
    def test_file_move_is_not_compiled_as_calendar_reschedule(self):
        compiled = outcome_compiler.compile("move that screenshot to my desktop")
        self.assertEqual([item.operation for item in compiled.outcomes],
                         [outcomes.MOVE_ARTIFACT])
        self.assertEqual(outcome_planner.EXACT_ACTIONS[outcomes.MOVE_ARTIFACT],
                         "MOVE_FILE")

    def test_latest_verified_artifact_moves_without_approval(self):
        with tempfile.TemporaryDirectory() as folder:
            captures = os.path.join(folder, "captures")
            desktop = os.path.join(folder, "Desktop")
            os.makedirs(captures)
            os.makedirs(desktop)
            source = os.path.join(captures, "shot.png")
            with open(source, "wb") as stream:
                stream.write(b"png")
            artifact_context.record(source, "screenshot", "TAKE_SCREENSHOT")
            actions.reset_task_ledger("move it to desktop")
            with patch("artifact_context.desktop_directory", return_value=desktop):
                actions.execute_actions(
                    '<ACTION type="MOVE_FILE" dst="desktop" />', {},
                    queue.Queue(), folder, origin="user")
            destination = os.path.join(desktop, "shot.png")
            self.assertTrue(os.path.isfile(destination))
            self.assertFalse(os.path.exists(source))
            self.assertIn("Verified", actions.execute_actions._last_output)
            self.assertNotIn("Approval", actions.execute_actions._last_output)
            self.assertEqual(artifact_context.latest()["path"], destination)

    def test_dashboard_content_has_an_executable_outcome(self):
        compiled = outcome_compiler.compile("show this code on the dashboard")
        self.assertEqual([item.operation for item in compiled.outcomes],
                         [outcomes.DISPLAY_CONTENT])
        self.assertEqual(outcome_planner.EXACT_ACTIONS[outcomes.DISPLAY_CONTENT],
                         "DASHBOARD_SHOW_TEXT")

    def test_dashboard_action_emits_a_visible_document(self):
        ui = queue.Queue()
        actions.reset_task_ledger("show this code on the dashboard")
        actions.execute_actions(
            '<ACTION type="DASHBOARD_SHOW_TEXT" title="Example" language="python">\n'
            'print("hello")\n</ACTION>', {}, ui, ".", origin="user")
        events = []
        while not ui.empty():
            events.append(ui.get_nowait())
        documents = [payload for kind, payload in events
                     if kind == "dashboard_document"]
        self.assertEqual(documents[0]["title"], "Example")
        self.assertIn('print("hello")', documents[0]["content"])
        self.assertIn("visibly rendered", actions.execute_actions._last_output)


if __name__ == "__main__":
    unittest.main()
