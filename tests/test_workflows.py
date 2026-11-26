
import os
import sys

# Every other test file carries this. Without it these four only passed
# under a runner that happened to put the project root on sys.path, so
# `python tests/test_x.py` failed with ModuleNotFoundError -- a suite that
# depends on how it is invoked is not a suite you can trust.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import json
import os
import tempfile
import unittest

import workflows
from autonomy import action_allowed


def payload(name="Founder outreach"):
    return {
        "name": name,
        "goal": "Find suitable founders and prepare careful internship outreach.",
        "triggers": ["find startup founders", "internship outreach"],
        "steps": [
            {"instruction": "Research and verify suitable founders.",
             "allowed_actions": ["SEARCH_WEB"], "success": "Sources identify each founder."},
            {"instruction": "Draft personalized messages for review.",
             "allowed_actions": [], "success": "Drafts exist."},
            {"instruction": "Send only after the required approval.",
             "allowed_actions": ["SEND_EMAIL"], "success": "Send receipts exist."},
        ],
        "guardrails": ["Never send without confirmation."],
    }


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = self.temp.name

    def tearDown(self):
        self.temp.cleanup()

    def test_create_is_versioned_and_idempotent(self):
        first = workflows.create(self.base, json.dumps(payload()))
        again = workflows.create(self.base, json.dumps(payload()))
        changed = payload(); changed["goal"] += " Carefully."
        second = workflows.create(self.base, changed)
        self.assertIn("wf-founder-outreach-v1", first)
        self.assertIn("no change", again)
        self.assertIn("wf-founder-outreach-v2", second)
        stored = workflows._load(self.base)["workflows"]
        self.assertEqual(len(stored), 2)
        self.assertEqual(stored[0]["status"], "superseded")
        self.assertEqual(stored[1]["supersedes"], stored[0]["workflow_id"])

    def test_unknown_action_is_rejected(self):
        value = payload(); value["steps"][0]["allowed_actions"] = ["BYPASS_POLICY"]
        self.assertTrue(workflows.create(self.base, value).startswith("[Error]"))
        self.assertFalse(os.path.exists(workflows._path(self.base)))

    def test_relevant_workflow_is_advisory(self):
        workflows.create(self.base, payload())
        result = workflows.relevant(self.base, "find Chennai startup founders")
        self.assertIn("never permissions", result)
        self.assertIn("SEARCH_WEB", result)
        allowed, error = action_allowed("UNKNOWN_FROM_WORKFLOW", "user", memory_permits=True)
        self.assertFalse(allowed)
        self.assertIn("blocked by default", error)

    def test_deactivate_preserves_history(self):
        workflows.create(self.base, payload())
        result = workflows.deactivate(self.base, "founder-outreach")
        self.assertIn("history was preserved", result)
        self.assertEqual(workflows._load(self.base)["workflows"][0]["status"], "inactive")

    def test_verified_experience_promotes_only_after_repeat(self):
        class Plan:
            goal = "Research companies and prepare a report"
            steps = ["Search reliable sources", "Read sources", "Write findings"]
            stop_conditions = ["Do not publish externally"]
        receipts = [{"action_type": "SEARCH_WEB", "status": "success"},
                    {"action_type": "READ_FILE", "status": "success"}]
        wid = workflows.observe_verified_success(self.base, "research companies", Plan(), receipts)
        stored = workflows._load(self.base)["workflows"][0]
        self.assertEqual(stored["status"], "candidate")
        self.assertEqual(stored["success_count"], 1)
        self.assertEqual(workflows.observe_verified_success(
            self.base, "research companies", Plan(), receipts), wid)
        stored = workflows._load(self.base)["workflows"][0]
        self.assertEqual(stored["status"], "active")
        self.assertEqual(stored["success_count"], 2)

    def test_failed_receipt_cannot_teach_a_workflow(self):
        class Plan:
            goal = "Research and send"
            steps = ["Search", "Send"]
            stop_conditions = []
        receipts = [{"type": "SEARCH_WEB", "ok": True},
                    {"type": "SEND_EMAIL", "ok": False}]
        self.assertEqual(workflows.observe_verified_success(
            self.base, "research and send", Plan(), receipts), "")
        self.assertFalse(os.path.exists(workflows._path(self.base)))


if __name__ == "__main__":
    unittest.main()
