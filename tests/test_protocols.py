import os
import sys
import time
import unittest
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import protocol_manager
from cognitive_pipeline import route_turn

class ProtocolTests(unittest.TestCase):
    def tearDown(self):
        protocol_manager.deactivate()
        protocol_manager.answer("cancel")

    def test_critical_recommendation_needs_explicit_yes(self):
        question = protocol_manager.recommend("medical_emergency", "critical")
        self.assertIn("112", question)
        self.assertEqual(protocol_manager.context(), "")
        handled, message = protocol_manager.answer("yes")
        self.assertTrue(handled)
        self.assertIn("activated", message)
        self.assertIn("never grants action permission", protocol_manager.context())

    def test_decline_does_not_activate(self):
        protocol_manager.recommend("cyber_incident", "serious")
        self.assertTrue(protocol_manager.answer("no")[0])
        self.assertEqual(protocol_manager.context(), "")

    def test_router_carries_valid_typed_recommendation(self):
        def model(*_args, **_kwargs):
            return '''{"mode_decision":"keep","target_mode":"assistant",
            "interaction":"respond","task_kind":"question","planning_depth":"deep",
            "needs_screen":false,"confidence":1,"retrieval":{"graph":false},
            "protocol_decision":"recommend","target_protocol":"medical_emergency",
            "protocol_severity":"critical","protocol_reason":"current accident"}'''
        route = route_turn("I met with an accident unique protocol test", "assistant", model)
        self.assertEqual(route.target_protocol, "medical_emergency")
        self.assertEqual(route.protocol_severity, "critical")

    def test_router_accepts_routine_operational_protocol(self):
        def model(*_args, **_kwargs):
            return '''{"mode_decision":"keep","target_mode":"assistant",
            "interaction":"respond","task_kind":"conversation","planning_depth":"direct",
            "needs_screen":false,"confidence":1,"retrieval":{"graph":false},
            "protocol_decision":"recommend","target_protocol":"owner_override",
            "protocol_severity":"routine","protocol_reason":"dark room"}'''
        route = route_turn("I need to talk in darkness unique protocol test",
                           "assistant", model)
        self.assertEqual(route.target_protocol, "owner_override")
        self.assertEqual(route.protocol_severity, "routine")

    def test_unknown_protocol_is_rejected(self):
        route = route_turn("unusual urgent event unique invalid protocol", "assistant",
            lambda *_a, **_k: '''{"mode_decision":"keep","target_mode":"assistant",
            "interaction":"respond","task_kind":"question","planning_depth":"deep",
            "needs_screen":false,"confidence":1,"retrieval":{},
            "protocol_decision":"recommend","target_protocol":"anything",
            "protocol_severity":"critical"}''')
        self.assertEqual(route.protocol_decision, "none")

    def test_active_protocol_contains_an_executable_playbook(self):
        protocol_manager.recommend("data_recovery", "serious")
        protocol_manager.answer("yes")
        context = protocol_manager.context()
        self.assertIn("PLAYBOOK:", context)
        self.assertIn("recovery points", context)
        self.assertIn("permission", context.lower())

    def test_execution_prompt_includes_active_protocol_state(self):
        from pathlib import Path
        source = (Path(__file__).resolve().parents[1] / "agent.py").read_text(
            encoding="utf-8")
        self.assertIn("protocol_block = protocol_manager.context()", source)
        self.assertIn('system_prompt += "\\n\\n" + protocol_block', source)

    def test_owner_override_is_temporary_and_does_not_grant_actions(self):
        protocol_manager.recommend("owner_override", "routine")
        handled, message = protocol_manager.answer("yes")
        self.assertTrue(handled)
        self.assertIn("30 minutes", message)
        self.assertTrue(protocol_manager.identity_bypass_active())
        self.assertIn("never grants action permission", protocol_manager.context())
        protocol_manager._active_until = time.time() - 1
        self.assertFalse(protocol_manager.identity_bypass_active())

    def test_lockdown_and_public_space_block_local_disclosure(self):
        for name in ("lockdown", "public_space"):
            protocol_manager.deactivate()
            protocol_manager.recommend(name, "routine")
            self.assertTrue(protocol_manager.answer("yes")[0])
            self.assertTrue(protocol_manager.local_disclosure_blocked())
            self.assertFalse(protocol_manager.identity_bypass_active())

if __name__ == "__main__":
    unittest.main()
