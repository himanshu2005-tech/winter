import queue
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import actions


ROOT = Path(__file__).resolve().parents[1]


def execute(text):
    events = queue.Queue()
    actions.reset_task_ledger("direct dashboard delivery test")
    actions.execute_actions(text, {}, events, str(ROOT), origin="user")
    captured = []
    while not events.empty():
        captured.append(events.get_nowait())
    return captured


class TypedDirectDeliveryTests(unittest.TestCase):
    def tearDown(self):
        actions.execute_actions._direct_user_delivery = None
        actions.execute_actions._direct_user_result = None
        actions.execute_actions._direct_user_spoken = None

    def test_dashboard_claim_is_returned_only_after_document_is_enqueued(self):
        delivery = actions.set_direct_user_delivery(
            "The useful detail", "The detail is on your dashboard.",
            surface="dashboard", title="Useful result")
        self.assertIsInstance(delivery, actions.DirectUserDelivery)

        events = queue.Queue()
        spoken = actions.enqueue_direct_user_delivery(events)

        self.assertEqual(spoken, "The detail is on your dashboard.")
        self.assertEqual(events.get_nowait(), ("dashboard_document", {
            "title": "Useful result",
            "language": "",
            "content": "The useful detail",
        }))
        self.assertIsNone(actions.execute_actions._direct_user_delivery)

    def test_failed_dashboard_enqueue_cannot_return_a_visibility_claim(self):
        class BrokenQueue:
            def put(self, _event):
                raise RuntimeError("UI queue unavailable")

        actions.set_direct_user_delivery(
            "The useful detail", "The detail is on your dashboard.",
            surface="dashboard")
        with self.assertRaisesRegex(RuntimeError, "UI queue unavailable"):
            actions.enqueue_direct_user_delivery(BrokenQueue())

    def test_untyped_compatibility_fallback_never_claims_a_dashboard(self):
        actions.execute_actions._direct_user_delivery = None
        actions.execute_actions._direct_user_result = "A legacy extension result"
        actions.execute_actions._direct_user_spoken = None
        events = queue.Queue()

        spoken = actions.enqueue_direct_user_delivery(events)

        self.assertNotIn("dashboard", spoken.casefold())
        self.assertEqual(events.get_nowait(),
                         ("info", "A legacy extension result"))


class ActionDeliveryRoutingTests(unittest.TestCase):
    def tearDown(self):
        actions.execute_actions._direct_user_delivery = None
        actions.execute_actions._direct_user_result = None
        actions.execute_actions._direct_user_spoken = None

    @patch("system_tools.read_screen_text",
           return_value="Zydus Medtech — Graduate Engineer Trainee")
    def test_raw_screen_ocr_stays_internal(self, read_screen):
        events = execute('<ACTION type="READ_SCREEN_TEXT" />')

        read_screen.assert_called_once_with("")
        self.assertIsNone(actions.execute_actions._direct_user_delivery)
        self.assertIsNone(actions.execute_actions._direct_user_result)
        self.assertFalse(any(kind == "dashboard_document"
                             for kind, _payload in events))
        self.assertIn("Zydus Medtech", actions.execute_actions._last_output)

    @patch("health_import.import_health",
           return_value="Imported 14 health records. Resting heart rate: 62 bpm.")
    def test_health_summary_is_an_explicit_dashboard_document(self, imported):
        execute('<ACTION type="IMPORT_HEALTH" path="health.xml" />')

        delivery = actions.execute_actions._direct_user_delivery
        self.assertEqual(delivery.surface, "dashboard")
        self.assertEqual(delivery.title, "Health import summary")
        self.assertIn("Resting heart rate", delivery.content)

        events = queue.Queue()
        spoken = actions.enqueue_direct_user_delivery(events)
        kind, payload = events.get_nowait()
        self.assertEqual(kind, "dashboard_document")
        self.assertIn("Resting heart rate", payload["content"])
        self.assertIn("dashboard", spoken.casefold())

    def test_substantive_local_results_are_typed_for_dashboard_delivery(self):
        cases = (
            (
                patch("dataset_tools.audit", return_value="AUDIT\n- 3 missing values"),
                '<ACTION type="AUDIT_DATASET" path="sample.csv" />',
                "Dataset audit",
            ),
            (
                patch("documents.read_document", return_value="DOCUMENT\nA useful section"),
                '<ACTION type="READ_DOCUMENT" path="notes.pdf" />',
                "notes.pdf",
            ),
            (
                patch("providers.google_api.recent_completed_events", return_value=[
                    {"summary": "Exam", "start": "yesterday"}
                ]),
                '<ACTION type="LIST_RECENT_EVENTS" hours_back="48" />',
                "Recent events",
            ),
        )
        for patcher, action, title in cases:
            with self.subTest(action=action), patcher:
                execute(action)
                delivery = actions.execute_actions._direct_user_delivery
                self.assertIsNotNone(delivery)
                self.assertEqual(delivery.surface, "dashboard")
                self.assertEqual(delivery.title, title)

    @patch("providers.google_api.recent_completed_events", return_value=[])
    def test_empty_recent_events_answer_does_not_open_dashboard(self, recent):
        execute('<ACTION type="LIST_RECENT_EVENTS" hours_back="48" />')
        delivery = actions.execute_actions._direct_user_delivery
        self.assertEqual(delivery.surface, "activity")
        self.assertNotIn("dashboard", delivery.spoken.casefold())

    @patch("health_import.import_health", return_value="Error: invalid export")
    def test_failed_health_import_does_not_claim_a_dashboard_summary(self, imported):
        execute('<ACTION type="IMPORT_HEALTH" path="broken.xml" />')
        self.assertIsNone(actions.execute_actions._direct_user_delivery)


class FalseDashboardLanguageTests(unittest.TestCase):
    def test_generic_runtime_fallbacks_do_not_claim_hidden_dashboard_content(self):
        source = (ROOT / "agent.py").read_text(encoding="utf-8")
        self.assertNotIn(
            "I found the requested information. The full details are on your dashboard.",
            source)
        self.assertNotIn("finished successfully. The result is on your dashboard.",
                         source)
        self.assertIn("A tool receipt and SAY alone are not visible dashboard content.",
                      source)

    def test_model_may_not_invent_a_policy_blocker_mid_task(self):
        source = (ROOT / "agent.py").read_text(encoding="utf-8")
        self.assertIn(
            "unless an actual runtime receipt in this turn says so", source)
        self.assertIn(
            "is still requested work, not a new\n  proactive act", source)


if __name__ == "__main__":
    unittest.main()
