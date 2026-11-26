import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import action_registry
import critic
from loop_guards import DiscoveryGuard, FailureCircuitBreaker
from memory import state_db
from providers import google_api


class AlarmCompatibilityTests(unittest.TestCase):
    @mock.patch.object(state_db, "store")
    def test_literal_id_lookup_returns_requested_projection(self, store):
        store.query.return_value = [{"status": "missed"}]
        rows = state_db.execute_read(
            "SELECT status FROM alarms WHERE id = 'missed_1'")
        self.assertEqual(rows, [("missed",)])
        query = store.query.call_args.args[0]
        kwargs = store.query.call_args.kwargs
        self.assertIn("a.status AS status", query)
        self.assertEqual(kwargs["id"], "missed_1")

    @mock.patch.object(state_db, "store")
    def test_recent_alarm_query_preserves_status_filter_projection_and_limit(self, store):
        store.query.return_value = [{
            "id": "a1", "fire_at": "2026-10-17T08:00:00+05:30",
            "label": "Wake", "status": "missed",
        }]
        rows = state_db.execute_read(
            "SELECT id, fire_at, label, status FROM alarms "
            "WHERE status IN ('fired', 'missed') "
            "ORDER BY fire_at DESC LIMIT 10")
        self.assertEqual(rows[0][3], "missed")
        query = store.query.call_args.args[0]
        kwargs = store.query.call_args.kwargs
        self.assertIn("a.status IN $statuses", query)
        self.assertIn("a.status AS status", query)
        self.assertIn("ORDER BY a.fire_at DESC LIMIT 10", query)
        self.assertEqual(kwargs["statuses"], ["fired", "missed"])


class CalendarReceiptTests(unittest.TestCase):
    def test_created_event_receipt_proves_full_server_stored_interval(self):
        receipt = google_api._created_event_receipt({
            "id": "event-1",
            "summary": "Prepare for emergent intelligence",
            "start": {"dateTime": "2026-10-17T16:30:00+05:30"},
            "end": {"dateTime": "2026-10-17T17:00:00+05:30"},
        })
        self.assertIn("start=2026-10-17T16:30:00+05:30", receipt)
        self.assertIn("end=2026-10-17T17:00:00+05:30", receipt)
        self.assertIn("timezone=+05:30", receipt)
        self.assertIn("duration=30 minutes", receipt)

    def test_receipt_metadata_remains_verifiable_not_phrase_filtered(self):
        criteria = [
            "The receipt shows Asia/Kolkata and a 30-minute duration",
            "Himanshu acknowledges it",
        ]
        observable, unobservable = critic.observable_criteria(criteria)
        self.assertEqual(observable, [criteria[0]])
        self.assertEqual(unobservable, [criteria[1]])


class LoopGuardTests(unittest.TestCase):
    def test_discovery_deduplicates_same_capabilities_across_paraphrases(self):
        guard = DiscoveryGuard(max_extensions=2)
        first = guard.check(("LIST_ALARMS", "SET_ALARM"))
        second = guard.check(("LIST_ALARMS", "SET_ALARM"))
        self.assertTrue(first.execute)
        self.assertTrue(first.grant_extension)
        self.assertFalse(second.execute)
        self.assertTrue(second.duplicate)

    def test_different_discovery_result_sets_are_not_merged(self):
        guard = DiscoveryGuard(max_extensions=2)
        self.assertTrue(guard.check(("LIST_ALARMS",)).execute)
        self.assertTrue(guard.check(("LIST_CALENDAR_EVENTS",)).execute)

    def test_action_is_blocked_only_after_same_failure_twice(self):
        guard = FailureCircuitBreaker(threshold=2)
        failure = {"type": "LIST_ALARMS", "ok": False,
                   "output": "[Error] tuple index out of range"}
        guard.record([failure])
        self.assertEqual(guard.reason_for(["LIST_ALARMS"]), "")
        guard.record([failure])
        self.assertIn("already failed 2 times", guard.reason_for(["LIST_ALARMS"]))
        self.assertEqual(guard.reason_for(["READ_CALENDAR"]), "")

    def test_corrected_payload_is_not_blocked_by_an_earlier_failure(self):
        guard = FailureCircuitBreaker(threshold=2)
        failure = {"type": "RUNTIME_APPLY", "ok": False,
                   "output": "[Error] missing required attribute: project"}
        malformed = '<ACTION type="RUNTIME_APPLY" adapter="blender" />'
        corrected = ('<ACTION type="RUNTIME_APPLY" adapter="blender" '
                     'project="network">import bpy\nprint("ok")</ACTION>')
        guard.record([failure], malformed)
        guard.record([failure], malformed)
        self.assertIn("already failed 2 times", guard.reason_for(malformed))
        self.assertEqual(guard.reason_for(corrected), "")

    def test_different_shell_commands_do_not_share_one_breaker(self):
        guard = FailureCircuitBreaker(threshold=2)
        failure = {"type": "RUN_COMMAND", "ok": False,
                   "output": "[Error] Command failed with exit code 1."}
        broken = '<ACTION type="RUN_COMMAND" cmd="broken command" />'
        probe = '<ACTION type="RUN_COMMAND" cmd="echo test" />'
        guard.record([failure], broken)
        guard.record([failure], broken)
        self.assertEqual(guard.reason_for(probe), "")

    @mock.patch.object(action_registry, "search")
    def test_discovery_signature_uses_callable_set(self, search):
        search.return_value = [
            {"name": "SET_ALARM", "catalog": "MACHINE"},
            {"name": "LIST_ALARMS", "catalog": "MACHINE"},
            {"name": "DISCOVER_ACTIONS", "catalog": action_registry.DISCOVERY_CATALOG},
        ]
        self.assertEqual(
            action_registry.discovery_signature(".", "alarm details"),
            ("LIST_ALARMS", "SET_ALARM"),
        )


if __name__ == "__main__":
    unittest.main()
