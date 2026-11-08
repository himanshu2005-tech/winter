# -*- coding: utf-8 -*-
"""
Tests for what an action actually tells the model.

The bug these exist for: VANTA read five unread emails and then said "You have
no unread emails right now, sir."

READ_EMAILS deliberately keeps message bodies out of the remote model context --
which is right, they are private. But it handed the model *only* a handle:

    [Success] [READ_EMAILS result (query: is:unread)] (Handle: 6D7915E4_H_7C7BF3)

There is no action that dereferences a handle. `session_context.resolve_handle`
exists but is not in the dispatcher, not in KNOWN_ACTIONS and not in any
catalogue. So the model could not tell five messages from zero, and on the next
attempt said so explicitly: "I'm receiving only handles, not the actual email
content."

The fix is not to ship the bodies. Senders, subjects and counts are metadata,
and they are enough to answer "anything new?" honestly while the bodies stay on
the machine.

SEARCH_VAULT had the same shape with none of the justification -- it searches
the graph the orchestrator already injects into every prompt.
"""

import io
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import actions

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

SAMPLE = """[1a0d8ae4952c81cc] (unread)
From: Mail Delivery Subsystem <mailer-daemon@googlemail.com>
Subject: Delivery Status Notification (Failure)
Snippet: Address not found Your message wasn't delivered to founder@example.org
---
[1a0d86b5e6ae45ea] (unread)
From: LinkedIn <messages-noreply@linkedin.com>
Subject: Frontend Developer: Tata Consultancy Services hired near you
Snippet: SampleUser, TCS is a top company hiring for Frontend Developer roles
---
[1a0d833e6ef7b13f]
From: Zara <zara@news.zara.com>
Subject: This week at Zara
Snippet: Woman & Man Collection
---"""


class InternalBrowserEvidenceTests(unittest.TestCase):

    def test_snapshots_are_not_user_facing_results(self):
        self.assertNotIn("BROWSER_SNAPSHOT", actions.VISIBLE_RESULT_ACTIONS)
        self.assertNotIn("BROWSER_READ", actions.VISIBLE_RESULT_ACTIONS)

    def test_snapshot_still_reaches_the_model_as_observation_evidence(self):
        self.assertIn("BROWSER_SNAPSHOT", actions.OBSERVATION_ONLY_ACTIONS)
        self.assertIn("BROWSER_READ", actions.OBSERVATION_ONLY_ACTIONS)


class EmailDigestTests(unittest.TestCase):

    def digest(self, raw=SAMPLE):
        # Shared/default installations keep snippets private. Himanshu's
        # private runtime opts in explicitly; test the conservative branch
        # without depending on whether another imported module loaded .env.
        with mock.patch.dict(os.environ,
                             {"VANTA_EMAIL_MODEL_CONTENT": "0"}):
            return actions._email_digest_for_model(raw)

    def test_the_model_learns_how_many_arrived(self):
        # The whole failure was not knowing five from zero.
        text = self.digest()
        self.assertIn("3 message(s)", text)
        self.assertIn("2 unread", text)

    def test_senders_and_subjects_are_visible(self):
        text = self.digest()
        self.assertIn("LinkedIn", text)
        self.assertIn("Frontend Developer", text)
        self.assertIn("Zara", text)

    def test_read_and_unread_are_distinguished(self):
        text = self.digest()
        self.assertIn("(unread) LinkedIn", text)
        self.assertIn("(read) Zara", text)

    def test_bodies_and_snippets_never_leave_the_machine(self):
        text = self.digest()
        for private in ("example.org", "SampleUser", "Woman & Man Collection",
                        "Address not found"):
            self.assertNotIn(private, text,
                             f"{private!r} leaked into the model context")

    def test_an_empty_inbox_says_zero_rather_than_nothing(self):
        text = self.digest("No emails found in INBOX.")
        self.assertIn("0 messages", text)

    def test_empty_input_is_safe(self):
        self.assertIn("0 messages", self.digest(""))

    def test_an_unexpected_format_is_admitted_not_guessed(self):
        text = self.digest("something entirely unlike an email listing")
        self.assertIn("could not read", text)

    def test_the_model_is_told_it_cannot_fetch_the_bodies(self):
        # Otherwise it spends turns trying, which is what happened.
        text = self.digest()
        self.assertIn("cannot retrieve the bodies", text)


class HandleVisibilitySourceTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        with open(os.path.join(ROOT, "actions.py"), encoding="utf-8") as handle:
            cls.source = handle.read()

    def test_read_emails_no_longer_returns_a_bare_handle(self):
        self.assertIn("_email_digest_for_model(res)", self.source)

    def test_search_vault_returns_its_results(self):
        self.assertIn('f"[Success] {handle_ref}\\n{results}"', self.source)

    def test_no_action_claims_to_resolve_a_handle(self):
        # If one is ever added, these tests should be revisited -- the digest
        # exists precisely because there is no way to dereference.
        for name in ("READ_HANDLE", "RESOLVE_HANDLE", "EXPAND_HANDLE"):
            self.assertNotIn(name, self.source)


class CatalogueTextTests(unittest.TestCase):
    """The prompt has to say what it was written to say.

    The example paths in the SYSTEM catalogue were written in ordinary triple
    quotes, so "C:\path\to\file.py" reached the model as C: then a real TAB
    then "o" then a FORMFEED -- and the formfeed split the line in two. Nothing
    failed; the model was simply shown a corrupt example of the one thing it
    most needs to get right, an absolute Windows path. Invalid escapes warn,
    but the damaging ones are the VALID escapes, which are silent.
    """

    CONTROL = {"\t": "tab", "\f": "formfeed", "\v": "vertical tab",
               "\a": "bell", "\b": "backspace", "\r": "carriage return"}

    def test_no_catalogue_contains_a_stray_control_character(self):
        import agent
        for name, text in agent.ACTION_CATALOGS.items():
            for char, label in self.CONTROL.items():
                self.assertNotIn(
                    char, text,
                    f"the {name} catalogue contains a {label}, which almost "
                    f"always means a Windows path lost its backslash")

    def test_agent_has_no_invalid_escape_sequences(self):
        import warnings
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with io.open(os.path.join(root, "agent.py"), encoding="utf-8") as handle:
            source = handle.read()
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            compile(source, "agent.py", "exec")
        escapes = [str(w.message) for w in caught
                   if "invalid escape" in str(w.message)]
        self.assertEqual(escapes, [], f"invalid escapes in agent.py: {escapes}")

    def test_the_example_paths_still_look_like_paths(self):
        import agent
        system = agent.ACTION_CATALOGS["SYSTEM"]
        self.assertIn(r"C:\path\to\file.py", system)
        self.assertIn(r"C:\some\folder", system)


class ApprovalRouteTests(unittest.TestCase):
    """An approval needs somewhere to go, and only one way around it.

    Observed in one real session: VANTA queued a read-only check, said it was
    waiting, retried it, was failed by its own critic, retried again, and then
    rewrote the same check as RUN_PYTHON to get the answer without approval.
    Eleven model calls and 71,508 prompt tokens, because a yes had nowhere to go
    and the gate had a door beside it.
    """

    def test_the_request_reaches_the_screen(self):
        import inspect
        import actions
        source = inspect.getsource(actions._queue_for_approval)
        self.assertIn('"approval_request"', source,
                      "an approval with no prompt can only be given by "
                      "knowing a magic phrase")
        self.assertIn("approval_prompt", source)

    def test_the_model_is_told_not_to_retry_or_reroute(self):
        import inspect
        import actions
        source = inspect.getsource(actions._queue_for_approval)
        self.assertIn("Do NOT retry", source)
        self.assertIn("another action", source)

    def test_the_prompt_reuses_the_existing_decision_path(self):
        """Buttons must not be a second implementation of approval.

        One queue, one replay, one audit trail -- the buttons send the same
        words the voice path already used.
        """
        import inspect
        import approval_prompt
        source = inspect.getsource(approval_prompt.ApprovalPromptMixin)
        self.assertIn('"approve"', source)
        self.assertIn('"reject"', source)
        self.assertIn("_in_q.put", source)

    def test_the_prompt_cannot_approve_twice(self):
        """Settling consumes the request, so a second answer finds nothing.

        The prompt buttons and voice both feed the same settlement path. The
        property being protected is unchanged: one approval
        settles one request, and the second attempt has nothing to settle.
        """
        import agent
        from unittest.mock import patch
        with patch.object(agent, "pending_approval_exists", return_value=False), \
                patch.object(agent, "_settle_pending_approval", return_value=False):
            self.assertEqual(agent.approval_verdict("approve", False),
                             "approve")
            self.assertFalse(agent.handle_text_during_task("approve"))

    def test_deciding_dismisses_the_prompt(self):
        import inspect
        import agent
        source = inspect.getsource(agent._settle_pending_approval)
        self.assertGreaterEqual(source.count('"approval_settled"'), 2,
                                "both approve and reject must clear the prompt")

    def test_central_approval_replays_the_exact_pending_payload(self):
        """Semantic consent and the button path must meet at one executor."""
        import agent
        payload = '{"type":"SEND_WHATSAPP","contact":"Madhu","message":"hello"}'
        thread = unittest.mock.Mock()
        with unittest.mock.patch(
                "memory.state_db.execute_read",
                return_value=[("approval-1", "SEND_WHATSAPP", payload)]), \
             unittest.mock.patch("memory.state_db.execute_write") as write, \
             unittest.mock.patch.object(agent, "Thread", return_value=thread) as factory:
            self.assertTrue(agent._settle_pending_approval("approve"))
        self.assertIn("approved", write.call_args.args[0])
        replay = factory.call_args.kwargs["args"][0]
        self.assertIn('type="SEND_WHATSAPP"', replay)
        self.assertIn('contact="Madhu"', replay)
        self.assertIn('approved="true"', replay)
        thread.start.assert_called_once_with()

    def test_semantic_rejection_never_starts_the_action(self):
        import agent
        payload = '{"type":"SEND_WHATSAPP","contact":"Madhu","message":"hello"}'
        with unittest.mock.patch(
                "memory.state_db.execute_read",
                return_value=[("approval-1", "SEND_WHATSAPP", payload)]), \
             unittest.mock.patch("memory.state_db.execute_write") as write, \
             unittest.mock.patch.object(agent, "Thread") as thread:
            self.assertTrue(agent._settle_pending_approval("reject"))
        self.assertIn("rejected", write.call_args.args[0])
        thread.assert_not_called()

    def test_an_action_is_described_not_just_named(self):
        # Approving a command without seeing the command is not consent to
        # anything in particular.
        import approval_prompt
        detail = approval_prompt.describe("RUN_COMMAND", {"cmd": "Remove-Item x"})
        self.assertIn("Remove-Item", detail)
        self.assertIn("RUN_COMMAND", detail)

    def test_a_payload_with_nothing_recognisable_still_describes(self):
        import approval_prompt
        self.assertEqual(approval_prompt.describe("MYSTERY", {}), "MYSTERY")
        self.assertIn("MYSTERY", approval_prompt.describe("MYSTERY", None))


class PythonGateTests(unittest.TestCase):
    """RUN_COMMAND was gated and RUN_PYTHON was not, which gated nothing."""

    def test_shelling_out_needs_approval(self):
        import actions
        for code in ("import subprocess; subprocess.run(['dir'])",
                     "import os; os.system('del x')",
                     "import os; os.popen('whoami')"):
            self.assertTrue(actions.python_needs_approval(code), code)

    def test_writing_and_deleting_need_approval(self):
        import actions
        for code in ("open('x.txt','w').write('hi')",
                     "with open('log','a') as f: f.write('x')",
                     "import shutil; shutil.rmtree('C:/x')",
                     "import os; os.remove('x')"):
            self.assertTrue(actions.python_needs_approval(code), code)

    def test_reaching_the_network_needs_approval(self):
        import actions
        for code in ("import requests; requests.post('http://x')",
                     "import smtplib",
                     "import socket; socket.socket()"):
            self.assertTrue(actions.python_needs_approval(code), code)

    def test_computation_still_runs_freely(self):
        """Gating all Python would be safe and useless.

        Himanshu asked to be interrupted only for things that cannot be undone,
        and most Python here is arithmetic on data already in hand.
        """
        import actions
        for code in ("print(sum(range(10)))",
                     "import pandas as pd; print(pd.DataFrame({'a':[1]}).describe())",
                     "import os; print(os.path.isdir('C:/x'))",
                     "import json; print(json.dumps({'a':1}))",
                     "import math; print(math.sqrt(2))"):
            self.assertEqual(actions.python_needs_approval(code), "", code)

    def test_the_reason_is_named(self):
        # A refusal he cannot understand is one he will approve blindly.
        import actions
        reason = actions.python_needs_approval("import subprocess")
        self.assertIn("subprocess", reason)

    def test_the_gate_runs_before_the_code_does(self):
        import action_bus
        body = action_bus.BUS.handler_source("RUN_PYTHON")
        self.assertTrue(body, "RUN_PYTHON has no handler on the bus")
        gate_at = body.index("python_needs_approval")
        run_at = body.index("Running Python code")
        self.assertLess(gate_at, run_at)

    def test_rubbish_input_does_not_crash_the_gate(self):
        import actions
        for code in (None, "", 123, []):
            self.assertEqual(actions.python_needs_approval(code), "")


class ApprovalExpiryTests(unittest.TestCase):
    """An hour-old permission is not permission.

    Every approval is written with expires_at = now + 1 hour, and nothing ever
    read it. Eight were found still 'pending' on 27 Sep 2026, the oldest a
    DELETE_PROJECT_FILE for src/parser.py queued on 22 Sep -- five days earlier.
    Because only the newest pending request is ever offered, saying "approve"
    repeatedly would work backwards through a stack of things Himanshu had long
    since stopped thinking about, deleting files and calendar events on consent
    given days ago to something else.
    """

    def test_the_read_filters_on_expiry(self):
        """An hour-old permission is not permission.

        Asserted against the shim's behaviour rather than a window of its
        source: a by-id lookup was added for power approvals, which pushed the
        strings this used to search for past the end of the window and failed
        a test whose property had not changed.
        """
        from memory.state_db import execute_read
        try:
            from memory.neo4j_store import store
            store.query("RETURN 1")
        except Exception as exc:
            self.skipTest(f"graph unavailable: {exc}")
        marker = "expiry-filter-" + os.urandom(4).hex()
        store.query(
            "MERGE (a:Approval {id: $id}) SET a.action_type='DELETE_FILE', "
            "a.payload='{}', a.status='pending', "
            "a.created_at = toString(datetime()), "
            "a.expires_at = toString(datetime() - duration('PT2H'))",
            id=marker)
        try:
            rows = execute_read(
                "SELECT id, action_type, payload FROM approvals "
                "WHERE status='pending' ORDER BY created_at DESC LIMIT 1")
            self.assertNotIn(marker, [row[0] for row in rows],
                             "an expired approval was still offered")
        finally:
            store.query("MATCH (a:Approval {id: $id}) DELETE a", id=marker)

    def test_expiry_is_written_when_the_approval_is_created(self):
        import inspect
        import actions
        source = inspect.getsource(actions._queue_for_approval)
        self.assertIn("expires_at", source)
        self.assertIn("+1 hour", source)

    def test_only_one_request_is_offered_at_a_time(self):
        """Not a bug by itself, but it is why expired ones piled up unseen."""
        from memory.state_db import execute_read
        try:
            from memory.neo4j_store import store
            store.query("RETURN 1")
        except Exception as exc:
            self.skipTest(f"graph unavailable: {exc}")
        ids = ["one-at-a-time-" + os.urandom(4).hex() for _ in range(3)]
        for approval_id in ids:
            store.query(
                "MERGE (a:Approval {id: $id}) SET a.action_type='DELETE_FILE', "
                "a.payload='{}', a.status='pending', "
                "a.created_at = toString(datetime()), "
                "a.expires_at = toString(datetime() + duration('PT1H'))",
                id=approval_id)
        try:
            rows = execute_read(
                "SELECT id, action_type, payload FROM approvals "
                "WHERE status='pending' ORDER BY created_at DESC LIMIT 1")
            self.assertLessEqual(len(rows), 1)
        finally:
            for approval_id in ids:
                store.query("MATCH (a:Approval {id: $id}) DELETE a",
                            id=approval_id)


if __name__ == "__main__":
    unittest.main()

class MatureControlGuardTests(unittest.TestCase):
    def test_volume_requires_current_turn_intent(self):
        import actions
        self.assertFalse(actions._volume_requested("play study music"))
        self.assertTrue(actions._volume_requested("turn the sound down a little"))
        self.assertTrue(actions._volume_requested("make it quieter"))

    def test_contextual_block_is_a_failed_receipt(self):
        import actions
        self.assertFalse(actions._classify_output("[Blocked] unsolicited change"))

    def test_music_start_attempt_is_bounded_per_task(self):
        import actions
        import queue
        from providers import ytmusic_api
        real = ytmusic_api.play
        calls = []
        try:
            ytmusic_api.play = lambda query: (
                calls.append(query)
                or "[Error] title has not updated yet")
            actions.reset_task_ledger("play study music")
            actions.execute_actions(
                '<ACTION type="MUSIC_PLAY" query="first" />'
                '<ACTION type="MUSIC_PLAY" query="second" />',
                {}, queue.Queue(), ROOT, origin="user")
            self.assertEqual(calls, ["first"])
            results = actions.execute_actions._results
            self.assertEqual(len(results), 2)
            self.assertFalse(results[1]["ok"])
            self.assertTrue(results[1]['not_run'])
            self.assertIn('earlier action is unresolved', results[1]['output'])
        finally:
            ytmusic_api.play = real
