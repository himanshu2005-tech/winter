import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import email_memory


MAIL = [
    {"id": "important-1", "sender": "University", "subject": "Exam moved",
     "snippet": "The examination is now on Friday."},
    {"id": "noise-1", "sender": "Shop", "subject": "Sale",
     "snippet": "Buy now."},
]


class EmailMemoryTests(unittest.TestCase):
    def test_every_message_is_written_regardless_of_triage(self):
        with patch.object(email_memory.store, "query") as query, \
                patch.object(email_memory, "add_edge") as add_edge:
            count = email_memory.remember_emails(MAIL, {
                "severity": "important", "reason": "schedule changed",
                "important_ids": ["important-1"],
            })
        self.assertEqual(count, 2)
        self.assertEqual(query.call_count, 2)
        self.assertEqual(add_edge.call_count, 2)
        source_ids = {call.kwargs["source_id"] for call in query.call_args_list}
        self.assertEqual(source_ids, {"gmail:important-1", "gmail:noise-1"})

    def test_no_selection_still_writes_everything(self):
        with patch.object(email_memory.store, "query") as query, \
                patch.object(email_memory, "add_edge"):
            self.assertEqual(email_memory.remember_emails(
                MAIL, {"severity": "none", "important_ids": []}), 2)
        self.assertEqual(query.call_count, 2)

    def test_addresses_are_stored_as_graph_facts(self):
        mail = [{"id": "m1", "sender": "Madhu <madhu@example.com>",
                 "to": "Himanshu <himanshu@example.com>",
                 "subject": "Project", "snippet": "CC a.friend@example.org"}]
        with patch.object(email_memory.store, "query"), \
                patch.object(email_memory, "add_edge") as add_edge:
            email_memory.remember_emails(mail, {})
        predicates = [call.args[1] for call in add_edge.call_args_list]
        objects = [call.args[2] for call in add_edge.call_args_list]
        self.assertIn("RECEIVED_EMAIL", predicates)
        self.assertGreaterEqual(predicates.count("MENTIONS_EMAIL_ADDRESS"), 3)
        self.assertIn("PRIMARY_EMAIL", predicates)
        self.assertIn("madhu@example.com", objects)


if __name__ == "__main__":
    unittest.main()
