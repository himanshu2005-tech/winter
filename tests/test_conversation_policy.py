import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from conversation_policy import BASE_CONVERSATION_CONTRACT, turn_guidance
from memory.consolidator import should_consolidate_personal


class ConversationPolicyTests(unittest.TestCase):
    def test_contract_rejects_question_answer_posture(self):
        self.assertIn("not a question-answer bot", BASE_CONVERSATION_CONTRACT)
        self.assertIn("Conversation is not an interview", BASE_CONVERSATION_CONTRACT)
        self.assertIn("I am here for you", BASE_CONVERSATION_CONTRACT)

    def test_consecutive_question_gets_no_question_guidance(self):
        chat = "User: I feel alone\nAssistant: What happened?\nUser: Family stuff"
        guidance = turn_guidance(chat, "conversation")
        self.assertIn("Do not end this reply with another question", guidance)

    def test_non_conversation_has_no_social_cadence_override(self):
        self.assertEqual(turn_guidance("Assistant: Ready?", "action"), "")

    def test_transient_needs_are_not_queued_as_durable_facts(self):
        self.assertFalse(should_consolidate_personal("I need reassurance right now"))
        self.assertFalse(should_consolidate_personal("I want to be alone tonight"))
        self.assertFalse(should_consolidate_personal("I am heartbroken"))

    def test_durable_goal_still_reaches_consolidation(self):
        self.assertTrue(should_consolidate_personal("I want to build a company"))

    def test_named_partner_relationship_reaches_consolidation(self):
        self.assertTrue(should_consolidate_personal("Madhu is my girlfriend"))
        self.assertTrue(should_consolidate_personal("Madhu is my gf"))

    def test_a_provenance_question_is_not_reingested_as_a_new_fact(self):
        self.assertFalse(should_consolidate_personal(
            "how did you know I speak Telugu"))
        self.assertFalse(should_consolidate_personal(
            "do you remember that I speak Telugu?"))

    def test_an_explicit_language_statement_is_durable(self):
        self.assertTrue(should_consolidate_personal("I speak Telugu"))


if __name__ == "__main__":
    unittest.main()
