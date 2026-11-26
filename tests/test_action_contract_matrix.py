"""Generated contract checks across VANTA's complete action surface."""
import os
import re
import sys
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import action_schema
import actions
import autonomy

def _valid_case(spec, group=None):
    attrs = {"type": "PROBE"}
    for name in spec.get("requires", []):
        attrs[name] = "1"
    if group:
        for name in group:
            if name != "__body__":
                attrs[name] = "1"
    return attrs, bool(spec.get("body"))

class SchemaMatrixTests(unittest.TestCase):
    def test_every_declared_schema_accepts_a_constructed_valid_case(self):
        for action, spec in action_schema.ACTION_SPECS.items():
            for group in spec.get("any_of") or [None]:
                attrs, body = _valid_case(spec, group)
                with self.subTest(action=action, group=group):
                    self.assertIsNone(action_schema.validate(
                        action, attrs, has_body=body))

    def test_every_required_attribute_is_actually_required(self):
        for action, spec in action_schema.ACTION_SPECS.items():
            for required in spec.get("requires", []):
                attrs, body = _valid_case(
                    spec, (spec.get("any_of") or [None])[0])
                attrs.pop(required, None)
                with self.subTest(action=action, required=required):
                    self.assertIn(required, action_schema.validate(
                        action, attrs, has_body=body))

    def test_every_any_of_contract_rejects_no_options(self):
        for action, spec in action_schema.ACTION_SPECS.items():
            if not spec.get("any_of"):
                continue
            attrs, _ = _valid_case(spec)
            with self.subTest(action=action):
                self.assertTrue(action_schema.validate(
                    action, attrs, has_body=False))

    def test_every_schema_rejects_a_misspelled_attribute(self):
        for action, spec in action_schema.ACTION_SPECS.items():
            attrs, body = _valid_case(
                spec, (spec.get("any_of") or [None])[0])
            attrs["definitely_misspelled"] = "x"
            with self.subTest(action=action):
                self.assertIn("does not take", action_schema.validate(
                    action, attrs, has_body=body))

class PolicyMatrixTests(unittest.TestCase):
    class _Mode:
        @staticmethod
        def action_allowed(_action):
            return True, None

    def setUp(self):
        self.mode = mock.patch(
            "mode_manager.get_mode_manager", return_value=self._Mode())
        self.mode.start()

    def tearDown(self):
        self.mode.stop()

    def test_every_registered_action_is_user_callable(self):
        for action in autonomy.KNOWN_ACTIONS:
            with self.subTest(action=action):
                self.assertTrue(autonomy.action_allowed(action, "user")[0])

    def test_proactive_policy_matches_block_registry_exactly(self):
        for action in autonomy.KNOWN_ACTIONS:
            allowed, _ = autonomy.action_allowed(action, "proactive")
            with self.subTest(action=action):
                self.assertEqual(
                    allowed, action not in autonomy.PROACTIVE_BLOCKED_ACTIONS)

    def test_every_policy_set_names_only_registered_actions(self):
        for policy_set in (
                autonomy.PROACTIVE_BLOCKED_ACTIONS,
                autonomy.CONFIRMATION_REQUIRED_ACTIONS,
                actions.SIDE_EFFECT_ACTIONS):
            self.assertFalse(set(policy_set) - set(autonomy.KNOWN_ACTIONS))

    def test_unknown_actions_are_default_denied(self):
        self.assertFalse(
            autonomy.action_allowed("UNREGISTERED_SIDE_EFFECT", "user")[0])

class DispatcherMatrixTests(unittest.TestCase):
    def test_every_registered_action_has_a_dispatch_path(self):
        """Asked of the table rather than scraped out of the source.

        This used to re-implement six regexes over actions.py and agent.py,
        because dispatch was an if/elif ladder and that was the only way to
        guess at its contents. The action bus can be asked, so it is asked --
        and the answer is exact rather than a guess that happened to agree.
        """
        import integrity
        handled = integrity.live_executable_names()
        # Older spellings normalised to a live name before dispatch, and the
        # two capabilities that run inside the agent loop rather than here.
        import capabilities
        handled |= {"MOUSE_MOVE", "MOUSE_DRAG", "DOUBLECLICK", "RIGHTCLICK",
                    "ADD_ALARM"}
        handled |= {name for name, cap in capabilities.CAPABILITIES.items()
                    if cap.runs_in != capabilities.RUNS_IN_DISPATCHER}
        unreachable = sorted(set(autonomy.KNOWN_ACTIONS) - handled)
        self.assertEqual(unreachable, [],
                         f"{len(unreachable)} authorized action(s) cannot be "
                         f"dispatched by any path")

    def test_receipt_classifier_rejects_failure_like_success_text(self):
        for receipt in (
                "[Error] failed", "[Blocked] refused", "nothing is playing",
                "could not open", "needs approval"):
            with self.subTest(receipt=receipt):
                self.assertFalse(actions._classify_output(receipt))
        self.assertTrue(actions._classify_output("[Success] completed"))

if __name__ == "__main__":
    unittest.main()
