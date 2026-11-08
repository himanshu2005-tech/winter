import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from queue import Queue
from unittest.mock import patch
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import actions
import approval_isolation
from memory.contacts import resolve_contact_phone
from proactive_learning import may_ask, record_answer_if_pending, record_question, context_summary


class ContactAndLearningTests(unittest.TestCase):
    @patch("providers.whatsapp.send_message", return_value="WhatsApp message sent to Madhu: hello")
    def test_direct_user_message_waits_for_final_confirmation(self, send):
        queue = Queue()
        with approval_isolation.no_approval_writes() as attempted:
            actions.execute_actions(
                '<ACTION type="SEND_WHATSAPP" contact="Madhu" message="hello" />',
                {}, queue, str(Path.cwd()), origin="user",
            )
        send.assert_not_called()
        self.assertIn("Approval Required", actions.execute_actions._last_output)
        self.assertIn("SEND_WHATSAPP",
                      approval_isolation.queued_action_types(attempted))

    @patch("providers.whatsapp.send_message")
    def test_proactive_message_is_still_blocked(self, send):
        queue = Queue()
        actions.execute_actions(
            '<ACTION type="SEND_WHATSAPP" contact="Madhu" message="hello" />',
            {}, queue, str(Path.cwd()), origin="proactive",
        )
        send.assert_not_called()
        self.assertIn("Autonomy Policy", actions.execute_actions._last_output)

    @patch("memory.contacts.get_all_contacts", return_value={"ambadas": "+91111"})
    @patch("memory.contacts.get_current_edges", return_value=[
        ("Himanshu", "HAS_FATHER", "Ambadas"),
        ("Ambadas", "HAS_ALIAS", "Pappa"),
    ])
    def test_family_role_and_alias_resolve_to_contact(self, _edges, _contacts):
        self.assertEqual(resolve_contact_phone("father"), "+91111")
        self.assertEqual(resolve_contact_phone("pappa"), "+91111")

    @patch("memory.contacts.lookup_contacts", return_value=[
        {"name": "Ambadas", "phone": "+91111", "found": True},
        {"name": "Unknown", "phone": None, "found": False},
    ])
    def test_lookup_action_returns_requested_contacts_only(self, lookup):
        queue = Queue()
        actions.execute_actions(
            '<ACTION type="LOOKUP_CONTACTS" names="Ambadas, Unknown" />',
            {}, queue, str(Path.cwd()), origin="user",
        )
        lookup.assert_called_once_with(["Ambadas", "Unknown"])
        self.assertIn("Ambadas: +91111", actions.execute_actions._direct_user_result)
        self.assertIn("Not found: Unknown", actions.execute_actions._direct_user_result)
        self.assertNotIn("Approval Required", actions.execute_actions._last_output)

    def test_question_ledger_blocks_frequency_and_semantic_repeats(self):
        with tempfile.TemporaryDirectory() as temp:
            now = datetime(2026, 9, 20, 12, 0)
            question = "How will Outrun generate revenue?"
            self.assertTrue(may_ask(temp, question, now)[0])
            record_question(temp, question, now)
            self.assertFalse(may_ask(temp, "What is Outrun's revenue plan?", now + timedelta(hours=1))[0])
            self.assertTrue(record_answer_if_pending(temp, "Via subscriptions", now + timedelta(minutes=2)))
            summary = context_summary(temp)
            self.assertIn("Via subscriptions", summary)
            self.assertFalse(may_ask(temp, "How will Outrun generate revenue?", now + timedelta(days=2))[0])

    def test_family_language_is_eligible_for_graph_consolidation(self):
        source = (Path(__file__).resolve().parents[1] / "memory" / "consolidator.py").read_text(encoding="utf-8")
        self.assertIn("HAS_FATHER", source)
        self.assertIn("HAS_ALIAS", source)
        # A written fact is still read back before it counts. This used to
        # assert the raise that carried that message, but the raise threw away
        # every remaining fact in the batch; the check itself survives.
        self.assertIn("verified = any(", source)
        self.assertIn("father|mother|sister|brother|family", source)


if __name__ == "__main__":
    unittest.main()
