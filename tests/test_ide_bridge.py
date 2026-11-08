import json
import tempfile
import unittest
from pathlib import Path
from queue import Queue
from unittest.mock import MagicMock, patch

import actions
import autonomy
import coding_tasks
import ide_bridge


class IdeBridgeTests(unittest.TestCase):
    def test_open_file_uses_native_goto_and_exact_position(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "app.py"
            target.write_text("print('ok')", encoding="utf-8")
            with patch.object(ide_bridge, "antigravity_executable", return_value="IDE.exe"), \
                 patch.object(ide_bridge, "_launch") as launch:
                result = ide_bridge.open_file(folder, target, 7, 3)
            args = launch.call_args.args[0]
            self.assertEqual(args[:2], ["IDE.exe", "--goto"])
            self.assertTrue(args[2].endswith("app.py:7:3"))
            self.assertIn("Opened", result)

    def test_missing_file_is_refused_before_launch(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(ide_bridge, "_launch") as launch:
            with self.assertRaises(FileNotFoundError):
                ide_bridge.open_file(folder, Path(folder) / "missing.py")
            launch.assert_not_called()

    def test_config_defaults_to_antigravity(self):
        with tempfile.TemporaryDirectory() as folder:
            self.assertEqual(ide_bridge.load_config(folder)["default_ide"], "antigravity")


class CodingTaskTests(unittest.TestCase):
    def test_commands_use_constrained_modes(self):
        with patch.object(coding_tasks, "_find_codex", return_value="codex.exe"):
            implementation = coding_tasks._command("codex", Path("D:/p"), "implement")
            review = coding_tasks._command("codex", Path("D:/p"), "review")
        self.assertIn("workspace-write", implementation)
        self.assertIn("read-only", review)
        self.assertNotIn("dangerously-bypass-approvals-and-sandbox", implementation)

    def test_prompt_is_sent_over_stdin_not_command_line(self):
        process = MagicMock()
        process.pid = 42
        process.returncode = 0
        process.communicate.return_value = (None, None)
        with tempfile.TemporaryDirectory() as base:
            project = Path(base) / "project"
            project.mkdir()
            with patch.object(coding_tasks, "_command", return_value=["agent.exe"]), \
                 patch.object(coding_tasks.subprocess, "Popen", return_value=process) as popen:
                receipt = coding_tasks.start(base, project, "private task", "codex")
                task_id = receipt.split("task ", 1)[1].split(" ", 1)[0]
                for _ in range(500):
                    record = coding_tasks._read(base, task_id)
                    if record["status"] in {"completed", "failed"}:
                        break
                    import time
                    time.sleep(0.01)
            command = popen.call_args.args[0]
            self.assertNotIn("private task", " ".join(command))
            process.communicate.assert_called_with("private task\n")

    def test_ide_and_agent_mutations_are_blocked_proactively(self):
        for name in ("IDE_OPEN_PROJECT", "IDE_OPEN_FILE", "IDE_SHOW_DIFF",
                     "CODING_TASK_START", "CODING_TASK_CANCEL"):
            self.assertFalse(autonomy.action_allowed(name, "proactive")[0], name)

    def test_two_implementers_cannot_edit_same_project(self):
        with tempfile.TemporaryDirectory() as base:
            project = Path(base) / "project"
            project.mkdir()
            root = coding_tasks._root(base)
            (root / "code-existing.json").write_text(json.dumps({
                "id": "code-existing", "agent": "codex", "mode": "implement",
                "project": str(project.resolve()), "status": "running"}), encoding="utf-8")
            with patch.object(coding_tasks, "_command", return_value=["agent.exe"]):
                with self.assertRaisesRegex(RuntimeError, "already editing"):
                    coding_tasks.start(base, project, "change it", "claude", "implement")

    @patch("ide_bridge.status", return_value={"text": "[Success] ready"})
    def test_ide_status_dispatches(self, status):
        actions.reset_task_ledger("check ide")
        actions.execute_actions('<ACTION type="IDE_STATUS" />', {}, Queue(), ".")
        status.assert_called_once()
        self.assertIn("ready", actions.execute_actions._last_output)


if __name__ == "__main__":
    unittest.main()
