# -*- coding: utf-8 -*-
"""The legacy Blender runner must not claim a visible persistent session."""

import queue
import unittest
from types import SimpleNamespace
from unittest import mock

import actions


class BlenderLegacyReceiptTests(unittest.TestCase):
    def tearDown(self):
        actions.set_action_speaker(None)
        actions.reset_task_ledger()

    @mock.patch("codetools.find_blender", return_value=("blender.exe", ""))
    @mock.patch("actions.subprocess.run")
    def test_nonzero_exit_is_failure(self, run, _find):
        run.return_value = SimpleNamespace(
            returncode=1, stdout="Blender quit", stderr="Traceback: bad API")
        actions.execute_actions(
            '<ACTION type="RUN_BLENDER_SCRIPT" speech="Running Blender">'
            'print("x")</ACTION>', {}, queue.Queue(), ".", origin="user")
        self.assertIn("[Error] Blender exited with code 1",
                      actions.execute_actions._last_output)
        self.assertFalse(actions.execute_actions._results[-1]["ok"])

    @mock.patch("codetools.find_blender", return_value=("blender.exe", ""))
    @mock.patch("actions.subprocess.run")
    def test_success_names_headless_lifecycle_honestly(self, run, _find):
        run.return_value = SimpleNamespace(
            returncode=0, stdout="wrote D:\\result.png", stderr="")
        actions.execute_actions(
            '<ACTION type="RUN_BLENDER_SCRIPT" speech="Running Blender">'
            'print("x")</ACTION>', {}, queue.Queue(), ".", origin="user")
        receipt = actions.execute_actions._last_output
        self.assertIn("[Success] Blender completed", receipt)
        self.assertIn("No visible Blender session remains", receipt)
        self.assertTrue(actions.execute_actions._results[-1]["ok"])


if __name__ == "__main__":
    unittest.main()
