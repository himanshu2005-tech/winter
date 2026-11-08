# -*- coding: utf-8 -*-
"""
Tests for carrying an approval into a corrected retry.

Himanshu asked for a failed command to be rewritten and run again. Handing the
failure back to the model was not enough on its own: the retry is the same gated
action type, so it queued for approval a second time and he was asked to approve
the same intent twice -- once for the broken command, once for the one that
worked. Telling the model "the approval still stands" changes nothing, because
the gate is not listening to the model.

So the approval is carried forward, and the whole value of this file is in how
narrowly. A grant covers one action type, once, for a few minutes, and only ever
comes into existence because a human approved something that then failed.
"""

import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import approval_grants


class GrantLifecycleTests(unittest.TestCase):

    def setUp(self):
        approval_grants.revoke()

    def tearDown(self):
        approval_grants.revoke()

    def test_nothing_is_granted_by_default(self):
        self.assertFalse(approval_grants.active("RUN_COMMAND"))
        self.assertFalse(approval_grants.consume("RUN_COMMAND"))

    def test_a_grant_permits_exactly_one_retry(self):
        """One yes funds one fix.

        If the corrected command also fails, the model is guessing -- and
        guessing is precisely when a human belongs back in the loop.
        """
        approval_grants.grant("RUN_COMMAND")
        self.assertTrue(approval_grants.consume("RUN_COMMAND"))
        self.assertFalse(approval_grants.consume("RUN_COMMAND"))

    def test_checking_does_not_spend_the_grant(self):
        approval_grants.grant("RUN_COMMAND")
        self.assertTrue(approval_grants.active("RUN_COMMAND"))
        self.assertTrue(approval_grants.active("RUN_COMMAND"))
        self.assertTrue(approval_grants.consume("RUN_COMMAND"))

    def test_a_grant_does_not_cover_another_action(self):
        """Consent to fix a command is not consent to send an email."""
        approval_grants.grant("RUN_COMMAND")
        self.assertFalse(approval_grants.consume("SEND_EMAIL"))
        self.assertFalse(approval_grants.consume("WRITE_FILE"))
        self.assertFalse(approval_grants.consume("DELETE_FILE"))
        # And the attempt must not have spent the real one.
        self.assertTrue(approval_grants.active("RUN_COMMAND"))

    def test_a_grant_expires(self):
        # Agreeing to fix a command now is not agreeing to run that kind of
        # command this evening.
        approval_grants.grant("RUN_COMMAND", ttl=0.15)
        time.sleep(0.35)
        self.assertFalse(approval_grants.active("RUN_COMMAND"))
        self.assertFalse(approval_grants.consume("RUN_COMMAND"))

    def test_the_window_is_short(self):
        self.assertLessEqual(approval_grants.TTL_SECONDS, 900)
        self.assertEqual(approval_grants.MAX_USES, 1)

    def test_case_does_not_matter(self):
        approval_grants.grant("run_command")
        self.assertTrue(approval_grants.consume("RUN_COMMAND"))

    def test_rubbish_is_refused(self):
        self.assertFalse(approval_grants.grant(""))
        self.assertFalse(approval_grants.grant(None))
        self.assertFalse(approval_grants.consume(None))


class GrantProvenanceTests(unittest.TestCase):
    """A grant must be impossible to manufacture from model output."""

    def _source(self, name):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        import io
        with io.open(os.path.join(root, name), encoding="utf-8") as handle:
            return handle.read()

    def test_only_a_failed_human_approval_issues_one(self):
        """grant() has exactly one caller, and it runs after a human said yes.

        If this ever grows a second caller, that caller needs the same scrutiny:
        it is the difference between "inherit an approval" and "self-approve".
        """
        callers = []
        for name in ("agent.py", "actions.py", "autonomy.py", "planner.py",
                     "runtime/action_adapter.py"):
            root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            path = os.path.join(root, name)
            if not os.path.exists(path):
                continue
            for number, line in enumerate(self._source(name).splitlines(), 1):
                if "approval_grants.grant(" in line:
                    callers.append(f"{name}:{number}")
        self.assertEqual(len(callers), 1, f"grant() callers: {callers}")
        self.assertTrue(callers[0].startswith("agent.py"))

    def test_the_caller_runs_only_after_approval(self):
        import inspect
        import agent
        source = inspect.getsource(agent._run_approved_action)
        self.assertIn("approval_grants.grant", source)
        # Issued on the failure path only: a command that worked needs no retry.
        grant_at = source.index("approval_grants.grant")
        success_return = source.index("completed.")
        self.assertLess(success_return, grant_at)

    def test_the_gate_consumes_rather_than_merely_checking(self):
        # A grant that survived its use would let a model try variations on one
        # yes until something went through.
        source = self._source("actions.py")
        self.assertIn("approval_grants.consume(action_type)", source)
        self.assertNotIn("approval_grants.active(action_type)", source)

    def test_every_use_is_announced(self):
        import inspect
        self.assertIn("print(", inspect.getsource(approval_grants.consume))
        self.assertIn("print(", inspect.getsource(approval_grants.grant))


class FailureDetectionTests(unittest.TestCase):
    """The retry has to notice the failure in the first place."""

    def test_failure_is_read_from_the_receipt(self):
        """_last_output arrives prefixed with the action name.

        "RUN_COMMAND: [Error] ..." does not start with "[Error]", so testing it
        with startswith missed every failure and the retry would never once have
        fired. The per-action receipt carries ok=False and is unambiguous.
        """
        import inspect
        import agent
        source = inspect.getsource(agent._run_approved_action)
        self.assertIn('item.get("ok")', source)
        self.assertIn("_results", source)

    def test_a_string_check_is_only_the_fallback(self):
        import inspect
        import agent
        source = inspect.getsource(agent._run_approved_action)
        receipt_at = source.index('item.get("ok")')
        fallback_at = source.index('actions._classify_output(clean_output)')
        self.assertLess(receipt_at, fallback_at)

    def test_the_prefix_bug_cannot_come_back(self):
        import inspect
        import agent
        source = inspect.getsource(agent._run_approved_action)
        self.assertNotIn('startswith(\n', source)
        self.assertNotIn('output.lstrip().startswith', source)


class PromptPlacementTests(unittest.TestCase):
    """A permission prompt that is half off-screen guards nothing.

    It sat bottom-right at first, so as to stay out of the way, and went partly
    off the edge: those offsets assumed SW/SH described the usable area, which
    they do not once display scaling and the taskbar are involved. If the prompt
    can be missed, the action it guards simply never happens.
    """

    class _Window:
        def __init__(self, w=1920, h=1080):
            self.geo = None
            self._w, self._h = w, h

        def winfo_exists(self):
            return True

        def winfo_screenwidth(self):
            return self._w

        def winfo_screenheight(self):
            return self._h

        def geometry(self, spec):
            self.geo = spec

    def _place(self, screen_w, screen_h, ask_window=False):
        import approval_prompt

        class Probe(approval_prompt.ApprovalPromptMixin):
            pass

        probe = Probe()
        probe._approval_win = self._Window(screen_w, screen_h)
        probe.SW = 0 if ask_window else screen_w
        probe.SH = 0 if ask_window else screen_h
        probe._centre_approval_prompt()
        spec = probe._approval_win.geo
        size, x, y = spec.split("+")
        width, height = (int(n) for n in size.split("x"))
        return width, height, int(x), int(y)

    def test_it_is_centred(self):
        width, height, x, y = self._place(1920, 1080)
        self.assertEqual(x, (1920 - width) // 2)
        self.assertEqual(y, (1080 - height) // 2)

    def test_it_is_centred_on_a_larger_display(self):
        width, height, x, y = self._place(3840, 2160)
        self.assertEqual(x, (3840 - width) // 2)
        self.assertEqual(y, (2160 - height) // 2)

    def test_it_never_goes_off_the_edge(self):
        for screen_w, screen_h in ((1920, 1080), (1366, 768), (400, 200),
                                   (3840, 2160)):
            width, height, x, y = self._place(screen_w, screen_h)
            self.assertGreaterEqual(x, 0, f"{screen_w}x{screen_h}")
            self.assertGreaterEqual(y, 0, f"{screen_w}x{screen_h}")

    def test_it_asks_the_window_when_the_size_is_not_known_yet(self):
        # The prompt can be built before SW/SH are set, and a position computed
        # from zero would put it in the corner for the rest of the session.
        width, height, x, y = self._place(1920, 1080, ask_window=True)
        self.assertEqual(x, (1920 - width) // 2)

    def test_it_is_re_centred_on_every_request(self):
        # The window is built once and reused, so placing it only at build time
        # freezes whatever was true then.
        import inspect
        import approval_prompt
        source = inspect.getsource(
            approval_prompt.ApprovalPromptMixin._show_approval_request)
        self.assertIn("_centre_approval_prompt()", source)

    def test_one_place_decides_the_geometry(self):
        import inspect
        import approval_prompt
        builder = inspect.getsource(
            approval_prompt.ApprovalPromptMixin._build_approval_prompt)
        self.assertNotIn("win.geometry(", builder,
                         "geometry must come from the one helper")


if __name__ == "__main__":
    unittest.main()
