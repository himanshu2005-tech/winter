# -*- coding: utf-8 -*-
"""
The equivalence gate: does the generated inventory say what the sources say?

Migrating 263 actions by hand would be enormous and would introduce exactly
the errors the descriptor layer exists to eliminate, so the inventory is
generated from the five places that already hold the facts. A generated view
that disagrees with its sources is worse than no view at all, because it looks
authoritative, so nothing may depend on it until every facet matches.

These are the nine promises made before migration, as tests.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import action_registry
import action_schema
import agent
import autonomy
import capabilities
import capability_migration
import integrity
import mode_manager

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _actions_source():
    with open(os.path.join(BASE_DIR, "actions.py"), encoding="utf-8",
              errors="replace") as handle:
        return handle.read()


class EquivalenceGate(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.inventory = capability_migration.build_live()
        cls.executable = integrity.executable_names(_actions_source())
        cls.mode_policies = {}
        for policy in mode_manager.POLICIES.values():
            blocked = set(policy.blocked_actions)
            blocked |= capabilities.mode_blocked_names(policy.label)
            cls.mode_policies[policy.label.lower()] = blocked
        cls.problems = capability_migration.equivalence_report(
            cls.inventory,
            catalogs=agent.ACTION_CATALOGS,
            schema_specs=action_schema.ACTION_SPECS,
            authorized=set(autonomy.KNOWN_ACTIONS),
            confirmation_required=set(autonomy.CONFIRMATION_REQUIRED_ACTIONS),
            mode_policies=cls.mode_policies,
            executable=cls.executable,
        )

    def _assert_clean(self, key):
        self.assertEqual(self.problems.get(key, []), [],
                         f"{key}: {self.problems.get(key, [])[:12]}")

    # --- the nine promises ------------------------------------------- #

    def test_every_action_is_represented_exactly_once(self):
        self._assert_clean("duplicate_representation")
        self._assert_clean("missing_from_inventory")

    def test_model_facing_syntax_is_preserved(self):
        self._assert_clean("syntax_lost")

    def test_schemas_are_identical(self):
        self._assert_clean("schema_differs")

    def test_authorization_is_equal_or_stricter(self):
        self._assert_clean("authorization_differs")
        self._assert_clean("authorization_loosened")

    def test_confirmation_policy_is_identical(self):
        self._assert_clean("confirmation_differs")

    def test_handler_selection_is_preserved(self):
        self._assert_clean("executor_lost")

    def test_aliases_normalise_identically(self):
        """Retrieval vocabulary survives the round trip."""
        action_registry.configure(agent.ACTION_CATALOGS)
        for entry in action_registry._entries:
            record = self.inventory.get(entry["name"])
            if record is None or record["declared"]:
                continue
            for token in entry.get("alias_tokens", ()):
                with self.subTest(action=entry["name"], token=token):
                    self.assertIn(token, record["aliases"])

    def test_prompt_size_remains_bounded(self):
        """The inventory is retrieval-side metadata, never prompt content."""
        action_registry.configure(agent.ACTION_CATALOGS)
        rendered = action_registry.render(action_registry.search(
            BASE_DIR, "remind me at 4:30 to call madhu", None, 12, "user"))
        # Twelve entries of catalogue text, not 263. The exact size is not the
        # property worth pinning; independence from registry size is.
        self.assertLess(len(rendered), 20000)
        self.assertGreater(len(self.inventory), 200)

    def test_latency_does_not_materially_regress(self):
        import time
        action_registry.configure(agent.ACTION_CATALOGS)
        start = time.perf_counter()
        for _ in range(10):
            action_registry.search(BASE_DIR, "remind me at 4:30", None, 12,
                                   "user")
        elapsed = (time.perf_counter() - start) / 10
        self.assertLess(elapsed, 0.25, f"{elapsed * 1000:.0f} ms per search")

    # --- the inventory itself ----------------------------------------- #

    def test_declared_capabilities_override_their_generated_record(self):
        for name in capabilities.names():
            with self.subTest(action=name):
                record = self.inventory[name]
                self.assertTrue(record["declared"])
                self.assertTrue(record["executor"])

    def test_every_action_has_an_authorization_class(self):
        for name, record in self.inventory.items():
            with self.subTest(action=name):
                self.assertIn(record["authorization"],
                              capabilities.AUTHORIZATION_CLASSES)

    def test_every_action_has_a_verification_strategy(self):
        """The facet 261 of 263 actions were missing."""
        for name, record in self.inventory.items():
            with self.subTest(action=name):
                self.assertIn(record["verification"],
                              capabilities.VERIFICATION_STRATEGIES)

    def test_external_sends_are_not_proactive(self):
        """Authorization may only tighten. A background send is the case."""
        for name, record in self.inventory.items():
            if record["authorization"] != capabilities.EXTERNAL:
                continue
            if name not in autonomy.PROACTIVE_BLOCKED_ACTIONS:
                continue
            with self.subTest(action=name):
                self.assertEqual(record["origins"], ("user",))

    def test_tutorial_example_is_not_an_action(self):
        self.assertNotIn("WORD_COUNT", self.inventory)

    def test_stats_are_reported(self):
        stats = capability_migration.stats(self.inventory)
        self.assertEqual(stats["total"],
                         stats["declared"] + stats["generated"])
        self.assertGreater(stats["documented"], 200)


if __name__ == "__main__":
    unittest.main()
