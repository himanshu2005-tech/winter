import os
import sys
import unittest
import datetime as dt
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from providers import knowledge_api


class FederatedKnowledgeTests(unittest.TestCase):
    def test_default_routing_is_bounded(self):
        self.assertIn("openalex", knowledge_api._default_sources("research paper on transformers"))
        self.assertIn("stack_overflow", knowledge_api._default_sources("python programming error"))
        self.assertEqual(knowledge_api._default_sources("AI", "news"), knowledge_api.NEWS)

    def test_partial_source_failure_does_not_lose_healthy_results(self):
        good = lambda query, limit: [knowledge_api._row(
            "Good", "Useful result", "https://example.com", "evidence", score=.8)]
        bad = lambda query, limit: (_ for _ in ()).throw(RuntimeError("offline"))
        with mock.patch.dict(knowledge_api.ADAPTERS, {"good": good, "bad": bad}, clear=True):
            data = knowledge_api.search("useful", sources=["good", "bad"], limit=5)
        self.assertEqual(len(data["results"]), 1)
        self.assertIn("bad", data["errors"])

    def test_duplicate_titles_are_collapsed(self):
        one = lambda query, limit: [knowledge_api._row(
            "One", "Same title", "https://one.example", score=.7)]
        two = lambda query, limit: [knowledge_api._row(
            "Two", "Same title!", "https://two.example", score=.9)]
        with mock.patch.dict(knowledge_api.ADAPTERS, {"one": one, "two": two}, clear=True):
            data = knowledge_api.search("same", sources=["one", "two"], limit=5)
        self.assertEqual(len(data["results"]), 1)
        self.assertEqual(data["results"][0]["source"], "Two")

    def test_formatter_preserves_source_url_and_retrieval_time(self):
        data = {"query": "q", "sources": ["x"], "retrieved_at": "now",
                "errors": {}, "results": [knowledge_api._row(
                    "Source", "Title", "https://example.com", "Summary")]}
        text = knowledge_api.format_for_model(data)
        self.assertIn("Source: Source", text)
        self.assertIn("https://example.com", text)
        self.assertIn("Retrieved: now", text)

    def test_openalex_key_is_read_from_credential_manager(self):
        with mock.patch("security.keyring_manager.get_secret_chunked", return_value="secret") as get:
            self.assertEqual(knowledge_api._openalex_key(), "secret")
        get.assert_called_once_with("WINTER_SYSTEM", "openalex_api_key")

    def test_news_drops_unrelated_low_signal_results(self):
        adapter = lambda query, limit: [
            knowledge_api._row("Feed", "Unrelated cooking story", "https://x", score=.7),
            knowledge_api._row("Feed", "New AI internship programme", "https://y", score=.7),
        ]
        with mock.patch.dict(knowledge_api.ADAPTERS, {"feed": adapter}, clear=True):
            data = knowledge_api.search(
                "AI internships", sources=["feed"], limit=5, purpose="news")
        self.assertEqual([row["title"] for row in data["results"]],
                         ["New AI internship programme"])

    def test_broad_news_request_keeps_verified_general_headlines(self):
        now = dt.datetime.now(dt.timezone.utc).isoformat()
        adapter = lambda query, limit: [
            knowledge_api._row(
                "Google News", "India launches new weather satellite",
                "https://news.example/satellite", published=now, score=.65),
            knowledge_api._row(
                "Google News", "Markets close higher after policy update",
                "https://news.example/markets", published=now, score=.65),
        ]
        with mock.patch.dict(knowledge_api.ADAPTERS, {"feed": adapter}, clear=True):
            data = knowledge_api.search(
                "latest news today", sources=["feed"], limit=5, purpose="news")
        self.assertEqual(len(data["results"]), 2)
        self.assertEqual(data["selection"], "broad_current_news")

    def test_news_freshness_affects_ranking(self):
        now = dt.datetime.now(dt.timezone.utc)
        adapter = lambda query, limit: [
            knowledge_api._row(
                "Feed", "AI internship from last year", "https://old.example",
                published=(now - dt.timedelta(days=400)).isoformat(), score=.8),
            knowledge_api._row(
                "Feed", "AI internship applications open today", "https://new.example",
                published=now.isoformat(), score=.7),
        ]
        with mock.patch.dict(knowledge_api.ADAPTERS, {"feed": adapter}, clear=True):
            data = knowledge_api.search(
                "AI internship", sources=["feed"], limit=5, purpose="news")
        self.assertEqual(data["results"][0]["url"], "https://new.example")
        self.assertLess(data["results"][0]["age_hours"], 1)

    def test_generic_google_news_uses_the_top_stories_feed(self):
        with mock.patch.object(knowledge_api, "_rss", return_value=[]) as rss:
            knowledge_api.google_news("news today", limit=4)
        called_url = rss.call_args.args[0]
        self.assertNotIn("/search?", called_url)
        self.assertIn("ceid=IN:en", called_url)

    def test_sensitive_topics_are_not_news_search_interests(self):
        rows = [
            {"topic": "health_problems", "confidence": 1},
            {"topic": "relationship anxiety", "confidence": 1},
            {"topic": "artificial intelligence", "confidence": .9},
        ]
        with mock.patch("memory.neo4j_store.store.query", return_value=rows):
            interests = knowledge_api.personal_interests(".")
        self.assertEqual(interests, ["artificial intelligence"])

    def test_personalized_briefing_balances_general_and_graph_interests(self):
        def fake_search(query, sources=None, limit=5, purpose="knowledge"):
            general = sources == ("google_news",)
            title = "India general headline" if general else "Student career headline"
            return {"query": query, "sources": list(sources or knowledge_api.NEWS),
                    "selection": "x", "errors": {}, "retrieved_at": "now",
                    "results": [knowledge_api._row(
                        "Google News", title,
                        "https://news.example/general" if general else
                        "https://news.example/personal")]}
        with mock.patch.object(knowledge_api, "personal_interests",
                               return_value=["student careers"]), \
                mock.patch.object(knowledge_api, "search",
                                  side_effect=fake_search):
            data = knowledge_api.personalized_news(".", limit=4)
        self.assertEqual(data["selection"], "balanced_general_and_personal")
        self.assertEqual({row["news_scope"] for row in data["results"]},
                         {"general", "personal"})


if __name__ == "__main__":
    unittest.main()
