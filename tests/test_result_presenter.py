import unittest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from result_presenter import present


class ResultPresenterTests(unittest.TestCase):
    def test_web_result_omits_scraped_bodies_and_keeps_sources(self):
        raw = """[Evidence quality]
Verified for requested date: True; matching sources: 2/2.

[Summary]
Two candidates were found, but no publicly listed email was found.

[Sources]
1. Founder profile
   URL: https://example.com/founder
   cookie banner and thousands of scraped words

2. Company page
   URL: https://example.com/company
   another full page
"""
        result = present("SEARCH_WEB", raw)
        self.assertEqual(result["title"], "Web Research")
        self.assertEqual(result["status"], "PARTIAL RESULT")
        self.assertIn("Founder profile", result["text"])
        self.assertIn("https://example.com/company", result["links"])
        self.assertEqual(result["sources"][0]["title"], "Founder profile")
        self.assertIn("no publicly listed email", result["summary"])
        self.assertNotIn("cookie banner", result["text"])

    def test_non_web_results_are_preserved(self):
        result = present("LIST_DIRECTORY", "[File] notes.txt")
        self.assertEqual(result["text"], "[File] notes.txt")

    def test_federated_receipt_becomes_compact_source_cards_not_raw_urls(self):
        raw = """RESULT
[Federated knowledge: current comparison]
Sources queried: google_news

1. A useful headline
Source: Google News | Published: Tue, 20 Oct 2026 02:23:00 GMT
URL: https://news.google.com/rss/articles/very-long-token?oc=5
A useful headline repeated
"""
        result = present("KNOWLEDGE_SEARCH", raw)
        self.assertEqual(result["sources"][0]["title"], "A useful headline")
        self.assertEqual(result["sources"][0]["source"], "Google News")
        self.assertIn("20 Oct 2026", result["sources"][0]["published"])
        self.assertFalse(result["synthesized"])

    def test_status_placeholders_are_not_information(self):
        from result_presenter import display_worthy
        for value in ("", "Ready", "Working on it...", "Loading"):
            self.assertFalse(display_worthy(value))
        self.assertTrue(display_worthy("Your pass is waiting for approval."))


if __name__ == "__main__":
    unittest.main()
