import unittest
from unittest import mock

import action_experience


class ActionExperienceTests(unittest.TestCase):
    @mock.patch("memory.neo4j_store.store.query")
    def test_verified_success_is_aggregated_without_raw_command(self, query):
        count = action_experience.record_results(
            "build a neural network scene in Blender",
            [{"type": "RUN_BLENDER_SCRIPT", "ok": True,
              "output": "[Success] C:\\secret\\scene.py token=private"}],
            turn_id="turn-1",
        )
        self.assertEqual(count, 1)
        cypher = query.call_args.args[0]
        params = query.call_args.kwargs
        self.assertIn("ActionExperience", cypher)
        self.assertEqual(params["action"], "RUN_BLENDER_SCRIPT")
        self.assertNotIn("output", params)
        self.assertNotIn("secret", repr(params))
        self.assertFalse(params.get("permission_grant", False))

    @mock.patch("memory.neo4j_store.store.query")
    def test_approval_and_network_failures_are_not_learned(self, query):
        written = action_experience.record_results(
            "send the project",
            [
                {"type": "SEND_EMAIL", "ok": False,
                 "output": "Approval required before sending."},
                {"type": "DRIVE_UPLOAD", "ok": False,
                 "output": "Network temporarily unavailable."},
            ],
            turn_id="turn-2",
        )
        self.assertEqual(written, 0)
        query.assert_not_called()

    @mock.patch("action_experience.list_relevant")
    def test_prompt_prefers_working_route_and_preserves_policy(self, relevant):
        relevant.return_value = [{
            "action_type": "RUN_BLENDER_SCRIPT", "success_count": 2,
            "failure_count": 0,
        }]
        block = action_experience.prompt_block("edit the Blender model")
        self.assertIn("Prefer RUN_BLENDER_SCRIPT", block)
        self.assertIn("not a permission grant", block)
        self.assertIn("Do not repeat exploratory alternatives", block)

    @mock.patch("action_experience.list_relevant")
    def test_repeated_deterministic_failure_becomes_caution(self, relevant):
        relevant.return_value = [{
            "action_type": "RUNTIME_APPLY", "success_count": 0,
            "failure_count": 3, "failure_class": "runtime_api_incompatible",
        }]
        block = action_experience.prompt_block("edit the Blender model")
        self.assertIn("Avoid retrying RUNTIME_APPLY unchanged", block)
        self.assertIn("runtime_api_incompatible", block)

    def test_topic_terms_drop_urls_emails_and_high_entropy_ids(self):
        terms = action_experience._topic_terms(
            "upload https://example.com/a for me@example.com "
            "abcdef0123456789abcdef0123456789 Blender scene")
        joined = " ".join(terms)
        self.assertNotIn("example", joined)
        self.assertNotIn("abcdef", joined)
        self.assertIn("blender", terms)


if __name__ == "__main__":
    unittest.main()
