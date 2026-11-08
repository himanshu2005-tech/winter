# -*- coding: utf-8 -*-
"""An approved power request must be findable after it is approved.

Himanshu said goodnight, VANTA proposed SHUTDOWN, he replied "approve", and
the approval settled correctly:

    [Approval] Settled mid-task via text: approve

Then:

    SHUTDOWN: [Approval Required] Power control was not executed.
              Approve the pending VANTA power request first.
    [Approved] SHUTDOWN failed after approval

Power actions verify consent properly -- they will not accept a model-authored
approved="true" and instead look up the opaque row id that a human settled:

    SELECT id FROM approvals WHERE id=? AND action_type=? AND status='approved'

But the SQL-to-graph shim ignores that WHERE clause. Every read of `approvals`
returned "the newest row whose status is pending", so once an approval was
settled -- the exact moment the lookup happens -- its status was 'approved'
and the query found nothing. SHUTDOWN and RESTART could therefore never run,
no matter how they were approved: the approval was consumed and the action
refused, twice in one evening.

The shim honouring only the query it was written for is the whole bug. A
compatibility layer that silently answers a different question than the one
asked is worse than one that refuses.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _store():
    from memory.neo4j_store import store
    return store


class ApprovalLookup(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            _store().query("RETURN 1")
        except Exception as exc:
            raise unittest.SkipTest(f"graph unavailable: {exc}")

    def setUp(self):
        self.approval_id = "test-lookup-" + os.urandom(4).hex()
        _store().query(
            "MERGE (a:Approval {id: $id}) SET a.action_type='SHUTDOWN', "
            "a.payload='{}', a.status='approved', "
            "a.created_at = toString(datetime()), "
            "a.expires_at = toString(datetime() + duration('PT1H'))",
            id=self.approval_id)

    def tearDown(self):
        _store().query("MATCH (a:Approval {id: $id}) DELETE a",
                       id=self.approval_id)

    def test_an_approved_row_is_found_by_its_id(self):
        """The lookup the power gate actually performs."""
        from memory.state_db import execute_read
        rows = execute_read(
            "SELECT id FROM approvals WHERE id=? AND action_type=? "
            "AND status='approved' LIMIT 1",
            (self.approval_id, "SHUTDOWN"))
        self.assertTrue(rows, "an approved power request was not findable")
        self.assertEqual(rows[0][0], self.approval_id)

    def test_the_wrong_action_type_does_not_match(self):
        """An approval for one action must not authorise another."""
        from memory.state_db import execute_read
        rows = execute_read(
            "SELECT id FROM approvals WHERE id=? AND action_type=? "
            "AND status='approved' LIMIT 1",
            (self.approval_id, "RESTART"))
        self.assertEqual(rows, [])

    def test_an_unknown_id_does_not_match(self):
        from memory.state_db import execute_read
        rows = execute_read(
            "SELECT id FROM approvals WHERE id=? AND action_type=? "
            "AND status='approved' LIMIT 1",
            ("no-such-approval", "SHUTDOWN"))
        self.assertEqual(rows, [])

    def test_a_pending_row_is_not_treated_as_approved(self):
        """Default-deny: pending is not consent."""
        from memory.state_db import execute_read
        _store().query(
            "MATCH (a:Approval {id: $id}) SET a.status='pending'",
            id=self.approval_id)
        rows = execute_read(
            "SELECT id FROM approvals WHERE id=? AND action_type=? "
            "AND status='approved' LIMIT 1",
            (self.approval_id, "SHUTDOWN"))
        self.assertEqual(rows, [])

    def test_an_expired_approval_is_not_accepted(self):
        from memory.state_db import execute_read
        _store().query(
            "MATCH (a:Approval {id: $id}) SET a.expires_at = "
            "toString(datetime() - duration('PT2H'))", id=self.approval_id)
        rows = execute_read(
            "SELECT id FROM approvals WHERE id=? AND action_type=? "
            "AND status='approved' LIMIT 1",
            (self.approval_id, "SHUTDOWN"))
        self.assertEqual(rows, [], "an expired approval was accepted")

    def test_the_newest_pending_lookup_still_works(self):
        """The query the approval reflex uses must keep working."""
        from memory.state_db import execute_read
        _store().query(
            "MATCH (a:Approval {id: $id}) SET a.status='pending'",
            id=self.approval_id)
        rows = execute_read(
            "SELECT id, action_type, payload FROM approvals "
            "WHERE status='pending' ORDER BY created_at DESC LIMIT 1")
        self.assertTrue(rows)
        self.assertEqual(len(rows[0]), 3)


if __name__ == "__main__":
    unittest.main()
