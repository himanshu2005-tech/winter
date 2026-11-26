import os
import sys
import unittest
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import health_import
from providers import research_api


class SearchEvidenceTests(unittest.TestCase):
    def test_mismatched_travel_dates_are_not_verified(self):
        data = {"query": "HYD to MAA lowest fare October 25 2026",
                "results": [{"title": "Fare for October 29, 2026",
                             "url": "https://example.test", "content": "$49",
                             "score": .8}]}
        evidence = research_api.assess_evidence(data)
        self.assertFalse(evidence["verified_for_requested_date"])
        self.assertTrue(evidence["warnings"])

    def test_matching_date_is_visible_to_the_model(self):
        data = {"query": "fare 2026-10-25",
                "results": [{"title": "Fare on 2026-10-25",
                             "url": "https://example.test", "content": "INR 5000",
                             "score": .9}]}
        evidence = research_api.assess_evidence(data)
        self.assertTrue(evidence['date_mentions_found'])
        self.assertFalse(evidence['verified_for_requested_date'])
        self.assertIn('Independently verified: False',
                      research_api.format_for_model(data))


class HealthCoverageTests(unittest.TestCase):
    def test_sparse_series_exposes_coverage(self):
        summary, _ = health_import._summarise({
            "2026-09-01": {"sleep_hours": 2.0},
            "2026-09-30": {"sleep_hours": 3.0}})
        self.assertLess(summary["sleep_hours"]["recent_coverage"], .1)
        self.assertLess(summary["sleep_hours"]["overall_coverage"], .1)

    def test_monthly_notes_state_measurement_days(self):
        notes = dict(health_import._monthly_notes({
            "2026-09-01": {"sleep_hours": 2.0},
            "2026-09-03": {"sleep_hours": 3.0}}))
        self.assertIn("2/30 days", notes["2026-09"])
        self.assertIn("coverage", notes["2026-09"])

    def test_graph_health_writer_is_idempotent_by_design(self):
        import inspect
        from memory.neo4j_store import GraphStore
        source = inspect.getsource(GraphStore.upsert_health_observations)
        self.assertIn("MERGE (o:HealthObservation {id:row.id})", source)
        self.assertIn("HAS_HEALTH_OBSERVATION", source)

    def test_apple_export_is_discovered_inside_a_folder(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "export.xml")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write('<?xml version="1.0"?><HealthData>'
                             '<Record type="HKQuantityTypeIdentifierStepCount" '
                             'startDate="2026-10-10 10:00:00 +0530" value="123"/>'
                             '</HealthData>')
            result = health_import.import_health(".", folder, store_facts=False)
            self.assertIn("Apple Health export", result)
            self.assertIn("123.0", result)

    def test_multiple_apple_exports_require_an_exact_path(self):
        with tempfile.TemporaryDirectory() as folder:
            for child in ("one", "two"):
                os.makedirs(os.path.join(folder, child))
                with open(os.path.join(folder, child, "export.xml"), "w") as handle:
                    handle.write("<HealthData />")
            result = health_import.import_health(".", folder, store_facts=False)
            self.assertIn("More than one Apple Health", result)


if __name__ == "__main__":
    unittest.main()
