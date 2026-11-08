# -*- coding: utf-8 -*-
"""No test may leave a live approval behind.

Seventy-two Approval nodes had accumulated in the live graph, forty-four still
marked pending, almost all of them from two tests that dispatch a genuinely
gated action to prove the gate holds. Both mocked the provider, so nothing was
ever shut down or trashed. What neither accounted for is that reaching the gate
writes a record.

The consequence is not cosmetic. Settling an approval takes the newest pending
record, so for the hour before each one expired, a bare "approve" from
Himanshu -- meaning something else entirely -- could have fired a SHUTDOWN that
a test suite had queued. Only the one-hour expiry kept that theoretical.

This test is the ratchet. It asserts the property rather than a count, so it
fails for whichever test starts writing approvals next, not only the two that
were fixed.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _store():
    from memory.neo4j_store import store
    return store


class NoLiveApprovalsFromTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            _store().query("RETURN 1")
        except Exception as exc:
            raise unittest.SkipTest(f"graph unavailable: {exc}")

    def test_no_pending_approval_is_still_live(self):
        """A pending-and-unexpired approval is one a stray "approve" can fire."""
        import datetime
        now = datetime.datetime.now().isoformat()
        rows = _store().query(
            "MATCH (a:Approval) WHERE a.status='pending' "
            "RETURN a.action_type AS type, a.created_at AS created, "
            "a.expires_at AS expires")
        live = [row for row in rows
                if not row["expires"] or str(row["expires"]) > now]
        described = [f"{row['type']} (created {row['created']})"
                     for row in live]
        self.assertEqual(
            described, [],
            "a live pending approval exists; a bare 'approve' would settle "
            "the newest of these")

    def test_no_test_fixture_payloads_are_pending(self):
        """The residue was recognisable: file_id "abc" is not a real id."""
        rows = _store().query(
            "MATCH (a:Approval) WHERE a.status='pending' "
            "RETURN a.action_type AS type, a.payload AS payload")
        fixtures = [row for row in rows
                    if '"abc"' in str(row["payload"] or "")]
        self.assertEqual(
            [row["type"] for row in fixtures], [],
            "test fixture approvals are pending in the live store; wrap the "
            "dispatch in approval_isolation.no_approval_writes()")


class TheIsolationHelperWorks(unittest.TestCase):
    """The helper must keep the gate real while writing nothing."""

    def test_the_gate_still_refuses(self):
        from queue import Queue
        import actions
        import approval_isolation
        with approval_isolation.no_approval_writes():
            actions.execute_actions(
                '<ACTION type="DRIVE_TRASH" file_id="isolation-check" />',
                {}, Queue(), ".")
        self.assertIn("approval",
                      actions.execute_actions._last_output.lower())

    def test_the_attempted_write_is_still_observable(self):
        from queue import Queue
        import actions
        import approval_isolation
        with approval_isolation.no_approval_writes() as attempted:
            actions.execute_actions(
                '<ACTION type="DRIVE_TRASH" file_id="isolation-check" />',
                {}, Queue(), ".")
        self.assertIn("DRIVE_TRASH",
                      approval_isolation.queued_action_types(attempted))

    def test_nothing_reached_the_real_store(self):
        from queue import Queue
        import actions
        import approval_isolation
        try:
            _store().query("RETURN 1")
        except Exception as exc:
            raise unittest.SkipTest(f"graph unavailable: {exc}")
        with approval_isolation.no_approval_writes():
            actions.execute_actions(
                '<ACTION type="DRIVE_TRASH" file_id="isolation-sentinel" />',
                {}, Queue(), ".")
        rows = _store().query(
            "MATCH (a:Approval) WHERE a.payload CONTAINS "
            "'isolation-sentinel' RETURN a.id AS id")
        self.assertEqual(rows, [], "the isolated write reached the real store")


if __name__ == "__main__":
    unittest.main()
