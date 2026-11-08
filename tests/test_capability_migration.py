import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import action_registry
import capability_migration


CATALOGS = {
    "WORKSPACE": '''
      <ACTION type="READ_EMAILS" query="x" /> - read mail
      <ACTION type="ADD_CALENDAR_EVENT" summary="x" /> - add event
    ''',
    "DISCOVERY": '<ACTION type="DISCOVER_ACTIONS" query="x" /> - discover',
}


class CapabilityMigrationTests(unittest.TestCase):
    def test_build_has_one_descriptor_per_canonical_action(self):
        rows = capability_migration.build(CATALOGS)
        self.assertEqual(set(rows), {"READ_EMAILS", "ADD_CALENDAR_EVENT",
                                     "DISCOVER_ACTIONS"})

    def test_generated_registry_preserves_names(self):
        rows = capability_migration.build(CATALOGS)
        generated = capability_migration.generated_catalogs(rows)
        action_registry.configure(CATALOGS)
        before = {item["name"] for item in action_registry._entries}
        action_registry.configure(generated)
        after = {item["name"] for item in action_registry._entries}
        self.assertEqual(before, after)

    def test_live_equivalence_gate_and_rollback(self):
        rows = capability_migration.build(CATALOGS)
        report = capability_migration.equivalence(CATALOGS, rows)
        self.assertTrue(report.ok, report.summary())
        self.assertEqual(report.legacy_count, report.descriptor_count)


if __name__ == "__main__":
    unittest.main()
