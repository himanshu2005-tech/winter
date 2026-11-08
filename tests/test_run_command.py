# -*- coding: utf-8 -*-
"""
Tests for running a shell command and knowing whether it worked.

The bug these exist for: Himanshu asked whether VANTA could install Python,
create a virtualenv and set a project up. It could not, for two reasons that
had nothing to do with the model.

RUN_COMMAND had a hardcoded 15 second timeout. Measured on this machine:
`python -m venv` took 20.2s and `pip install numpy` 15.3s, so environment
setup timed out on its two most basic steps.

And it collected result.returncode and threw it away. A command that failed
reached the model as ordinary output, indistinguishable from one that
succeeded -- the same shape as the browser receipts that always said ok:true
and the SET_FIELD that always said Success.
"""

import os
import queue
import subprocess
import sys
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class RunCommandContractTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        # Asked of the dispatch table rather than sliced out of actions.py
        # between two `elif` markers -- that slicing was guesswork that broke
        # whenever a neighbouring action moved, and it silently returned the
        # wrong code rather than failing.
        # Importing the bus alone does not register handlers.  This test used
        # to pass only when another module happened to import actions first.
        import actions  # noqa: F401
        import action_bus
        cls.body = action_bus.BUS.handler_source("RUN_COMMAND")
        assert cls.body, "RUN_COMMAND has no handler on the bus"

    def test_the_timeout_is_no_longer_fifteen_seconds(self):
        self.assertNotIn("timeout=15", self.body,
                         "a venv cannot be created in 15 seconds")

    def test_the_timeout_can_be_raised_by_the_caller(self):
        self.assertIn("attrs.get('timeout'", self.body)

    def test_the_timeout_is_bounded(self):
        # A hung command must not block the turn forever.
        self.assertIn("min(timeout_s, 900.0)", self.body)

    def test_a_nonzero_exit_becomes_an_error(self):
        self.assertIn("process.returncode != 0", self.body)
        self.assertIn("Command failed with exit code", self.body)

    def test_a_timeout_says_what_to_do_about_it(self):
        self.assertIn("timed out after", self.body)
        self.assertIn("WAIT_FOR", self.body)

    def test_the_schema_knows_about_timeout(self):
        with open(os.path.join(ROOT, "action_schema.py"), encoding="utf-8") as h:
            schema = h.read()
        self.assertIn('"RUN_COMMAND": {"requires": ["cmd"], "optional": ["timeout", "cwd", "shell"]}',
                      schema)

    def test_the_catalogue_documents_both(self):
        with open(os.path.join(ROOT, "agent.py"), encoding="utf-8") as handle:
            catalogue = handle.read()
        self.assertIn('RUN_COMMAND" cmd=', catalogue)
        self.assertIn("timeout is in seconds", catalogue)
        self.assertIn("non-zero exit code", catalogue)


class LiveCommandTests(unittest.TestCase):
    """Through the real dispatcher. approved=true replays a granted approval."""

    def setUp(self):
        import actions
        self.actions = actions
        self.ui = queue.Queue()

    def run_cmd(self, cmd, timeout=None):
        attr = f' timeout="{timeout}"' if timeout else ""
        xml = f'<ACTION type="RUN_COMMAND" approved="true"{attr} cmd="{cmd}" />'
        self.actions.execute_actions._last_output = ""
        self.actions.execute_actions(xml, {}, self.ui, ROOT, origin="user")
        return str(getattr(self.actions.execute_actions, "_last_output", ""))

    def test_a_successful_command_is_not_marked_as_an_error(self):
        out = self.run_cmd("Write-Output vanta-ok")
        self.assertIn("vanta-ok", out)
        self.assertNotIn("[Error]", out)

    def test_a_silent_success_has_a_verified_exit_receipt(self):
        out = self.run_cmd('exit 0')
        self.assertIn('exit_code=0', out)
        self.assertTrue(self.actions.execute_actions._results[0]['ok'])

    def test_a_failing_command_is_reported_as_one(self):
        out = self.run_cmd("exit 3")
        self.assertIn("[Error]", out)
        self.assertIn("exit code 3", out)

    def test_stderr_with_a_bad_exit_is_an_error(self):
        out = self.run_cmd("Write-Error boom; exit 1")
        self.assertIn("[Error]", out)

    def test_a_long_command_is_allowed_when_asked_for(self):
        # Comfortably past the old 15s ceiling.
        out = self.run_cmd("Start-Sleep -Seconds 18; Write-Output slept",
                           timeout=120)
        self.assertIn("slept", out)
        self.assertNotIn("timed out", out)

    def test_a_command_past_its_budget_times_out_clearly(self):
        out = self.run_cmd("Start-Sleep -Seconds 30", timeout=3)
        self.assertIn("timed out after", out)
        self.assertIn("WAIT_FOR", out)

    def test_abort_stops_a_running_command_and_its_task(self):
        interrupted = threading.Event()
        timer = threading.Timer(0.35, interrupted.set)
        xml = ('<ACTION type="RUN_COMMAND" approved="true" timeout="30" '
               'cmd="Start-Sleep -Seconds 20; Write-Output should-not-run" />')
        started = time.monotonic()
        timer.start()
        try:
            self.actions.execute_actions(
                xml, {}, self.ui, ROOT, origin="user",
                interrupt_event=interrupted)
        finally:
            timer.cancel()
        elapsed = time.monotonic() - started
        out = str(getattr(self.actions.execute_actions, "_last_output", ""))
        self.assertIn("[Interrupted]", out)
        self.assertLess(elapsed, 5.0, "abort left the PowerShell process running")
        self.assertNotIn("should-not-run", out)


@unittest.skipUnless(os.getenv("WINTER_ENV_TESTS"),
                     "set WINTER_ENV_TESTS=1 to build a real virtualenv")
class LiveEnvironmentTests(unittest.TestCase):
    """The thing he actually asked about: can it set a project up."""

    def setUp(self):
        import shutil
        import tempfile
        self.proj = os.path.join(tempfile.gettempdir(), "vanta_env_suite")
        shutil.rmtree(self.proj, ignore_errors=True)
        os.makedirs(self.proj, exist_ok=True)
        self.venv_py = os.path.join(self.proj, ".venv", "Scripts", "python.exe")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.proj, ignore_errors=True)

    def test_creating_a_venv_and_installing_into_it(self):
        import actions
        ui = queue.Queue()

        def run(cmd, timeout):
            xml = (f'<ACTION type="RUN_COMMAND" approved="true" '
                   f'timeout="{timeout}" cmd="{cmd}" />')
            actions.execute_actions._last_output = ""
            actions.execute_actions(xml, {}, ui, ROOT, origin="user")
            return str(getattr(actions.execute_actions, "_last_output", ""))

        venv_dir = os.path.join(self.proj, ".venv")
        out = run(f'python -m venv &quot;{venv_dir}&quot;', 300)
        self.assertNotIn("[Error]", out, out[:200])
        self.assertTrue(os.path.exists(self.venv_py), "no venv interpreter")

        out = run(f'&amp; &quot;{self.venv_py}&quot; -m pip install requests -q', 600)
        self.assertNotIn("[Error]", out, out[:200])

        out = run(f'&amp; &quot;{self.venv_py}&quot; -c '
                  f'&quot;import requests; print(requests.__version__)&quot;', 120)
        self.assertNotIn("[Error]", out, out[:200])
        self.assertRegex(out, r"\d+\.\d+")


class AttributeEscapeTests(unittest.TestCase):
    """A command is not allowed to be silently truncated.

    Attributes were read with (\w+)="([^"]+)", which stops at the first quote
    inside a value. A PowerShell command escaped the way PowerShell needs left
    cmd as a lone backslash, and the console reported:

        \ : The term '\' is not recognized as the name of a cmdlet

    The model had escaped its quotes correctly. The parser had not read them,
    and the failure looked like the model's mistake -- which sends you correcting
    the wrong thing.
    """

    def test_plain_attributes_still_work(self):
        import action_attrs
        parsed = action_attrs.parse('type="RUN_COMMAND" cmd="Get-Process"')
        self.assertEqual(parsed, {"type": "RUN_COMMAND", "cmd": "Get-Process"})

    def test_escaped_quotes_survive(self):
        import action_attrs
        raw = 'type="RUN_COMMAND" cmd="powershell -Command \\"Get-Item x\\""'
        parsed = action_attrs.parse(raw)
        self.assertEqual(parsed["cmd"], 'powershell -Command "Get-Item x"')

    def test_single_quoted_attributes_are_accepted(self):
        # A command full of double quotes is far easier to write inside single
        # ones, and there was never a reason to refuse it.
        import action_attrs
        parsed = action_attrs.parse("""type='RUN_COMMAND' cmd='Get-Item "HKLM:*"'""")
        self.assertEqual(parsed["cmd"], 'Get-Item "HKLM:*"')

    def test_nothing_is_lost_after_an_inner_quote(self):
        import action_attrs
        raw = 'a="one" b="two \\"inner\\" three" c="four"'
        parsed = action_attrs.parse(raw)
        self.assertEqual(parsed["c"], "four", "parsing stopped early")
        self.assertEqual(parsed["b"], 'two "inner" three')

    def test_the_dispatcher_uses_it(self):
        import inspect
        import actions
        self.assertIn("action_attrs.parse",
                      inspect.getsource(actions.execute_actions))

    def test_rubbish_does_not_raise(self):
        import action_attrs
        for raw in (None, "", "no pairs here", 'unclosed="'):
            self.assertIsInstance(action_attrs.parse(raw), dict)


class ApprovedActionOutcomeTests(unittest.TestCase):
    """Approval is not a promise of success.

    An approved action ran in a detached thread, so its receipt went nowhere.
    When the approved command failed, nothing saw the error, nothing retried,
    and Himanshu had been told it was approved while the console quietly showed
    it had not worked.
    """

    def test_a_failure_comes_back_as_a_turn(self):
        import inspect
        import agent
        source = inspect.getsource(agent._run_approved_action)
        self.assertIn("input_queue.put", source)
        self.assertIn("APPROVED ACTION FAILED", source)

    def test_the_approval_is_not_requested_again(self):
        # Re-queueing it would put the retry behind another approval he has
        # already given, which is how one check sat unrun for days.
        import inspect
        import agent
        source = inspect.getsource(agent._run_approved_action)
        self.assertIn("do not ask for it a second time", source)

    def test_retrying_forever_is_ruled_out(self):
        import inspect
        import agent
        source = inspect.getsource(agent._run_approved_action)
        self.assertIn("fails twice", source)

    def test_success_is_not_fed_back(self):
        import inspect
        import agent
        source = inspect.getsource(agent._run_approved_action)
        good = source.index("completed.")
        feed = source.index("input_queue.put")
        self.assertLess(good, feed, "a success must return before feeding back")

    def test_success_is_spoken_after_approval(self):
        import inspect
        import agent
        source = inspect.getsource(agent._run_approved_action)
        success = source.index('if not failed:')
        failure = source.index('failed after approval')
        self.assertIn('speak(message', source[success:failure])
        self.assertIn('agent_response', source[success:failure])

    def test_failure_is_judged_by_the_receipt_not_the_prefix(self):
        """This test used to assert the prefix list, which was the bug.

        _last_output arrives as "RUN_COMMAND: [Error] ...", so no prefix check
        on it could ever match and the retry would never have fired. The
        per-action receipt carries ok=False and says so unambiguously; the
        string check survives only for the case where no receipt exists.
        """
        import inspect
        import agent
        source = inspect.getsource(agent._run_approved_action)
        self.assertIn('item.get("ok")', source)
        receipt_at = source.index('item.get("ok")')
        fallback_at = source.index('actions._classify_output(clean_output)')
        self.assertLess(receipt_at, fallback_at,
                        "the receipt must be the primary signal")

    def test_the_dispatcher_is_told_not_to_wrap_powershell(self):
        """The cause, not just the symptom.

        RUN_COMMAND already runs in PowerShell; wrapping it forces quote
        escaping inside an attribute for no benefit.
        """
        import agent
        notes = agent.ACTION_CATALOGS["SYSTEM"]
        self.assertIn("never write", notes)
        self.assertIn("powershell -Command", notes)


if __name__ == "__main__":
    unittest.main()
